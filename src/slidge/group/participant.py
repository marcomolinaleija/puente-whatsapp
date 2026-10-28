import logging
import string
import uuid
import warnings
from copy import copy
from datetime import datetime
from typing import TYPE_CHECKING, Any, Generic, Literal
from xml.etree import ElementTree as ET

import sqlalchemy as sa
from slixmpp import JID, InvalidJID, Message, Presence
from slixmpp.plugins.xep_0030.stanza.info import DiscoInfo
from slixmpp.plugins.xep_0045.stanza import MUCAdminItem
from slixmpp.plugins.xep_0492.stanza import Never
from slixmpp.types import MessageTypes, OptJid
from sqlalchemy.orm.exc import DetachedInstanceError

from ..core.mixins import ChatterDiscoMixin, MessageMixin, PresenceMixin
from ..core.mixins.db import DBMixin
from ..db.models import Participant
from ..util import SubclassableOnce, strip_illegal_chars
from ..util.types import (
    AnyMUC,
    CachedPresence,
    Hat,
    LegacyContactType,
    MessageOrPresenceTypeVar,
    MucAffiliation,
    MucRole,
)

if TYPE_CHECKING:
    from slidge.command.base import ContactCommand


def strip_non_printable(nickname: str) -> str:
    new = (
        "".join(x for x in nickname if x in string.printable)
        + f"-slidge-{hash(nickname)}"
    )
    warnings.warn(f"Could not use {nickname} as a nickname, using {new}")
    return new


class LegacyParticipant(
    Generic[LegacyContactType],
    PresenceMixin,
    MessageMixin,
    ChatterDiscoMixin,
    DBMixin,
    SubclassableOnce,
):
    """
    A legacy participant of a legacy group chat.
    """

    is_participant: Literal[True] = True

    mtype: MessageTypes = "groupchat"
    _can_send_carbon = False
    USE_STANZA_ID = True
    STRIP_SHORT_DELAY = False
    stored: Participant
    contact: LegacyContactType | None

    def __init__(
        self,
        muc: AnyMUC,
        stored: Participant,
        is_system: bool = False,
        contact: LegacyContactType | None = None,
    ) -> None:
        self.muc = muc
        self.session = muc.session
        self.xmpp = muc.session.xmpp
        self.is_system = is_system

        if contact is None and stored.contact is not None:
            contact = self.session.contacts.from_store(stored=stored.contact)
        if contact is not None and stored.contact is None:
            stored.contact = contact.stored

        self.stored = stored
        self.contact = contact

        super().__init__()

        if stored.resource is None:
            self.__update_resource(stored.nickname)

        self.log = logging.getLogger(f"{self.user_jid.bare}:{self.jid}")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LegacyParticipant) and self.jid == other.jid

    @property
    def is_user(self) -> bool:
        try:
            return self.stored.is_user
        except DetachedInstanceError:
            self.merge()
            return self.stored.is_user

    @is_user.setter
    def is_user(self, is_user: bool) -> None:
        with self.xmpp.store.session(expire_on_commit=True) as orm:
            orm.add(self.stored)
            self.stored.is_user = is_user
            orm.commit()

    @property
    def jid(self) -> JID:
        jid = JID(self.muc.jid)
        if self.stored.resource:
            jid.resource = self.stored.resource
        return jid

    @jid.setter
    def jid(self, x: JID) -> None:
        # FIXME: without this, mypy yields
        #        "Cannot override writeable attribute with read-only property"
        #        But it does not happen for LegacyContact. WTF?
        raise RuntimeError

    @property
    def commands(self) -> dict[str, "type[ContactCommand[Any]]"]:  # type:ignore[override]
        if self.contact is None:
            return {}
        else:
            return self.contact.commands

    def __should_commit(self) -> bool:
        if self.is_system:
            return False
        if self.muc.get_lock("fill participants"):
            return False
        return not self.muc.get_lock("fill history")

    def commit(self) -> None:
        if not self.__should_commit():
            return
        super().commit()

    @property
    def user_jid(self) -> JID:
        return self.session.user_jid

    def __repr__(self) -> str:
        return f"<Participant '{self.nickname}'/'{self.jid}' of '{self.muc}'>"

    @property
    def _presence_sent(self) -> bool:
        # we track if we already sent a presence for this participant.
        # if we didn't, we send it before the first message.
        # this way, event in plugins that don't map "user has joined" events,
        # we send a "join"-presence from the participant before the first message
        return self.stored.presence_sent

    @_presence_sent.setter
    def _presence_sent(self, val: bool) -> None:
        if self._presence_sent == val:
            return
        self.stored.presence_sent = val
        if not self.__should_commit():
            return
        with self.xmpp.store.session() as orm:
            orm.execute(
                sa.update(Participant)
                .where(Participant.id == self.stored.id)
                .values(presence_sent=val)
            )
            orm.commit()

    @property
    def nickname_no_illegal(self) -> str:
        return self.stored.nickname_no_illegal

    @property
    def affiliation(self) -> MucAffiliation:
        return self.stored.affiliation

    @affiliation.setter
    def affiliation(self, affiliation: MucAffiliation) -> None:
        if self.affiliation == affiliation:
            return
        was = self.stored.affiliation
        self.stored.affiliation = affiliation
        if not self.muc.participants_filled:
            return
        self.commit()
        if self.cached_presence is None or self.cached_presence.ptype == "unavailable":
            self.muc.send_affiliation_change(self, was)
        self.send_last_presence(force=True, no_cache_online=True)

    @property
    def role(self) -> MucRole:
        return self.stored.role

    @role.setter
    def role(self, role: MucRole) -> None:
        if self.role == role:
            return
        self.stored.role = role
        if not self.muc.participants_filled:
            return
        self.commit()
        if not self._presence_sent:
            return
        self.send_last_presence(force=True, no_cache_online=True)

    @property
    def hats(self) -> list[Hat]:
        return [Hat(*h) for h in self.stored.hats] if self.stored.hats else []

    def set_hats(self, hats: list[Hat]) -> None:
        if self.hats == hats:
            return
        self.stored.hats = hats
        if not self.muc.participants_filled:
            return
        self.commit()
        if not self._presence_sent:
            return
        self.send_last_presence(force=True, no_cache_online=True)

    def __update_resource(self, unescaped_nickname: str | None) -> None:
        if not unescaped_nickname:
            self.stored.resource = ""
            if self.is_system:
                self.stored.nickname_no_illegal = ""
            else:
                warnings.warn(
                    "Only the system participant is allowed to not have a nickname"
                )
                nickname = f"unnamed-{uuid.uuid4()}"
                self.stored.resource = self.stored.nickname_no_illegal = nickname
            return

        self.stored.nickname_no_illegal, jid = escape_nickname(
            self.muc.jid,
            unescaped_nickname,
        )
        self.stored.resource = jid.resource

    def send_configuration_change(self, codes: tuple[int, ...]) -> None:
        if not self.is_system:
            raise RuntimeError("This is only possible for the system participant")
        msg = self._make_message()
        msg["muc"]["status_codes"] = codes
        self._send(msg)

    @property
    def nickname(self) -> str:
        return self.stored.nickname

    @nickname.setter
    def nickname(self, new_nickname: str) -> None:
        old = self.nickname
        if new_nickname == old:
            return

        if self.muc.stored.id is not None:
            with self.xmpp.store.session() as orm:
                if not self.xmpp.store.rooms.nick_available(
                    orm, self.muc.stored.id, new_nickname
                ):
                    if self.contact is None:
                        new_nickname = f"{new_nickname} ({self.occupant_id})"
                    else:
                        new_nickname = f"{new_nickname} ({self.contact.legacy_id})"

        cache = getattr(self, "_last_presence", None)
        if cache:
            last_seen = cache.last_seen
            kwargs = cache.presence_kwargs
        else:
            last_seen = None
            kwargs = {}

        kwargs["status_codes"] = {303}

        p = self._make_presence(ptype="unavailable", last_seen=last_seen, **kwargs)
        # in this order so pfrom=old resource and we actually use the escaped nick
        # in the muc/item/nick element
        self.__update_resource(new_nickname)
        p["muc"]["item"]["nick"] = self.jid.resource
        self._send(p)

        self.stored.nickname = new_nickname
        self.commit()
        kwargs["status_codes"] = set()
        p = self._make_presence(ptype="available", last_seen=last_seen, **kwargs)
        self._send(p)

    def _make_presence(  # type:ignore[no-untyped-def]
        self,
        *,
        last_seen: datetime | None = None,
        status_codes: set[int] | None = None,
        user_full_jid: JID | None = None,
        **presence_kwargs,  # noqa type:ignore[no-untyped-def]
    ) -> Presence:
        p = super()._make_presence(last_seen=last_seen, **presence_kwargs)
        p["muc"]["affiliation"] = self.affiliation
        p["muc"]["role"] = self.role
        if self.hats:
            p["hats"].add_hats(self.hats)
        codes = status_codes or set()
        if self.is_user:
            codes.add(110)
        if not self.muc.is_anonymous and not self.is_system:
            if self.is_user:
                if user_full_jid:
                    p["muc"]["jid"] = user_full_jid
                else:
                    jid = JID(self.user_jid)
                    try:
                        jid.resource = next(iter(self.muc.get_user_resources()))
                    except StopIteration:
                        jid.resource = "pseudo-resource"
                    p["muc"]["jid"] = self.user_jid
                codes.add(100)
            elif self.contact:
                p["muc"]["jid"] = self.contact.jid
                if a := self.contact.get_avatar():
                    p["vcard_temp_update"]["photo"] = a.id
            else:
                warnings.warn(
                    f"Private group but no 1:1 JID associated to '{self}'",
                )
        if self.is_user and (hash_ := self.session.user.avatar_hash):
            p["vcard_temp_update"]["photo"] = hash_
        p["muc"]["status_codes"] = codes
        return p

    @property
    def DISCO_NAME(self) -> str:
        return self.nickname

    @DISCO_NAME.setter
    def DISCO_NAME(self, _: str) -> Never:
        raise RuntimeError

    def __send_presence_if_needed(
        self, stanza: Message | Presence, full_jid: JID, archive_only: bool
    ) -> None:
        if (
            archive_only
            or self.is_system
            or self.is_user
            or self._presence_sent
            or stanza["subject"]
        ):
            return
        if isinstance(stanza, Message):
            if "muc" in stanza:
                return
            self.send_initial_presence(full_jid)

    @property
    def occupant_id(self) -> str:
        return self.stored.occupant_id

    def _send(
        self,
        stanza: MessageOrPresenceTypeVar,
        full_jid: JID | None = None,
        archive_only: bool = False,
        legacy_msg_id: str | None = None,
        initial_presence: bool = False,
        **send_kwargs: Any,  # noqa:ANN401
    ) -> MessageOrPresenceTypeVar:
        if stanza.get_from().resource:
            stanza["occupant-id"]["id"] = self.occupant_id
        else:
            stanza["occupant-id"]["id"] = "room"
        self.__add_nick_element(stanza)
        if not self.is_user and isinstance(stanza, Presence):
            if stanza["type"] == "unavailable" and not self._presence_sent:
                return stanza
            if initial_presence:
                self._presence_sent = True
            else:
                self._presence_sent = True
        if full_jid:
            stanza["to"] = full_jid
            self.__send_presence_if_needed(stanza, full_jid, archive_only)
            if self.is_user:
                assert stanza.stream is not None
                stanza.stream.send(stanza, use_filters=False)
            else:
                stanza.send()
        else:
            if hasattr(self.muc, "archive") and isinstance(stanza, Message):
                self.muc.archive.add(stanza, self, archive_only, legacy_msg_id)
            if archive_only:
                return stanza
            for user_full_jid in self.muc.user_full_jids():
                stanza = copy(stanza)
                stanza["to"] = user_full_jid
                self.__send_presence_if_needed(stanza, user_full_jid, archive_only)
                stanza.send()
        return stanza

    def mucadmin_item(self) -> MUCAdminItem:
        item = MUCAdminItem()
        item["nick"] = self.nickname
        item["affiliation"] = self.affiliation
        item["role"] = self.role
        if not self.muc.is_anonymous:
            if self.is_user:
                item["jid"] = self.user_jid.bare
            elif self.contact:
                item["jid"] = self.contact.jid.bare
            else:
                warnings.warn(
                    (
                        f"Private group but no contact JID associated to {self.jid} in"
                        f" {self}"
                    ),
                )
        return item

    def __add_nick_element(self, stanza: Presence | Message) -> None:
        if (nick := self.nickname_no_illegal) != self.jid.resource:
            n = self.xmpp.plugin["xep_0172"].stanza.UserNick()
            n["nick"] = nick
            stanza.append(n)

    def _get_last_presence(self) -> CachedPresence | None:
        own = super()._get_last_presence()
        if own is None and self.contact:
            return self.contact._get_last_presence()
        return own

    def send_initial_presence(
        self,
        full_jid: JID,
        nick_change: bool = False,
        presence_id: str | None = None,
        mav_until: str | None = None,
    ) -> None:
        """
        Called when the user joins a MUC, as a mechanism
        to indicate to the joining XMPP client the list of "participants".

        Can be called this to trigger a "participant has joined the group" event.

        :param full_jid: Set this to only send to a specific user XMPP resource.
        :param nick_change: Used when the user joins and the MUC renames them (code 210)
        :param presence_id: set the presence ID. used internally by slidge
        """
        #  MUC status codes: https://xmpp.org/extensions/xep-0045.html#registrar-statuscodes
        codes = set()
        if nick_change:
            codes.add(210)

        if self.is_user:
            # the "initial presence" of the user has to be vanilla, as it is
            # a crucial part of the MUC join sequence for XMPP clients.
            kwargs = {}
        else:
            cache = self._get_last_presence()
            self.log.debug("Join muc, initial presence: %s", cache)
            if cache:
                ptype = cache.ptype
                if ptype == "unavailable":
                    return
                kwargs = dict(
                    last_seen=cache.last_seen, pstatus=cache.pstatus, pshow=cache.pshow
                )
            else:
                kwargs = {}
        p = self._make_presence(
            status_codes=codes,
            user_full_jid=full_jid,
            **kwargs,  # type:ignore
        )
        if presence_id:
            p["id"] = presence_id
        if self.is_user and mav_until is not None:
            p["muc"]["mav"]["until"] = mav_until
        self._send(p, full_jid, initial_presence=True)

    def leave(self) -> None:
        """
        Call this when the participant leaves the room
        """
        self.muc.remove_participant(self)

    def kick(self, reason: str | None = None) -> None:
        """
        Call this when the participant is kicked from the room
        """
        self.muc.remove_participant(self, kick=True, reason=reason)

    def ban(self, reason: str | None = None) -> None:
        """
        Call this when the participant is banned from the room
        """
        self.muc.remove_participant(self, ban=True, reason=reason)

    async def get_disco_info(
        self, jid: OptJid = None, node: str | None = None
    ) -> DiscoInfo:
        if self.contact is not None:
            return await self.contact.get_disco_info()
        return await super().get_disco_info()

    def moderate(self, legacy_msg_id: str, reason: str | None = None) -> None:
        for i in self._legacy_to_xmpp(legacy_msg_id):
            m = self.muc.get_system_participant()._make_message()
            m["retract"]["id"] = i
            if self.is_system:
                m["retract"].enable("moderated")
            else:
                m["retract"]["moderated"]["by"] = self.jid
                m["retract"]["moderated"]["occupant-id"]["id"] = self.occupant_id
            if reason:
                m["retract"]["reason"] = reason
            self._send(m)

    def set_room_subject(
        self,
        subject: str,
        full_jid: JID | None = None,
        when: datetime | None = None,
        update_muc: bool = True,
    ) -> None:
        if update_muc:
            self.muc._subject = subject  # type: ignore
            self.muc.subject_setter = self.nickname
            self.muc.subject_date = when

        msg = self._make_message()
        if when is not None:
            msg["delay"].set_stamp(when)
            msg["delay"]["from"] = self.muc.jid
        if subject:
            msg["subject"] = subject
        else:
            # may be simplified if slixmpp lets it do it more easily some day
            msg.xml.append(ET.Element(f"{{{msg.namespace}}}subject"))
        self._send(msg, full_jid)

    def set_thread_subject(
        self,
        thread: str,
        subject: str | None,
        when: datetime | None = None,
    ) -> None:
        msg = self._make_message()
        msg["thread"] = str(thread)
        if when is not None:
            msg["delay"].set_stamp(when)
            msg["delay"]["from"] = self.muc.jid
        if subject:
            msg["subject"] = subject
        else:
            # may be simplified if slixmpp lets it do it more easily some day
            msg.xml.append(ET.Element(f"{{{msg.namespace}}}subject"))
        self._send(msg)

    async def on_set_affiliation(
        self,
        affiliation: MucAffiliation,
        reason: str | None,
        nickname: str | None,
    ) -> None:
        """
        Triggered when the user requests changing the affiliation of a contact
        for this group.

        Examples: promotion them to moderator, ban (affiliation=outcast).

        :param contact: The contact whose affiliation change is requested
        :param affiliation: The new affiliation
        :param reason: A reason for this affiliation change
        :param nickname:
        """
        raise NotImplementedError

    async def on_kick(self, reason: str | None) -> None:
        """
        Triggered when the user requests changing the role of a contact
        to "none" for this group. Action commonly known as "kick".

        :param contact: Contact to be kicked
        :param reason: A reason for this kick
        """
        raise NotImplementedError

    async def on_invitation(self, reason: str | None) -> None:
        """
        Triggered when the user invites this :term:`Contact <Legacy Contact>`
        to a legacy MUC via :xep:`0249`.

        The default implementation calls :meth:`LegacyMUC.on_set_affiliation`
        with the 'member' affiliation. Override if you want to customize this
        behaviour.

        :param muc: The group
        :param reason: Optionally, a reason
        """
        # part = await self.muc.get_participant_by_contact(self)
        await self.on_set_affiliation("member", reason, None)


def escape_nickname(muc_jid: JID, nickname: str) -> tuple[str, JID]:
    nickname = nickname_no_illegal = strip_illegal_chars(nickname).replace("\n", " | ")

    jid = JID(muc_jid)

    try:
        jid.resource = nickname
    except InvalidJID:
        nickname = nickname.encode("punycode").decode()
        try:
            jid.resource = nickname
        except InvalidJID:
            # at this point there still might be control chars
            jid.resource = strip_non_printable(nickname)

    return nickname_no_illegal, jid


log = logging.getLogger(__name__)
