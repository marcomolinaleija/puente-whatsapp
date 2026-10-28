import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from slidge.contact import LegacyContact, LegacyRoster
from slidge.util.types import XMPPMessage
from slixmpp.exceptions import XMPPError

from . import config
from .generated import whatsapp
from .mixins import AvatarMixin, RecipientMixin, strip_quote_prefix

if TYPE_CHECKING:
    from .session import Session


def _modern_mexico_legacy_id(legacy_id: str) -> str | None:
    local, separator, server = legacy_id.partition("@")
    if (
        separator
        and server == whatsapp.DefaultUserServer
        and local.startswith("521")
        and len(local) == 13
        and local.isdigit()
    ):
        return f"52{local[3:]}@{server}"
    return None


class Contact(AvatarMixin, RecipientMixin, LegacyContact):
    session: "Session"

    CORRECTION = True
    REACTIONS_SINGLE_EMOJI = True

    async def update_presence(
        self, presence: whatsapp.PresenceKind, last_seen_timestamp: int
    ) -> None:
        if last_seen_timestamp > 0:
            last_seen = datetime.fromtimestamp(last_seen_timestamp, tz=UTC)
        else:
            cached_presence = self._get_last_presence()
            last_seen = (
                cached_presence.last_seen if cached_presence is not None else None
            )
        if presence == whatsapp.PresenceUnavailable:  # type:ignore[comparison-overlap]
            self.away(last_seen=last_seen)
        else:
            self.online(last_seen=last_seen)

    async def update_info(self) -> None:
        if whatsapp.IsAnonymousJID(self.legacy_id):  # type:ignore[no-untyped-call]
            raise XMPPError(
                "item-not-found", f"LIDs are not valid contact IDs: {self.legacy_id}"
            )
        # If we receive presences, the status will be updated accordingly. But presences do not
        # work reliably, and having contacts offline has annoying side effects, such as contacts not
        # appearing in the participant list of groups.
        self.send_last_presence(force=True, no_cache_online=True)

    async def update_whatsapp_info(self, wa_contact: whatsapp.Contact) -> None:
        with self.updating_info():
            self.session.log.debug(
                "User named %s, friend: %s", wa_contact.Name, wa_contact.IsFriend
            )
            self.name = wa_contact.Name
            self.is_friend = bool(
                wa_contact.IsFriend
                or self.session.user.preferences.get("roster_add_non_friends", True)
            )
            await self.update_whatsapp_avatar()
            self.set_vcard(full_name=self.name, phone=str(self.jid.local))

    def _wa_legacy_id(self) -> str:
        return self.session.contacts._outbound_legacy_id(self.legacy_id)

    def get_wa_chat(self) -> whatsapp.Chat:
        return whatsapp.Chat(  # type:ignore[no-untyped-call]
            JID=self._wa_legacy_id(),
            IsGroup=False,
        )

    async def get_wa_actor(self, legacy_msg_id: str) -> whatsapp.Actor:
        carbon = self.session.message_is_carbon(self, legacy_msg_id)
        return whatsapp.Actor(  # type:ignore[no-untyped-call]
            JID=(
                self.session.contacts.user_legacy_id
                if carbon
                else self._wa_legacy_id()
            ),
            IsMe=carbon,
        )

    @property
    def phone(self) -> str:
        return self.legacy_id.split("@")[0]

    def _set_reply_to(self, xmpp_msg: XMPPMessage, wa_msg: whatsapp.Message) -> None:
        if not xmpp_msg.reply:
            return

        wa_msg.ReplyID = xmpp_msg.reply.msg_id
        if xmpp_msg.reply.fallback:
            wa_msg.ReplyBody = strip_quote_prefix(xmpp_msg.reply.fallback)
            wa_msg.Body = wa_msg.Body.lstrip()

        reply_target = xmpp_msg.reply.to
        # A private group reply retains the original WhatsApp group and participant.
        if reply_target is not None and hasattr(reply_target, "muc"):
            participant = reply_target
            wa_msg.OriginActor.IsMe = participant.is_user
            if participant.contact:
                wa_msg.OriginActor.JID = participant.contact.legacy_id
            participant_lid = (
                participant.occupant_id
                if participant.occupant_id and participant.occupant_id.endswith("@lid")
                else ""
            )
            wa_msg.OriginActor.LID = participant_lid + "" + participant.muc.legacy_id
            return

        if reply_target is None:
            wa_msg.OriginActor.IsMe = True
            wa_msg.OriginActor.JID = self.session.contacts.user_legacy_id
            return

        xmpp_msg.reply.to = cast(Contact, reply_target)
        wa_msg.OriginActor.JID = self._wa_legacy_id()

class Roster(LegacyRoster[Contact]):
    session: "Session"

    def _refresh_mexico_aliases(self, wa_contacts: list[whatsapp.Contact]) -> None:
        legacy_ids = {data.Actor.JID for data in wa_contacts if data.Actor.JID}
        aliases = {
            legacy_id: modern
            for legacy_id in legacy_ids
            if (modern := _modern_mexico_legacy_id(legacy_id)) is not None
            and modern in legacy_ids
        }
        self._mexico_aliases = aliases
        self._mexico_outbound_aliases = {
            modern: legacy_id for legacy_id, modern in aliases.items()
        }

    def _canonical_legacy_id(self, legacy_id: str) -> str:
        return getattr(self, "_mexico_aliases", {}).get(legacy_id, legacy_id)

    def _outbound_legacy_id(self, legacy_id: str) -> str:
        return getattr(self, "_mexico_outbound_aliases", {}).get(legacy_id, legacy_id)

    async def fill(self) -> AsyncIterator[Contact]:
        """
        Retrieve contacts from remote WhatsApp service, subscribing to their presence and adding to
        local roster.
        """
        # CAN nonblocking contact fill v1
        wa_contacts = await asyncio.to_thread(
            lambda: list(self.session.whatsapp.GetContacts(refresh=config.ALWAYS_SYNC_ROSTER))
        )
        self._refresh_mexico_aliases(wa_contacts)

        contacts_by_legacy_id: dict[str, whatsapp.Contact] = {}
        for wa_contact in wa_contacts:
            original_id = wa_contact.Actor.JID
            canonical_id = self._canonical_legacy_id(original_id)
            current = contacts_by_legacy_id.get(canonical_id)
            if current is None or original_id == canonical_id:
                contacts_by_legacy_id[canonical_id] = wa_contact

        for legacy_id, wa_contact in contacts_by_legacy_id.items():
            wa_contact.Actor.JID = legacy_id
            contact = await self.add_whatsapp_contact(wa_contact)
            if contact is not None:
                yield contact
        await asyncio.to_thread(self.session.whatsapp.SubscribeToPresences)

    async def add_whatsapp_contact(self, data: whatsapp.Contact) -> Contact | None:
        """
        Adds a WhatsApp contact to local roster, filling all required and optional information.
        """
        # Don't attempt to add ourselves to the roster.
        if self.user_legacy_id == data.Actor.JID:
            return None
        if not data.Actor.JID:
            return None
        data.Actor.JID = self._canonical_legacy_id(data.Actor.JID)
        contact = await self.by_legacy_id(data.Actor.JID)
        await contact.update_whatsapp_info(data)
        return contact

    async def by_legacy_id(self, legacy_id: str) -> Contact:
        return await super().by_legacy_id(self._canonical_legacy_id(legacy_id))

    async def legacy_id_to_jid_username(self, legacy_id: str) -> str:
        if "@" not in legacy_id:
            raise XMPPError("item-not-found", "Invalid contact ID, not a JID")
        return "+" + legacy_id[: legacy_id.find("@")]

    async def jid_username_to_legacy_id(self, jid_username: str) -> str:
        if jid_username.startswith("#"):
            raise XMPPError("item-not-found", "Invalid contact ID: group ID given")
        if not jid_username.startswith("+"):
            raise XMPPError("item-not-found", "Invalid contact ID, expected '+' prefix")
        legacy_id = jid_username.removeprefix("+") + "@" + whatsapp.DefaultUserServer
        return self._canonical_legacy_id(legacy_id)
