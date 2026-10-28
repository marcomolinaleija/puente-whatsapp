from __future__ import annotations
from .message_info import ingest_message, record_receipt

import asyncio
from concurrent.futures import CancelledError as CallbackCancelled
import json
import warnings
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast
from urllib.parse import quote as url_quote
from xml.etree import ElementTree as ET

import sqlalchemy
from slidge import BaseSession, global_config
from slidge.command import FormField, SearchResult
from slidge.command.user import SyncContacts
from slidge.contact.roster import ContactIsUser
from slidge.db.models import ArchivedMessage, GatewayUser
from slidge.util import is_valid_phone_number
from slidge.util.types import (
    Avatar,
    LegacyAttachment,
    MessageReference,
    PseudoPresenceShow,
)
from slidge.util.types import LinkPreview as SlidgeLinkPreview
from slixmpp.exceptions import XMPPError
from slixmpp.types import ResourceDict

from .contact import Contact, Roster
from .gateway import Gateway
from .generated import go, whatsapp
from .sticker_pack import normalize_pack
from .group import MUC, Bookmarks, Participant
import time
from .deepgram_transcription import (
    build_audio_dedup_key,
    claim_audio,
    complete_audio,
    format_audio_duration,
    inspect_audio,
    is_audio_attachment,
    release_audio,
    transcription_enabled,
    transcribe_audio,
)

MESSAGE_PAIR_SUCCESS = (
    "Pairing successful! You might need to repeat this process in the future if the"
    " Linked Device is re-registered from your main device."
)

MESSAGE_LOGGED_OUT = (
    "You have been logged out, please use the re-login adhoc command "
    "and re-scan the QR code on your main device."
)


Recipient = Contact | MUC

POLL_NAMESPACE = "urn:marco-ml:whatsapp:poll:0"
CALL_NAMESPACE = "urn:marco-ml:whatsapp:call:1"
CALL_CONTRACT_VERSION = 1
CALL_TRANSPORT_PREFIX = "call-contract-v1:"
_CALL_FALLBACK_LABELS = {
    "offered": "Incoming call",
    "accepted": "Call accepted",
    "missed": "Missed call",
    "rejected": "Call rejected",
    "ended": "Call ended",
    "failed": "Call failed",
}


def parse_call_metadata(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str) or not raw.startswith(CALL_TRANSPORT_PREFIX):
        return None
    try:
        metadata = json.loads(raw.removeprefix(CALL_TRANSPORT_PREFIX))
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("version") != CALL_CONTRACT_VERSION:
        return None
    if not isinstance(metadata.get("call_id"), str) or not metadata["call_id"]:
        return None
    if not isinstance(metadata.get("sequence"), int) or metadata["sequence"] < 1:
        return None
    return metadata


def make_call_extension(metadata: dict[str, object]) -> ET.Element:
    attributes = {
        "contract-version": str(CALL_CONTRACT_VERSION),
        "call-id": str(metadata["call_id"]),
        "direction": str(metadata.get("direction") or "unknown"),
        "kind": str(metadata.get("kind") or "unknown"),
        "state": str(metadata.get("state") or "unknown"),
        "sequence": str(metadata["sequence"]),
    }
    for source, target in (
        ("peer_jid", "peer-jid"),
        ("chat_jid", "chat-jid"),
        ("group_jid", "group-jid"),
        ("event_timestamp", "event-timestamp"),
        ("answered_at", "answered-at"),
        ("ended_at", "ended-at"),
        ("terminal_reason", "terminal-reason"),
        ("outcome", "outcome"),
        ("source", "source"),
    ):
        value = metadata.get(source)
        if isinstance(value, str) and value:
            attributes[target] = value
    duration = metadata.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        attributes["duration-seconds"] = str(duration)
    return ET.Element(f"{{{CALL_NAMESPACE}}}call", attributes)




P = ParamSpec("P")
T = TypeVar("T")


WrappedSessionMethod = Callable[Concatenate["Session", P], Coroutine[Any, Any, T]]


def ignore_contact_is_user(
    func: WrappedSessionMethod[P, T],
) -> WrappedSessionMethod[P, T | None]:
    @wraps(func)
    async def wrapped(self: Session, /, *a: P.args, **k: P.kwargs) -> T | None:
        try:
            return await func(self, *a, **k)
        except ContactIsUser as e:
            self.log.debug("A wild ContactIsUser has been raised!", exc_info=e)
            return None

    return wrapped


class Session(BaseSession[Contact]):
    xmpp: Gateway
    contacts: Roster
    bookmarks: Bookmarks

    def __init__(self, user: GatewayUser) -> None:
        super().__init__(user)
        try:
            device = whatsapp.LinkedDevice(ID=self.user.legacy_module_data["device_id"])  # type:ignore[no-untyped-call]
        except KeyError:
            device = whatsapp.LinkedDevice()  # type:ignore[no-untyped-call]
        self.__presence_status: str = ""
        self.user_phone: str | None = None
        self.whatsapp: whatsapp.Session = self.xmpp.whatsapp.NewSession(device)
        self.__handle_event = make_sync(self.handle_event, self.xmpp.loop)
        self.whatsapp.SetEventHandler(self.__handle_event)
        self.__reset_connected()
        self.__roster_sync_task: asyncio.Task[None] | None = None

    async def login(self) -> str:
        """
        Initiate login process and connect session to WhatsApp. Depending on existing state, login
        might either return having initiated the Linked Device registration process in the background,
        or will re-connect to a previously existing Linked Device session.
        """
        self.__reset_connected()
        self.whatsapp.Login()  # type:ignore[no-untyped-call]
        return await self.__connected

    async def logout(self) -> None:
        """
        Disconnect the active WhatsApp session. This will not remove any local or remote state, and
        will thus allow previously authenticated sessions to re-authenticate without needing to pair.
        """
        self.whatsapp.Disconnect()  # type:ignore[no-untyped-call]
        self.logged = False

    @ignore_contact_is_user
    async def handle_event(self, event_kind: int, ptr: int) -> None:
        """
        Handle incoming event, as propagated by the WhatsApp adapter. Typically, events carry all
        state required for processing by the Gateway itself, and will do minimal processing themselves.
        """
        if event_kind == whatsapp.EventUnknown:
            return
        if event_kind not in (
            whatsapp.EventQRCode,
            whatsapp.EventPairDeviceID,
            whatsapp.EventConnect,
            whatsapp.EventLoggedOut,
        ):
            await self.contacts.ready
            await self.bookmarks.ready
        event = whatsapp.EventPayload(handle=ptr)  # type:ignore[no-untyped-call]
        match event_kind:
            case whatsapp.EventQRCode:
                await self.on_wa_qr(event.QRCode)
            case whatsapp.EventConnect:
                await self.on_wa_connect(event.Connect)
            case whatsapp.EventPairDeviceID:
                await self.on_wa_pair(event.PairDeviceID)
            case whatsapp.EventLoggedOut:
                await self.on_wa_logged_out(event.LoggedOut)
            case whatsapp.EventContact:
                await self.on_wa_contact(event.Contact)
            case whatsapp.EventPresence:
                await self.on_wa_presence(event.Presence)
            case whatsapp.EventMessage:
                await self.on_wa_message(event.Message)
            case whatsapp.EventChatState:
                await self.on_wa_chat_state(event.ChatState)
            case whatsapp.EventReceipt:
                await self.on_wa_receipt(event.Receipt)
            case whatsapp.EventGroup:
                await self.on_wa_group(event.Group)
            case whatsapp.EventCall:
                await self.on_wa_call(event.Call)
            case whatsapp.EventAvatar:
                await self.on_wa_avatar(event.Avatar)
            case _:
                self.log.warning("No handler for event of kind %s", event_kind)

    async def on_wa_qr(self, qr: str) -> None:
        self.send_gateway_status("QR Scan Needed", show="dnd")
        await self.send_qr(qr)

    async def on_wa_pair(self, device_id: str) -> None:
        self.send_gateway_message(MESSAGE_PAIR_SUCCESS)
        self.legacy_module_data_set({"device_id": device_id})

    async def on_wa_connect(self, connect: whatsapp.Connect) -> None:
        # CAN: re-pair can change the phone identity even after the login future completed.
        if not connect.Error and connect.JID:
            if self.contacts.user_legacy_id != connect.JID:
                self._can_own_occupant_id = None
            self.contacts.user_legacy_id = connect.JID
            self.user_phone = "+" + connect.JID.split("@")[0]
        # On re-pair, Session.login() is not called by slidge core, so the status message is
        # not updated.
        if self.__connected.done():
            if connect.Error != "":
                self.send_gateway_status("Connection error", show="dnd")
                self.send_gateway_message(connect.Error)
            else:
                self.send_gateway_status(
                    self.__get_connected_status_message(), show="chat"
                )
        elif connect.Error != "":
            self.xmpp.loop.call_soon_threadsafe(
                self.__connected.set_exception,
                XMPPError("internal-server-error", connect.Error),
            )
        else:
            self.contacts.user_legacy_id = connect.JID
            self.user_phone = "+" + connect.JID.split("@")[0]
            self.xmpp.loop.call_soon_threadsafe(
                self.__connected.set_result, self.__get_connected_status_message()
            )

        if connect.Error == "":
            self.__schedule_roster_sync()

    def __schedule_roster_sync(self) -> None:
        if self.__roster_sync_task is not None and not self.__roster_sync_task.done():
            return
        self.__roster_sync_task = self.create_task(
            self.__sync_roster_after_connect(),
            name=f"whatsapp-roster-sync-{self.user_jid.bare}",
        )

    async def __sync_roster_after_connect(self) -> None:
        try:
            await self.contacts.ready
            # CAN nonblocking roster v1
            wa_contacts = await asyncio.to_thread(
                lambda: list(self.whatsapp.GetContacts(refresh=True))
            )
            saved_contacts = sum(bool(contact.IsFriend) for contact in wa_contacts)
            self.log.info(
                "Loaded %d WhatsApp contacts, including %d saved contacts",
                len(wa_contacts),
                saved_contacts,
            )
            self.__authoritative_saved_contacts = {
                self.contacts._canonical_legacy_id(contact.Actor.JID): contact
                for contact in wa_contacts
                if contact.Actor.JID and contact.IsFriend
            }
            try:
                await asyncio.sleep(5)
                for wa_contact in wa_contacts:
                    await self.contacts.add_whatsapp_contact(wa_contact)
                result = await SyncContacts.sync(  # type:ignore
                    self, self, self.user_jid
                )
            finally:
                self.__authoritative_saved_contacts = {}
            self.log.info("Automatic XMPP roster sync completed: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("Automatic XMPP roster sync failed")

    async def on_wa_logged_out(self, logged_out: whatsapp.LoggedOut) -> None:
        self.logged = False
        message = MESSAGE_LOGGED_OUT
        if logged_out.Reason:
            message += f"\nReason: {logged_out.Reason}"
        self.send_gateway_message(message)
        self.send_gateway_status("Logged out", show="away")
        for muc in self.bookmarks:
            # When we are logged out, the initial history sync may not completely
            # cover the "hole" between logout and re-pair, so we want to request
            # more history.
            muc.history_requested = False

    async def on_wa_contact(self, wa_contact: whatsapp.Contact) -> None:
        if wa_contact.Actor.JID:
            canonical_id = self.contacts._canonical_legacy_id(wa_contact.Actor.JID)
            authoritative_contacts = getattr(
                self, "_Session__authoritative_saved_contacts", {}
            )
            wa_contact = authoritative_contacts.get(canonical_id, wa_contact)
            contact = await self.contacts.add_whatsapp_contact(wa_contact)
            if contact is not None and contact.is_friend:
                # slidge core would do that automatically if the is_friend flag
                # was set in update_info(), but it actually happens in
                # update_whatsapp_info()
                await contact.add_to_roster()
        elif wa_contact.Actor.LID:
            await self.bookmarks.rename_anonymous_participants(wa_contact)

    async def on_wa_group(self, group: whatsapp.Group) -> None:
        await self.bookmarks.add_whatsapp_group(group)

    async def on_wa_presence(self, presence: whatsapp.Presence) -> None:
        if presence.Actor.JID:
            contact = await self.contacts.by_legacy_id(presence.Actor.JID)
            await contact.update_presence(presence.Kind, presence.LastSeen)
        # TODO: LID participant presence update?

    async def on_wa_chat_state(self, state: whatsapp.ChatState) -> None:
        if (
            not state.Chat.IsGroup
            and (not state.Actor.JID or not state.Chat.JID)
        ):
            # For unknown/new contacts, we receive 1:1 *LID* chat states.
            # We currently have no way to map those, so let's ignore them.
            return

        contact, _muc = await self.__get_contact_or_participant(state.Chat, state.Actor)
        if state.Kind == whatsapp.ChatStateComposing:
            contact.composing(media=state.Media)
        elif state.Kind == whatsapp.ChatStatePaused:
            contact.paused()

    async def on_wa_receipt(self, receipt: whatsapp.Receipt) -> None:
        """
        Handle incoming delivered/read receipt, as propagated by the WhatsApp adapter.
        """
        try:
            contact, _muc = await self.__get_contact_or_participant(
                receipt.Chat, receipt.Actor
            )
        except ValueError:
            self.log.warning("What do with this receipt? %s", receipt)
            return
        await record_receipt(self, receipt, contact)
        for message_id in receipt.MessageIDs:
            if receipt.Kind == whatsapp.ReceiptDelivered:
                contact.received(message_id)
            elif receipt.Kind == whatsapp.ReceiptRead:
                contact.displayed(legacy_msg_id=message_id, carbon=receipt.Actor.IsMe)
    
    async def on_wa_call(self, call: whatsapp.Call) -> None:
        metadata = parse_call_metadata(call.Actor.LID)
        group_jid = metadata.get("group_jid") if metadata else None
        contact = None
        recipient_name = ""
        canonical_chat_jid = ""
        if isinstance(group_jid, str) and group_jid:
            muc = await self.bookmarks.by_legacy_id(group_jid)
            recipient_name = muc.name or str(muc.jid.bare)
            canonical_chat_jid = str(muc.jid.bare).strip()
        else:
            peer_jid = call.Actor.JID
            if not peer_jid and metadata:
                raw_peer = metadata.get("peer_jid")
                if isinstance(raw_peer, str):
                    peer_jid = raw_peer
            if not peer_jid:
                warnings.warn("Ignoring a call without a resolvable peer or group")
                return
            contact = await self.contacts.by_legacy_id(peer_jid)
            recipient_name = contact.name or "tel:" + str(contact.jid.local)
            canonical_chat_jid = str(contact.jid.bare).strip()

        state = str(metadata.get("state")) if metadata else ""
        outcome = str(metadata.get("outcome") or "") if metadata else ""
        direction = str(metadata.get("direction") or "") if metadata else ""
        kind = str(metadata.get("kind") or "") if metadata else ""
        if outcome:
            text = f"{direction.capitalize() or 'Call'} {kind} call: {outcome.replace('_', ' ')}"
        else:
            text = _CALL_FALLBACK_LABELS.get(state, "Call")
            if not metadata:
                if call.State == whatsapp.CallIncoming:
                    text = "Incoming call"
                elif call.State == whatsapp.CallMissed:
                    text = "Missed call"
        text += f" with {recipient_name}"
        duration = metadata.get("duration_seconds") if metadata else None
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            text += f", {duration:g} seconds"

        call_at: datetime | None = None
        timestamp = metadata.get("event_timestamp") if metadata else None
        if isinstance(timestamp, str) and timestamp:
            try:
                call_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                call_at = None
        elif call.Timestamp > 0:
            call_at = datetime.fromtimestamp(call.Timestamp, tz=UTC)
        if call_at is not None:
            text += f" at {call_at.astimezone(UTC)}"
        message_kwargs: dict[str, object] = {}
        if call_at is not None:
            message_kwargs["when"] = call_at
        if metadata:
            if canonical_chat_jid:
                metadata["chat_jid"] = canonical_chat_jid
            message_kwargs["extra_xml"] = make_call_extension(metadata)
        self.send_gateway_message(text, **message_kwargs)

    async def on_wa_message(self, message: whatsapp.Message) -> None:
        """
        Handle incoming message, as propagated by the WhatsApp adapter. Messages can be one of many
        types, including plain-text messages, media messages, reactions, etc., and may also include
        other aspects such as references to other messages for the purposes of quoting or correction.
        """
        await ingest_message(self, message)
        # Skip handing message that's already in our message archive.
        if (
            message.Chat.IsGroup
            and message.IsHistory
            and await self.__is_message_in_archive(message.ID)
        ):
            # FIXME: this only works for messages with a body
            # Messages without body have no "legacy_msg_id" attached to them. In practice, this means
            # we fill our MAM table with (hopefully just a few) duplicate rows for all reactions, receipts,
            # displayed markers, retractions and corrections.
            return
        try:
            actor, muc = await self.__get_contact_or_participant(
                message.Chat, message.Actor
            )
        except ValueError:
            self.log.warning(
                "Ignoring WhatsApp message with unresolved anonymous actor: "
                "chat=%s actor_jid=%s actor_lid=%s",
                message.Chat.JID,
                message.Actor.JID,
                message.Actor.LID,
            )
            return
        if message.GroupInvite.JID:
            muc = await self.bookmarks.by_legacy_id(message.GroupInvite.JID)
            muc_name = f"{muc.name} xmpp:{url_quote(muc.jid.user)}@{muc.jid.server}"
            self.send_gateway_message(
                f"Received group invite for {muc_name} from {actor.name}, auto-joining…"
            )

        match message.Kind:
            case whatsapp.MessagePlain:
                await self.on_wa_msg_plain(message, actor, muc)
            case whatsapp.MessageEdit:
                await self.on_wa_msg_edit(message, actor, muc)
            case whatsapp.MessageRevoke:
                await self.on_wa_msg_revoke(message, actor, muc)
            case whatsapp.MessageReaction:
                await self.on_wa_msg_reaction(message, actor, muc)
            case whatsapp.MessageAttachment:
                await self.on_wa_msg_attachment(message, actor, muc)
            case whatsapp.MessagePoll:
                await self.on_wa_msg_poll(message, actor, muc)

        for receipt in message.Receipts:
            await self.on_wa_receipt(receipt)
        for reaction in message.Reactions:
            await self.on_wa_message(reaction)

    def __get_timestamp(self, message: whatsapp.Message) -> datetime | None:
        return (
            datetime.fromtimestamp(message.Timestamp, tz=UTC)
            if message.Timestamp > 0
            else None
        )

    async def on_wa_msg_plain(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        actor.send_text(
            body=await self.__get_body(message, muc),
            legacy_msg_id=message.ID,
            when=self.__get_timestamp(message),
            reply_to=await self.__get_reply_to(message, muc),
            carbon=message.Actor.IsMe,
            link_previews=_get_link_previews(message.Preview),
            is_forwarded=message.IsForwarded,
        )

    async def on_wa_msg_attachment(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        attachments = await Attachment.convert_list(message.Attachments, muc)
        transcription_jobs = []
        if not message.IsHistory and not message.Actor.IsMe:
            for index, attachment in enumerate(attachments):
                if is_audio_attachment(attachment):
                    data = bytes(attachment.data or b"")
                    transcription_jobs.append(
                        (
                            build_audio_dedup_key(message.ID, index, data),
                            data,
                            str(attachment.content_type or ""),
                            str(attachment.name or ""),
                        )
                    )

        await actor.send_files(
            attachments=attachments,
            legacy_msg_id=message.ID,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
            is_forwarded=message.IsForwarded,
        )
        account = str(self.user_jid.bare)
        chat = str(muc.jid.bare if muc is not None else actor.jid.bare)
        for dedupe_key, data, content_type, filename in transcription_jobs:
            if data and transcription_enabled(account, chat):
                self.create_task(
                    self.__transcribe_and_send(
                        actor,
                        account,
                        dedupe_key,
                        data,
                        content_type,
                        filename,
                    ),
                    name="deepgram-transcription",
                )
        for attachment in attachments:
            if getattr(global_config, "NO_UPLOAD_PATH", None):
                continue
            if getattr(global_config, "NO_UPLOAD_METHOD", None) == "symlink":
                continue
            self.log.debug("Removing '%s' from disk", attachment.path)
            if attachment.path is None:
                continue
            Path(attachment.path).unlink(missing_ok=True)

    async def __transcribe_and_send(
        self,
        actor: Contact | Participant,
        account: str,
        dedupe_key: str,
        data: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        if not claim_audio(account, dedupe_key):
            self.log.info("Skipping duplicate audio transcription: %s", dedupe_key)
            return
        try:
            inspection = await inspect_audio(data, content_type, filename)
            if not inspection.accepted:
                self.log.info(
                    "Skipping Deepgram transcription for %s: %s",
                    filename or "audio",
                    inspection.reason,
                )
                complete_audio(account, dedupe_key)
                return
            started_at = time.perf_counter()
            transcript = await transcribe_audio(
                self.http,
                data,
                content_type=content_type,
                filename=filename,
            )
            elapsed = time.perf_counter() - started_at
            complete_audio(account, dedupe_key)
        except Exception as exc:
            release_audio(account, dedupe_key)
            self.log.warning(
                "Deepgram transcription failed for %s (%d bytes): %s",
                filename or "audio",
                len(data),
                exc,
            )
            return
        if transcript:
            duration = (
                f" Audio: {format_audio_duration(inspection.duration_seconds)}."
                if inspection.duration_seconds is not None
                else ""
            )
            actor.send_text(
                f'Transcripción: "{transcript}".{duration} '
                f"Transcrito en {elapsed:.1f} s."
            )

    async def on_wa_msg_edit(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        actor.correct(
            legacy_msg_id=message.ReferenceID,
            new_text=message.Body,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
            correction_event_id=message.ID,
        )

    async def on_wa_msg_revoke(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        if muc is None or message.OriginActor.JID == message.Actor.JID:
            actor.retract(legacy_msg_id=message.ID, carbon=message.Actor.IsMe)
        else:
            assert isinstance(actor, Participant)
            actor.moderate(legacy_msg_id=message.ID)

    async def on_wa_msg_reaction(
        self, message: whatsapp.Message, actor: Contact | Participant, _muc: MUC | None
    ) -> None:
        emojis = [message.Body] if message.Body else []
        actor.react(legacy_msg_id=message.ID, emojis=emojis, carbon=message.Actor.IsMe)

    async def on_wa_msg_poll(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        if message.ReferenceID:
            self.log.info(
                "Forwarding WhatsApp poll update event=%s reference=%s actor=%s own=%s muc=%s",
                message.ID,
                message.ReferenceID,
                message.Actor.JID,
                message.Actor.IsMe,
                muc is not None,
            )
            update_xml = ET.Element(
                f"{{{POLL_NAMESPACE}}}poll-update",
                {
                    "id": message.ReferenceID,
                    "voter": message.Actor.JID,
                    "voter-is-me": str(message.Actor.IsMe).lower(),
                },
            )
            if message.Actor.LID:
                update_xml.set("voter-lid", message.Actor.LID)
            for option in message.Poll.Options:
                ET.SubElement(
                    update_xml,
                    f"{{{POLL_NAMESPACE}}}option",
                    {"hash": option.Title},
                )
            actor.send_text(
                # Keep a whitespace body because some XMPP routing layers discard
                # extension-only messages. The client strips it before rendering.
                body=" ",
                legacy_msg_id=message.ID,
                when=self.__get_timestamp(message),
                carbon=message.Actor.IsMe,
                extra_xml=update_xml,
            )
            self.log.info(
                "Forwarded WhatsApp poll update event=%s reference=%s to XMPP",
                message.ID,
                message.ReferenceID,
            )
            return

        fallback_body = f"\U0001f5f3 {message.Poll.Title}"
        options = [option.Title for option in message.Poll.Options if option.Title]
        for option in options:
            fallback_body = fallback_body + f"\n\u2610 {option}"

        selection_mode, separator, selectable_value = (message.Body or "").partition(":")
        if not separator or selection_mode not in {"single", "multiple"}:
            selection_mode = "single"
            selectable_value = "1"
        try:
            selectable_count = int(selectable_value)
        except (TypeError, ValueError):
            selectable_count = 1
        if selectable_count <= 0 or selectable_count > len(options):
            selectable_count = len(options) if selection_mode == "multiple" else 1
        if selection_mode == "single":
            selectable_count = 1

        poll_xml = ET.Element(
            f"{{{POLL_NAMESPACE}}}poll",
            {
                "id": message.ID,
                "title": message.Poll.Title,
                "creator": message.Actor.JID,
                "creator-is-me": str(message.Actor.IsMe).lower(),
                "max-selections": str(selectable_count),
                "selection-mode": selection_mode,
            },
        )
        if message.Actor.LID:
            poll_xml.set("creator-lid", message.Actor.LID)
        for option in options:
            ET.SubElement(poll_xml, f"{{{POLL_NAMESPACE}}}option").text = option

        actor.send_text(
            body=fallback_body,
            legacy_msg_id=message.ID,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
            extra_xml=poll_xml,
        )
    async def on_wa_avatar(self, avatar: whatsapp.Avatar) -> None:
        if avatar.IsGroup:
            chat: MUC | Contact = await self.bookmarks.by_legacy_id(avatar.ResourceID)
        else:
            chat = await self.contacts.by_legacy_id(avatar.ResourceID)
        chat.avatar = Avatar(url=avatar.URL or None, unique_id=avatar.ID or None)

    async def on_presence(
        self,
        resource: str,
        show: PseudoPresenceShow,
        status: str,
        resources: dict[str, ResourceDict],
        merged_resource: ResourceDict | None,
    ) -> None:
        """
        Send outgoing availability status (i.e. presence) based on combined status of all connected
        XMPP clients.
        """
        if not merged_resource:
            self.whatsapp.SendPresence(whatsapp.PresenceUnavailable, "")  # type:ignore[no-untyped-call]
        else:
            presence = (
                whatsapp.PresenceAvailable
                if merged_resource["show"] in ["chat", ""]
                else whatsapp.PresenceUnavailable
            )
            status = (
                merged_resource["status"]
                if self.__presence_status != merged_resource["status"]
                else ""
            )
            if status:
                self.__presence_status = status
            self.whatsapp.SendPresence(presence, status)  # type:ignore[no-untyped-call]

    async def on_avatar(
        self,
        bytes_: bytes | None,
        hash_: str | None,
        type_: str | None,
        width: int | None,
        height: int | None,
    ) -> None:
        """The own WhatsApp profile picture is managed in WhatsApp only."""
        # Missing XMPP metadata is not a request to delete a WhatsApp picture.
        # Also reject nonempty updates, including stale XMPP profile images.
        raise NotImplementedError("WhatsApp profile picture synchronization is disabled")

    async def on_create_group(self, name: str, contacts: list[Contact]) -> str:
        """
        Creates a WhatsApp group for the given human-readable name and participant list.
        """
        group = self.whatsapp.CreateGroup(
            name,
            go.Slice_string([c.legacy_id for c in contacts]),  # type:ignore[no-untyped-call]
        )
        muc = await self.bookmarks.by_legacy_id(group.JID)
        return muc.legacy_id

    async def on_search(self, form_values: dict[str, str]) -> SearchResult | None:
        """
        Searches for, and automatically adds, WhatsApp contact based on phone number. Phone numbers
        not registered on WhatsApp will be ignored with no error.
        """
        phone = form_values.get("phone")
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number", phone)

        data: whatsapp.Contact = self.whatsapp.FindContact(phone)  # type:ignore[no-untyped-call]
        if not data.Actor.JID:
            return None

        contact = await self.contacts.add_whatsapp_contact(data)
        assert contact is not None
        await contact.add_to_roster()

        return SearchResult(
            fields=[FormField("phone"), FormField("jid", type="jid-single")],
            items=[{"phone": cast(str, phone), "jid": contact.jid.bare}],
        )

    async def on_preferences(
        self, previous: dict[str, Any], new: dict[str, Any]
    ) -> None:
        if previous.get("roster_add_non_friends") != new.get("roster_add_non_friends"):
            self.log.debug(
                "Running contact sync after group contacts in roster policy change"
            )
            # This updates the "friend" status of contacts
            wa_contacts = await asyncio.to_thread(
                lambda: list(self.whatsapp.GetContacts(refresh=True))
            )
            for wa_contact in wa_contacts:
                await self.contacts.add_whatsapp_contact(wa_contact)
            # This works but is really hacky, slidge core should expose this more cleanly
            await SyncContacts.sync(self, self, self.user_jid)  # type:ignore

    def message_is_carbon(self, c: Recipient, legacy_msg_id: str) -> bool:
        with self.xmpp.store.session() as orm:
            return bool(
                self.xmpp.store.id_map.get_xmpp(
                    orm, c.stored.id, legacy_msg_id, c.is_group
                )
            )

    def __reset_connected(self) -> None:
        if hasattr(self, "__connected") and not self.__connected.done():
            self.xmpp.loop.call_soon_threadsafe(self.__connected.cancel)
        self.__connected: asyncio.Future[str] = self.xmpp.loop.create_future()

    def __get_connected_status_message(self) -> str:
        return f"Connected as {self.user_phone}"

    async def __get_body(
        self, message: whatsapp.Message, muc: MUC | None = None
    ) -> str:
        body: str = message.Body
        if muc:
            body = await muc.replace_mentions(body)
        if message.Location.Latitude != 0 or message.Location.Longitude != 0:
            body = f"geo:{message.Location.Latitude:f},{message.Location.Longitude:f}"
            if message.Location.Accuracy > 0:
                body += ";u={message.Location.Accuracy:d}"
        if message.Album.IsAlbum:
            body += "Album: "
            if message.Album.ImageCount > 0:
                body += f"{message.Album.ImageCount} photos, "
            if message.Album.VideoCount > 0:
                body += f"{message.Album.VideoCount} videos"
            body = body.rstrip(" ,:")
        return body

    async def __get_reply_to(
        self, message: whatsapp.Message, muc: MUC | None = None
    ) -> MessageReference | None:
        if not message.ReplyID:
            return None
        reply_to = MessageReference(
            legacy_id=message.ReplyID,
            body=(
                message.ReplyBody
                if muc is None
                else await muc.replace_mentions(message.ReplyBody)
            ),
        )
        # reply fallback without author
        reply_actor, quoted_group_jid = _private_group_reply_context(
            message.OriginActor
        )
        # WhatsApp may omit the quoted participant on a self-sent message. Keep
        # the native reply ID usable in XMPP even when the author is unknown.
        if not reply_actor.JID and not reply_actor.LID:
            return reply_to
        if quoted_group_jid:
            quoted_chat = whatsapp.Chat(JID=quoted_group_jid, IsGroup=True)
            reply_to.author, _muc = await self.__get_contact_or_participant(
                quoted_chat, reply_actor
            )
        elif self.contacts.user_legacy_id == reply_actor.JID:
            reply_to.author = "user"
        else:
            reply_to.author, _muc = await self.__get_contact_or_participant(
                message.Chat, reply_actor
            )
        return reply_to

    async def __is_message_in_archive(self, legacy_msg_id: str) -> bool:
        with self.xmpp.store.session() as orm:
            return bool(
                orm.scalar(
                    sqlalchemy.exists()
                    .where(ArchivedMessage.legacy_id == legacy_msg_id)
                    .select()
                )
            )

    async def __get_contact_or_participant(
        self, chat: whatsapp.Chat, actor: whatsapp.Actor
    ) -> tuple[Contact | Participant, MUC | None]:
        """
        Return either a Contact or a Participant instance for the given contact and group JIDs.
        """
        if chat.IsGroup:
            muc = await self.bookmarks.by_legacy_id(chat.JID)
            if actor.IsMe:
                return (
                    await muc.get_user_participant(occupant_id=actor.LID or None),
                    muc,
                )
            elif actor.JID:
                return (
                    await muc.get_participant_by_legacy_id(
                        actor.JID, occupant_id=actor.LID or None
                    ),
                    muc,
                )
            else:
                assert actor.LID
                return await muc.get_participant(occupant_id=actor.LID), muc
        elif not actor.JID or not chat.JID:
            raise ValueError("Contact or chat for anonymous JID")
        else:
            return await self.contacts.by_legacy_id(chat.JID), None


STICKER_CAPTION_MARKER = "\x1eWHATSAPP_STICKER\x1f"


class Attachment(LegacyAttachment):
    @staticmethod
    async def convert_list(
        attachments: list[whatsapp.Attachment], muc: MUC | None = None
    ) -> list[Attachment]:
        return [await Attachment.convert(attachment, muc) for attachment in attachments]

    @staticmethod
    async def convert(
        wa_attachment: whatsapp.Attachment, muc: MUC | None = None
    ) -> Attachment:
        caption = (
            wa_attachment.Caption
            if muc is None
            else await muc.replace_mentions(wa_attachment.Caption)
        )
        if wa_attachment.MIME == "application/x-whatsapp-sticker-pack":
            try:
                data = await normalize_pack(bytes(wa_attachment.Data))
            except Exception:
                # Keep the message visible without leaking media keys/paths in diagnostics.
                caption = "No se pudo preparar el paquete de stickers. Pide que lo reenvíen."
                return Attachment(content_type="text/plain", data=caption.encode(),
                                  caption=caption, name="paquete-no-disponible.txt")
            return Attachment(content_type="application/zip", data=data,
                              caption=caption, name="stickers.canstickers")
        is_sticker = caption.startswith(STICKER_CAPTION_MARKER)
        if is_sticker:
            caption = caption.removeprefix(STICKER_CAPTION_MARKER).strip()
        return Attachment(
            content_type=wa_attachment.MIME,
            data=bytes(wa_attachment.Data),
            caption=caption,
            name=wa_attachment.Filename,
            is_sticker=is_sticker,
        )


def _private_group_reply_context(actor: whatsapp.Actor) -> tuple[whatsapp.Actor, str]:
    lid, separator, group_jid = actor.LID.partition("\x1f")
    if not separator or not group_jid:
        return actor, ""
    return whatsapp.Actor(JID=actor.JID, LID=lid, IsMe=actor.IsMe), group_jid


def add_quote_prefix(text: str) -> str:
    """
    Return multi-line text with leading quote marks (i.e. the ">" character).
    """
    return "\n".join(("> " + x).strip() for x in text.split("\n")).strip()


def make_sync(
    func: Callable[P, Coroutine[Any, Any, T]], loop: asyncio.AbstractEventLoop
) -> Callable[P, T]:
    """
    Wrap async function in synchronous operation, running against the given loop in thread-safe mode.
    """

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        result = func(*args, **kwargs)
        future = asyncio.run_coroutine_threadsafe(result, loop)
        try:
            return future.result()
        except CallbackCancelled:
            return None

    return wrapper


def _get_link_previews(preview: whatsapp.Preview) -> list[SlidgeLinkPreview]:
    if preview:
        return [
            SlidgeLinkPreview(
                about=preview.URL,
                title=preview.Title or None,
                description=preview.Description or None,
                url=None,
                image=_bytes_conversion(preview.Thumbnail),
                type=None,
                site_name=None,
            )
        ]
    return []


def _bytes_conversion(slice_byte: go.Slice_byte) -> bytes | None:
    if len(slice_byte) == 0:
        # if we call bytes() on an empty one, we get panic:
        # panic: runtime error: index out of range [0] with length 0
        return None
    return bytes(slice_byte)

# CAN inbound isolation v1
from .inbound import install as _install_inbound
_install_inbound(Session, whatsapp)
