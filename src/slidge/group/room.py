import hashlib
import json
import logging
import re
import string
import uuid
import warnings
from asyncio import Lock, sleep as _can_inbound_yield
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from copy import copy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeAlias, overload

import sqlalchemy as sa
from slixmpp import JID, Iq, Message, Presence
from slixmpp.exceptions import IqError, IqTimeout, XMPPError
from slixmpp.plugins.xep_0004.stanza.form import Form
from slixmpp.plugins.xep_0045.stanza import MUCUserItem
from slixmpp.plugins.xep_0060.stanza import Item
from slixmpp.plugins.xep_0082 import parse as str_to_datetime
from slixmpp.plugins.xep_0469.stanza import NS as PINNING_NS
from slixmpp.plugins.xep_0492.stanza import NS as NOTIFY_NS
from slixmpp.plugins.xep_0492.stanza import WhenLiteral
from slixmpp.xmlstream import ET
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.exc import DetachedInstanceError

from ..contact.contact import LegacyContact
from ..contact.roster import ContactIsUser
from ..core.mixins.avatar import AvatarMixin
from ..core.mixins.disco import ChatterDiscoMixin
from ..core.mixins.recipient import RecipientMixin
from ..db.models import Participant, Room
from ..util.archive_msg import HistoryMessage
from ..util.jid_escaping import unescape_node
from ..util.types import (
    AnyParticipant,
    AnySession,
    HoleBound,
    LegacyParticipantType,
    Mention,
    MucAffiliation,
    MucType,
)
from ..util.util import SubclassableOnce, timeit
from .archive import MessageArchive
from .participant import LegacyParticipant, escape_nickname

if TYPE_CHECKING:
    from ..command.base import MUCCommand
    from ..db.avatar import CachedAvatar

ADMIN_NS = "http://jabber.org/protocol/muc#admin"

SubjectSetterType: TypeAlias = "str | None | LegacyContact | AnyParticipant"


class LegacyMUC(
    Generic[LegacyParticipantType],
    AvatarMixin,
    ChatterDiscoMixin,
    RecipientMixin,
    SubclassableOnce,
):
    """
    A room, a.k.a. a Multi-User Chat.

    MUC instances are obtained by calling :py:meth:`slidge.group.bookmarks.LegacyBookmarks`
    on the user's :py:class:`slidge.core.session.BaseSession`.
    """

    max_history_fetch = 100

    is_group: Literal[True] = True

    DISCO_TYPE = "text"
    DISCO_CATEGORY = "conference"

    STABLE_ARCHIVE = False
    """
    Because legacy events like reactions, editions, etc. don't all map to a stanza
    with a proper legacy ID, slidge usually cannot guarantee the stability of the archive
    across restarts.

    Set this to True if you know what you're doing, but realistically, this can't
    be set to True until archive is permanently stored on disk by slidge.

    This is just a flag on archive responses that most clients ignore anyway.
    """

    """
    Set this to true if the fill_participants() / fill_participants() design does not
    fit the legacy API, ie, no lazy loading of the participant list and history.
    """

    HAS_DESCRIPTION = True
    """
    Set this to false if the legacy network does not allow setting a description
    for the group. In this case the description field will not be present in the
    room configuration form.
    """

    HAS_SUBJECT = True
    """
    Set this to false if the legacy network does not allow setting a subject
    (sometimes also called topic) for the group. In this case, as a subject is
    recommended by :xep:`0045` ("SHALL"), the description (or the group name as
    ultimate fallback) will be used as the room subject.
    By setting this to false, an error will be returned when the :term:`User`
    tries to set the room subject.
    """

    archive: MessageArchive
    session: AnySession

    stored: Room

    commands: ClassVar[dict[str, "type[MUCCommand]"]] = {}  # type:ignore[type-arg]
    commands_chat: ClassVar[dict[str, "type[MUCCommand]"]] = {}  # type:ignore[type-arg]

    _participant_cls: type[LegacyParticipantType]

    is_participant: Literal[False] = False

    def __init__(self, session: AnySession, stored: Room) -> None:
        self.session = session
        self.xmpp = session.xmpp
        self.stored = stored
        self._set_logger()
        super().__init__()

        self.archive = MessageArchive(stored, self.xmpp.store)

    def pop_unread_xmpp_ids_up_to(self, horizon_xmpp_id: str) -> list[str]:
        """
        Return XMPP msg ids sent in this group up to a given XMPP msg id.

        Plugins have no reason to use this, but it is used by slidge core
        for legacy networks that need to mark *all* messages as read (most XMPP
        clients only send a read marker for the latest message).

        This has side effects: all messages up to the horizon XMPP id will be marked
        as read in the DB. If the horizon XMPP id is not found, all messages of this
        MUC will be marked as read.

        :param horizon_xmpp_id: The latest message
        :return: A list of XMPP ids if horizon_xmpp_id was not found
        """
        with self.xmpp.store.session() as orm:
            assert self.stored.id is not None
            ids = self.xmpp.store.mam.pop_unread_up_to(
                orm, self.stored.id, horizon_xmpp_id
            )
            orm.commit()
        return ids

    def participant_from_store(
        self, stored: Participant, contact: LegacyContact | None = None
    ) -> LegacyParticipantType:
        if contact is None and stored.contact is not None:
            contact = self.session.contacts.from_store(stored.contact)
        return self._participant_cls(self, stored=stored, contact=contact)

    @property
    def jid(self) -> JID:
        return self.stored.jid

    @jid.setter
    def jid(self, x: JID) -> None:
        # FIXME: without this, mypy yields
        #        "Cannot override writeable attribute with read-only property"
        #        But it does not happen for LegacyContact. WTF?
        raise RuntimeError

    @property
    def legacy_id(self) -> str:
        return self.stored.legacy_id

    @property
    def space_legacy_id(self) -> str:
        return self.stored.space.legacy_id

    @space_legacy_id.setter
    def space_legacy_id(self, legacy_id: str) -> None:
        if (
            self.stored
            and self.stored.space
            and self.stored.space.legacy_id == legacy_id
        ):
            return
        with self.orm(expire_on_commit=False) as orm:
            space = self.xmpp.store.spaces.add_or_get(orm, self.user_pk, str(legacy_id))
            self.stored.space = space
            if self._updating_info:
                with orm.no_autoflush:
                    self.stored = orm.merge(self.stored)
                return
            orm.add(self.stored)
            orm.commit()

    def orm(
        self,
        **kwargs: Any,  # noqa:ANN401
    ) -> OrmSession:
        return self.xmpp.store.session(**kwargs)

    @property
    def type(self) -> MucType:
        return self.stored.muc_type

    @type.setter
    def type(self, type_: MucType) -> None:
        if self.type == type_:
            return
        self.update_stored_attribute(muc_type=type_)

    @property
    def n_participants(self) -> int | None:
        return self.stored.n_participants

    @n_participants.setter
    def n_participants(self, n_participants: int | None) -> None:
        if self.stored.n_participants == n_participants:
            return
        self.update_stored_attribute(n_participants=n_participants)

    @property
    def user_jid(self) -> JID:
        return self.session.user_jid

    def _set_logger(self) -> None:
        self.log = logging.getLogger(f"{self.user_jid}:muc:{self}")

    def __repr__(self) -> str:
        return f"<MUC #{self.stored.id} '{self.name}' ({self.stored.legacy_id} - {self.jid.user})'>"

    @property
    def subject_date(self) -> datetime | None:
        if self.stored.subject_date is None:
            return None
        return self.stored.subject_date.replace(tzinfo=UTC)

    @subject_date.setter
    def subject_date(self, when: datetime | None) -> None:
        if self.subject_date == when:
            return
        self.update_stored_attribute(subject_date=when)

    def __send_configuration_change(self, codes: tuple[int, ...]) -> None:
        part = self.get_system_participant()
        part.send_configuration_change(codes)

    @property
    def user_nick(self) -> str:
        return (
            self.stored.user_nick
            or self.session.bookmarks.user_nick
            or self.user_jid.node
        )

    @user_nick.setter
    def user_nick(self, nick: str) -> None:
        if nick == self.user_nick:
            return
        self.update_stored_attribute(user_nick=nick)

    def add_user_resource(self, resource: str) -> None:
        stored_set = self.get_user_resources()
        if resource in stored_set:
            return
        stored_set.add(resource)
        self.update_stored_attribute(
            user_resources=(json.dumps(list(stored_set)) if stored_set else None)
        )

    def get_user_resources(self) -> set[str]:
        stored_str = self.stored.user_resources
        if stored_str is None:
            return set()
        return set(json.loads(stored_str))

    def remove_user_resource(self, resource: str) -> None:
        stored_set = self.get_user_resources()
        if resource not in stored_set:
            return
        stored_set.remove(resource)
        self.update_stored_attribute(
            user_resources=(json.dumps(list(stored_set)) if stored_set else None)
        )

    @asynccontextmanager
    async def lock(self, id_: str) -> AsyncIterator[None]:
        async with self.session.lock((self.legacy_id, id_)):
            yield

    def get_lock(self, id_: str) -> Lock | None:
        return self.session.get_lock((self.legacy_id, id_))

    async def __fill_participants(self) -> None:
        if self.participants_filled:
            return

        async with self.lock("fill participants"):
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                orm.add(self.stored)
                with orm.no_autoflush:
                    orm.refresh(self.stored, ["participants_filled"])
            if self.participants_filled:
                return
            parts: list[Participant] = []
            resources = set[str]()
            # During fill_participants(), self.get_participant*() methods may
            # return a participant with a conflicting nick/resource.
            user_found = False
            async for participant in self.fill_participants():
                if participant.is_user:
                    user_found = True
                if participant.stored.resource in resources:
                    self.log.debug(
                        "Participant '%s' was yielded more than once by fill_participants(), ignoring",
                        participant.stored.resource,
                    )
                    continue
                parts.append(participant.stored)
                resources.add(participant.stored.resource)

            if not user_found:
                participant = await self.get_user_participant()
                if participant.stored.resource in resources:
                    for p in parts:
                        if p.resource == participant.jid.resource:
                            p.is_user = True
                else:
                    parts.append(participant.stored)
                    resources.add(participant.stored.resource)

        with self.xmpp.store.session(expire_on_commit=False) as orm:
            orm.add(self.stored)
            # because self.fill_participants() is async, self.stored may be stale at
            # this point, and the only thing we want to update is the participant list
            # and the participant_filled attribute.
            with orm.no_autoflush:
                orm.refresh(self.stored, ["participants"])
                for part in parts:
                    orm.merge(part)
            self.stored.participants_filled = True
            orm.commit()

    async def get_participants(
        self, affiliation: MucAffiliation | None = None
    ) -> AsyncIterator[LegacyParticipantType]:
        await self.__fill_participants()
        with self.xmpp.store.session(expire_on_commit=False, autoflush=False) as orm:
            self.stored = orm.merge(self.stored)
            db_participants = self.stored.participants
        for db_participant in db_participants:
            if affiliation is not None and db_participant.affiliation != affiliation:
                continue
            yield self.participant_from_store(db_participant)

    async def __fill_history(self) -> None:
        async with self.lock("fill history"):
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                orm.add(self.stored)
                with orm.no_autoflush:
                    orm.refresh(self.stored, ["history_filled"])
            if self.stored.history_filled:
                self.log.debug("History has already been fetched.")
                return
            log.debug("Fetching history for %s", self)
            try:
                before, after = self.archive.get_hole_bounds()
                if before is not None:
                    before = before._replace(id=before.id)
                if after is not None:
                    after = after._replace(id=after.id)
                await self.backfill(before, after)
            except NotImplementedError:
                return
            except Exception as e:
                self.log.exception("Could not backfill", exc_info=e)

            self.stored.history_filled = True
            self.commit()

    def _get_disco_name(self) -> str | None:
        return self.name

    @property
    def name(self) -> str | None:
        return self.stored.name

    @name.setter
    def name(self, n: str | None) -> None:
        if self.name == n:
            return
        self.update_stored_attribute(name=n)
        self._set_logger()
        self.__send_configuration_change((104,))

    @property
    def description(self) -> str:
        return self.stored.description or ""

    @description.setter
    def description(self, d: str) -> None:
        if self.description == d:
            return
        self.update_stored_attribute(description=d)
        self.__send_configuration_change((104,))

    def on_presence_unavailable(self, p: Presence) -> None:
        pto = p.get_to()
        if pto.bare != self.jid.bare:
            return

        pfrom = p.get_from()
        if pfrom.bare != self.user_jid.bare:
            return
        if (resource := pfrom.resource) in self.get_user_resources():
            if pto.resource != self.user_nick:
                self.log.debug(
                    "Received 'leave group' request but with wrong nickname. %s", p
                )
            self.remove_user_resource(resource)
        else:
            self.log.debug(
                "Received 'leave group' request but resource was not listed. %s", p
            )

    async def update_info(self) -> None:
        """
        Fetch information about this group from the legacy network

        This is awaited on MUC instantiation, and should be overridden to
        update the attributes of the group chat, like title, subject, number
        of participants etc.

        To take advantage of the slidge avatar cache, you can check the .avatar
        property to retrieve the "legacy file ID" of the cached avatar. If there
        is no change, you should not call
        :py:meth:`slidge.core.mixins.avatar.AvatarMixin.set_avatar()` or
        attempt to modify
        the :attr:.avatar property.
        """
        raise NotImplementedError

    async def backfill(
        self,
        after: HoleBound | None = None,
        before: HoleBound | None = None,
    ) -> None:
        """
        Override this if the legacy network provide server-side group archives.

        In it, send history messages using ``self.get_participant(xxx).send_xxxx``,
        with the ``archive_only=True`` kwarg. This is only called once per slidge
        run for a given group.

        :param after: Fetch messages after this one.
            If ``None``, slidge's local archive was empty before start-up,
            ie, no history was ever fetched for this room since the user registered.
            It's up to gateway implementations to decide how far to fetch messages before
            the user registered.
            If not ``None``, slidge has some messages in this archive, and
            the gateway shall try to fetch history up to (and excluding) this message
            to avoid "holes" in the history of this group.
        :param before: Fetch messages before this one.
            If ``None``, the gateway shall fetch all messages up to the most recent one.
            If not ``None``, slidge has already archived some live messages
            it received during its lifetime, and there is no need to query the legacy
            network for any message after (and including) this one.
        """
        raise NotImplementedError

    async def fill_participants(self) -> AsyncIterator[LegacyParticipantType]:
        """
        This method should yield the list of all members of this group.

        Typically, use ``participant = self.get_participant()``, self.get_participant_by_contact(),
        of self.get_user_participant(), and update their affiliation, hats, etc.
        before yielding them.
        """
        return
        yield

    @property
    def subject(self) -> str:
        return self.stored.subject or ""

    @subject.setter
    def subject(self, s: str) -> None:
        if s == self.subject:
            return

        self.update_stored_attribute(subject=s)
        self.__get_subject_setter_participant().set_room_subject(
            s, None, self.subject_date, False
        )

    @property
    def is_anonymous(self) -> bool:
        return self.type == MucType.CHANNEL

    @property
    def subject_setter(self) -> str | None:
        return self.stored.subject_setter

    @subject_setter.setter
    def subject_setter(self, subject_setter: SubjectSetterType) -> None:
        if isinstance(subject_setter, LegacyContact):
            subject_setter = subject_setter.name
        elif isinstance(subject_setter, LegacyParticipant):
            subject_setter = subject_setter.nickname

        if subject_setter == self.subject_setter:
            return
        assert isinstance(subject_setter, str | None)
        self.update_stored_attribute(subject_setter=subject_setter)

    def __get_subject_setter_participant(self) -> AnyParticipant:
        if self.subject_setter is None:
            return self.get_system_participant()
        return self._participant_cls(
            self,
            Participant(nickname=self.subject_setter, occupant_id="subject-setter"),
        )

    def features(self) -> list[str]:
        features = [
            "http://jabber.org/protocol/muc",
            "http://jabber.org/protocol/muc#stable_id",
            "http://jabber.org/protocol/muc#self-ping-optimization",
            "urn:xmpp:mam:2",
            "urn:xmpp:mam:2#extended",
            "urn:xmpp:sid:0",
            "muc_persistent",
            "vcard-temp",
            "urn:xmpp:ping",
            "urn:xmpp:occupant-id:0",
            "jabber:iq:register",
            "http://jabber.org/protocol/commands",
            "urn:xmpp:muc:affiliations:1",
            self.xmpp.plugin["xep_0425"].stanza.NS,
        ]
        if self.type == MucType.GROUP:
            features.extend(["muc_membersonly", "muc_nonanonymous", "muc_hidden"])
        elif self.type == MucType.CHANNEL:
            features.extend(["muc_open", "muc_semianonymous", "muc_public"])
        elif self.type == MucType.CHANNEL_NON_ANONYMOUS:
            features.extend(["muc_open", "muc_nonanonymous", "muc_public"])

        try:  # oh boy, this sucks
            has_space = self.stored.space is not None
        except DetachedInstanceError:
            self.refresh()
            has_space = self.stored.space is not None

        if has_space:
            features.append("urn:xmpp:spaces:0")
        return features

    async def extended_features(self) -> list[Form]:
        is_group = self.type == MucType.GROUP

        form = self.xmpp.plugin["xep_0004"].make_form(ftype="result")

        form.add_field(
            "FORM_TYPE", "hidden", value="http://jabber.org/protocol/muc#roominfo"
        )
        form.add_field("muc#roomconfig_persistentroom", "boolean", value=True)
        form.add_field("muc#roomconfig_changesubject", "boolean", value=False)
        form.add_field("muc#maxhistoryfetch", value=str(self.max_history_fetch))
        form.add_field("muc#roominfo_subjectmod", "boolean", value=False)

        if self.stored.id is not None and self.participants_filled:
            with self.xmpp.store.session() as orm:
                n = orm.scalar(
                    sa.select(sa.func.count(Participant.id)).filter_by(
                        room_id=self.stored.id
                    )
                )
        else:
            n = self.n_participants
        if n is not None:
            form.add_field("muc#roominfo_occupants", value=str(n))

        if d := self.description:
            form.add_field("muc#roominfo_description", value=d)

        if s := self.subject:
            form.add_field("muc#roominfo_subject", value=s)

        if name := self.name:
            form.add_field("muc#roomconfig_roomname", value=name)

        if self._set_avatar_task is not None:
            await self._set_avatar_task
        avatar = self.get_avatar()
        if avatar and (h := avatar.id):
            form.add_field(
                "{http://modules.prosody.im/mod_vcard_muc}avatar#sha1", value=h
            )
            form.add_field("muc#roominfo_avatarhash", "text-multi", value=[h])

        form.add_field("muc#roomconfig_membersonly", "boolean", value=is_group)
        form.add_field(
            "muc#roomconfig_whois",
            "list-single",
            value="moderators" if self.is_anonymous else "anyone",
        )
        form.add_field("muc#roomconfig_publicroom", "boolean", value=not is_group)
        form.add_field("muc#roomconfig_allowpm", "boolean", value=False)

        r = [form]

        if reaction_form := await self.restricted_emoji_extended_feature():
            r.append(reaction_form)

        if self.stored.space is not None:
            node = await self.session.bookmarks.space_legacy_id_to_node(
                self.stored.space.legacy_id
            )
            iri = f"xmpp:{self.xmpp.boundjid.bare}?node={node}"
            form.add_field("muc#roominfo_pubsub", value=iri)
            space_form = self.xmpp.plugin["xep_0004"].make_form(ftype="result")
            space_form.add_field("FORM_TYPE", "hidden", value="urn:xmpp:spaces:0")
            space_form.add_field("parent", label="Space parent", value=iri)

        return r

    def shutdown(self) -> None:
        _, user_jid = escape_nickname(self.jid, self.user_nick)
        for user_full_jid in self.user_full_jids():
            presence = self.xmpp.make_presence(
                pfrom=user_jid, pto=user_full_jid, ptype="unavailable"
            )
            presence["muc"]["affiliation"] = "none"
            presence["muc"]["role"] = "none"
            presence["muc"]["status_codes"] = {110, 332}
            presence.send()

    def user_full_jids(self) -> Iterable[JID]:
        for r in self.get_user_resources():
            j = JID(self.user_jid)
            j.resource = r
            yield j

    @property
    def user_muc_jid(self) -> JID:
        _, user_muc_jid = escape_nickname(self.jid, self.user_nick)
        return user_muc_jid

    async def echo(self, msg: Message, legacy_msg_id: str | None = None) -> str:
        msg.set_from(self.user_muc_jid)
        if legacy_msg_id:
            msg["stanza_id"]["id"] = legacy_msg_id
        else:
            msg["stanza_id"]["id"] = str(uuid.uuid4())
        msg["stanza_id"]["by"] = self.jid

        user_part = await self.get_user_participant()
        msg["occupant-id"]["id"] = user_part.stored.occupant_id

        # CAN ordered nonblocking group echoes v1
        # Keep archive-before-send and per-room order across the new await.
        async with self.session.lock(("can-group-echo", str(self.jid))):
            await self.archive.add_async(msg, user_part)

            for user_full_jid in self.user_full_jids():
                self.log.debug("Echoing to %s", user_full_jid)
                msg = copy(msg)
                msg.set_to(user_full_jid)
                msg.send()

            return msg["stanza_id"]["id"]  # type:ignore[no-any-return]

    def _post_avatar_update(self, cached_avatar: "CachedAvatar | None") -> None:
        self.__send_configuration_change((104,))
        self._send_room_presence()

    def _send_room_presence(self, user_full_jid: JID | None = None) -> None:
        tos = self.user_full_jids() if user_full_jid is None else [user_full_jid]
        for to in tos:
            p = self.xmpp.make_presence(pfrom=self.jid, pto=to)
            if (avatar := self.get_avatar()) and (h := avatar.id):
                p["vcard_temp_update"]["photo"] = h
            else:
                p["vcard_temp_update"]["photo"] = ""
            p.send()

    @timeit
    async def join(self, join_presence: Presence) -> None:
        user_full_jid = join_presence.get_from()
        requested_nickname = join_presence.get_to().resource
        client_resource = user_full_jid.resource

        if client_resource in self.get_user_resources():
            self.log.debug("Received join from a resource that is already joined.")

        if not requested_nickname or not client_resource:
            raise XMPPError("jid-malformed", by=self.jid)

        self.add_user_resource(client_resource)

        self.log.debug(
            "Resource %s of %s wants to join room %s with nickname %s",
            client_resource,
            self.user_jid,
            self.legacy_id,
            requested_nickname,
        )

        user_nick = self.user_nick
        user_participant = None
        await self.__fill_participants()
        if "mav" in join_presence["muc_join"]:
            mav_until = await self.__get_mav()
            self.log.debug("client uses MUC affiliation versioning")
            if join_presence["muc_join"]["mav"]["since"] != mav_until:
                self.log.debug(
                    "client mav: %s vs our mav: %s",
                    join_presence["muc_join"]["mav"]["since"],
                    mav_until,
                )
                await self.__send_mav(user_full_jid, mav_until)
        else:
            mav_until = None
        async for participant in self.get_participants():
            if participant.is_user:
                user_participant = participant
                continue
            participant.send_initial_presence(full_jid=user_full_jid)

        if user_participant is None:
            user_participant = await self.get_user_participant()
            with self.xmpp.store.session() as orm:
                orm.add(self.stored)
                with orm.no_autoflush:
                    orm.refresh(self.stored, ["participants"])
        if not user_participant.is_user:
            self.log.warning("is_user flag not set on user_participant")
            user_participant.is_user = True
        user_participant.send_initial_presence(
            user_full_jid,
            presence_id=join_presence["id"],
            nick_change=user_nick != requested_nickname,
            mav_until=mav_until,
        )

        history_params = join_presence["muc_join"]["history"]
        maxchars = int_or_none(history_params["maxchars"])
        maxstanzas = int_or_none(history_params["maxstanzas"])
        seconds = int_or_none(history_params["seconds"])
        try:
            since = self.xmpp.plugin["xep_0082"].parse(history_params["since"])
        except ValueError:
            since = None
        if seconds is not None:
            since = datetime.now() - timedelta(seconds=seconds)
        if equals_zero(maxchars) or equals_zero(maxstanzas):
            log.debug("Joining client does not want any old-school MUC history-on-join")
        else:
            self.log.debug("Old school history fill")
            await self.__fill_history()
            await self.__old_school_history(
                user_full_jid,
                maxchars=maxchars,
                maxstanzas=maxstanzas,
                since=since,
            )
        if self.HAS_SUBJECT:
            subject = self.subject or ""
        else:
            subject = self.description or self.name or ""
        self.__get_subject_setter_participant().set_room_subject(
            subject,
            user_full_jid,
            self.subject_date,
        )
        if t := self._set_avatar_task:
            await t
        self._send_room_presence(user_full_jid)

    async def __get_mav(self) -> str:
        data = self.__get_mav_data()
        return self.__compute_mav_ver(data)

    def __get_mav_data(self) -> list[tuple[str, MucAffiliation]]:
        data: list[tuple[str, MucAffiliation]] = []
        with self.orm(expire_on_commit=False) as orm:
            self.stored = orm.merge(self.stored)
            for part in self.stored.participants:
                if not (part.is_user or part.contact):
                    continue
                if part.affiliation == "none":
                    continue
                data.append((self.__get_part_mav_id(part), part.affiliation))
        return data

    def __get_part_mav_id(self, part: "LegacyParticipantType | Participant") -> str:
        return str(self.user_jid if part.is_user else part.contact.legacy_id)  # type:ignore[union-attr] # ty:ignore[unresolved-attribute]

    def __compute_mav_ver(self, data: list[tuple[Any, MucAffiliation]]) -> str:
        self.log.debug("MAV data: %s", data)
        affs = []
        for id_, aff in data:
            if aff == "none":
                continue
            affs.append(f"{id_}\0{aff}".encode())
        affs.sort()
        return hashlib.sha256(b"\0".join(affs)).hexdigest()

    async def __send_mav(self, full_jid: JID, until: str) -> None:
        msg = self.xmpp.make_message(mto=full_jid, mfrom=self.jid)
        msg["muc"]["mav"]["until"] = until
        with self.orm(expire_on_commit=False) as orm:
            self.stored = orm.merge(self.stored)
            for part in self.stored.participants:
                if not (part.is_user or part.contact):
                    continue
                item = MUCUserItem()
                item["jid"] = self.user_jid if part.is_user else part.contact.jid.bare  # type:ignore[union-attr]
                item["affiliation"] = part.affiliation
                msg["muc"].append(item)
        msg.send()

    async def get_user_participant(
        self,
        *,
        fill_first: bool = False,
        store: bool = True,
        occupant_id: str | None = None,
    ) -> "LegacyParticipantType":
        """
        Get the participant representing the gateway user

        :param kwargs: additional parameters for the :class:`.Participant`
            construction (optional)
        :return:
        """
        # CAN: own identity survives phone changes; nicknames are not unique identities.
        if not occupant_id:
            occupant_id = getattr(self.session, "_can_own_occupant_id", None)
        if not occupant_id and self.stored.id is not None:
            with self.xmpp.store.session() as orm:
                own_ids = [row[0] for row in orm.query(Participant.occupant_id).filter_by(
                    room_id=self.stored.id, is_user=True
                ).all()]
            # Keep old aliases and archives untouched. Never choose an arbitrary old row.
            if len(own_ids) == 1:
                occupant_id = own_ids[0]
        occupant_id = occupant_id or "slidge-user"
        p = await self.get_participant(
            self.user_nick,
            is_user=True,
            fill_first=fill_first,
            store=store,
            occupant_id=occupant_id,
        )
        # An exact own occupant lookup may enrich an earlier anonymous row.
        if not p.is_user:
            p.is_user = True
        self.__store_participant(p)
        return p

    def __store_participant(self, p: "LegacyParticipantType") -> None:
        if self.get_lock("fill participants"):
            return
        try:
            p.commit()
        except IntegrityError as e:
            log.debug("Could not store participant: %r", e)
        self.stored = p.stored.room

    @overload
    async def get_participant(self, nickname: str) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(self, *, occupant_id: str) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(
        self, *, occupant_id: str, create: Literal[False]
    ) -> "LegacyParticipantType | None": ...

    @overload
    async def get_participant(
        self, *, occupant_id: str, create: Literal[True]
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(
        self, nickname: str, *, occupant_id: str
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(
        self, nickname: str, *, create: Literal[False]
    ) -> "LegacyParticipantType | None": ...

    @overload
    async def get_participant(
        self, nickname: str, *, create: Literal[True]
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(
        self,
        nickname: str,
        *,
        create: Literal[True],
        is_user: bool,
        fill_first: bool,
        store: bool,
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant(
        self,
        nickname: str,
        *,
        create: Literal[False],
        is_user: bool,
        fill_first: bool,
        store: bool,
    ) -> "LegacyParticipantType | None": ...

    @overload
    async def get_participant(
        self,
        nickname: str,
        *,
        create: bool,
        fill_first: bool,
    ) -> "LegacyParticipantType | None": ...

    @overload
    async def get_participant(
        self,
        nickname: str,
        *,
        is_user: Literal[True],
        fill_first: bool,
        store: bool,
        occupant_id: str | None = None,
    ) -> "LegacyParticipantType": ...

    async def get_participant(
        self,
        nickname: str | None = None,
        *,
        create: bool = True,
        is_user: bool = False,
        fill_first: bool = False,
        store: bool = True,
        occupant_id: str | None = None,
    ) -> "LegacyParticipantType | None":
        """
        Get a participant by their nickname.

        In non-anonymous groups, you probably want to use
        :meth:`.LegacyMUC.get_participant_by_contact` instead.

        :param nickname: Nickname of the participant (used as resource part in the MUC)
        :param create: By default, a participant is created if necessary. Set this to
            False to return None if participant was not created before.
        :param is_user: Whether this participant is the slidge user.
        :param fill_first: Ensure :meth:`.LegacyMUC.fill_participants()` has been called
            first (internal use by slidge, plugins should not need that)
        :param store: persistently store the user in the list of MUC participants
        :param occupant_id: optionally, specify the unique ID for this participant, cf
            xep:`0421`
        :return: A participant of this room.
        """
        if not any((nickname, occupant_id)):
            raise TypeError("You must specify either a nickname or an occupant ID")
        if fill_first:
            await self.__fill_participants()
        if self.stored.id is not None:
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                if occupant_id is not None:
                    stored = (
                        orm.query(Participant)
                        .filter(
                            Participant.room == self.stored,
                            Participant.occupant_id == occupant_id,
                        )
                        .one_or_none()
                    )
                elif nickname is not None:
                    stored = (
                        orm.query(Participant)
                        .filter(
                            Participant.room == self.stored,
                            (Participant.nickname == nickname)
                            | (Participant.resource == nickname),
                        )
                        .one_or_none()
                    )
                else:
                    raise RuntimeError("NEVER")
                if stored is not None:
                    if occupant_id and occupant_id != stored.occupant_id:
                        warnings.warn(
                            f"Occupant ID mismatch in get_participant(): {occupant_id} vs {stored.occupant_id}",
                        )
                    part = self.participant_from_store(stored)
                    if occupant_id and nickname and nickname != stored.nickname:
                        stored.nickname = nickname
                        orm.add(stored)
                        orm.commit()
                    return part

        if not create:
            return None

        if occupant_id is None:
            occupant_id = "slidge-user" if is_user else str(uuid.uuid4())

        if nickname is None:
            nickname = occupant_id

        with self.xmpp.store.session() as orm:
            if not self.xmpp.store.rooms.nick_available(orm, self.stored.id, nickname):
                nickname = f"{nickname} ({occupant_id})"
                if is_user:
                    self.user_nick = nickname

        p = self._participant_cls(
            self,
            Participant(
                room=self.stored,
                nickname=nickname or occupant_id,
                is_user=is_user,
                occupant_id=occupant_id,
            ),
        )
        if store:
            self.__store_participant(p)
        self.send_affiliation_change(p)
        return p

    def send_affiliation_change(
        self, part: "LegacyParticipantType", was: MucAffiliation = "none"
    ) -> None:
        # internal use by slidge
        if not self.participants_filled:
            return
        if self.get_lock("fill participants"):
            return
        if self.get_lock("fill history"):
            return
        if part.contact is None:
            return
        if part.is_system:
            return
        if was == part.affiliation:
            return
        system_part = self.get_system_participant()
        msg = system_part._make_message(mtype="normal")
        data = self.__get_mav_data()
        since_data = [
            (id_, was if id_ == self.__get_part_mav_id(part) else aff)
            for id_, aff in data
        ]
        if part.affiliation == "none":
            since_data.append((self.__get_part_mav_id(part), was))
        msg["muc"]["mav"]["since"] = self.__compute_mav_ver(since_data)
        msg["muc"]["mav"]["until"] = self.__compute_mav_ver(data)
        item = MUCUserItem()
        item["affiliation"] = part.affiliation
        item["jid"] = self.user_jid if part.is_user else part.contact.jid.bare
        msg["muc"].append(item)
        system_part._send(msg)

    def get_system_participant(self) -> "LegacyParticipantType":
        """
        Get a pseudo-participant, representing the room itself

        Can be useful for events that cannot be mapped to a participant,
        e.g. anonymous moderation events, or announces from the legacy
        service
        :return:
        """
        return self._participant_cls(
            self, Participant(occupant_id="room"), is_system=True
        )

    @overload
    async def get_participant_by_contact(
        self, c: "LegacyContact"
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant_by_contact(
        self, c: "LegacyContact", *, occupant_id: str | None = None
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant_by_contact(
        self,
        c: "LegacyContact",
        *,
        create: Literal[False],
        occupant_id: str | None,
    ) -> "LegacyParticipantType | None": ...

    @overload
    async def get_participant_by_contact(
        self,
        c: "LegacyContact",
        *,
        create: Literal[True],
        occupant_id: str | None,
    ) -> "LegacyParticipantType": ...

    async def get_participant_by_contact(
        self, c: LegacyContact, *, create: bool = True, occupant_id: str | None = None
    ) -> "LegacyParticipantType | None":
        """
        Get a non-anonymous participant.

        This is what should be used in non-anonymous groups ideally, to ensure
        that the Contact jid is associated to this participant

        :param c: The :class:`.LegacyContact` instance corresponding to this contact
        :param create: Creates the participant if it does not exist.
        :param occupant_id: Optionally, specify a unique occupant ID (:xep:`0421`) for
            this participant.
        :return:
        """
        await self.session.contacts.ready

        if self.stored.id is not None:
            # Attaching an anonymous participant must keep the merged room usable after commit.
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                self.stored = orm.merge(self.stored)
                stored = (
                    orm.query(Participant)
                    .filter_by(contact=c.stored, room=self.stored)
                    .one_or_none()
                )
                if stored is None:
                    if occupant_id is not None:
                        stored = (
                            orm.query(Participant)
                            .filter_by(
                                occupant_id=occupant_id,
                                room=self.stored,
                                contact_id=None,
                            )
                            .one_or_none()
                        )
                        if stored is not None:
                            self.log.debug(
                                "Updating the contact of a previously anonymous participant"
                            )
                            stored.contact_id = c.stored.id
                            orm.add(stored)
                            orm.commit()
                            return self.participant_from_store(stored=stored, contact=c)
                    if not create:
                        return None
                else:
                    if occupant_id and stored.occupant_id != occupant_id:
                        warnings.warn(
                            f"Occupant ID mismatch: {occupant_id} vs {stored.occupant_id}",
                        )
                    return self.participant_from_store(stored=stored, contact=c)

        nickname = c.name or unescape_node(c.jid.node)

        if self.stored.id is None:
            nick_available = True
        else:
            with self.xmpp.store.session() as orm:
                nick_available = self.xmpp.store.rooms.nick_available(
                    orm, self.stored.id, nickname
                )

        if not nick_available:
            self.log.debug("Nickname conflict")
            nickname = f"{nickname} ({c.jid.node})"
        p = self._participant_cls(
            self,
            Participant(
                nickname=nickname,
                room=self.stored,
                occupant_id=occupant_id or str(c.jid),
            ),
            contact=c,
        )

        self.__store_participant(p)
        # FIXME: this is not great but given the current design,
        #        during participants fill and history backfill we do not
        #        want to send presence, because we might :update affiliation
        #        and role afterwards.
        # We need a refactor of the MUC class… later™
        if (
            self.participants_filled
            and not self.get_lock("fill participants")
            and not self.get_lock("fill history")
        ):
            p.send_last_presence(force=True, no_cache_online=True)
            self.send_affiliation_change(p)
        return p

    @overload
    async def get_participant_by_legacy_id(
        self, legacy_id: str
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant_by_legacy_id(
        self,
        legacy_id: str,
        *,
        occupant_id: str | None,
        create: Literal[True],
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant_by_legacy_id(
        self,
        legacy_id: str,
        *,
        occupant_id: str | None,
    ) -> "LegacyParticipantType": ...

    @overload
    async def get_participant_by_legacy_id(
        self,
        legacy_id: str,
        *,
        occupant_id: str | None,
        create: Literal[False],
    ) -> "LegacyParticipantType | None": ...

    async def get_participant_by_legacy_id(
        self,
        legacy_id: str,
        *,
        occupant_id: str | None = None,
        create: bool = True,
    ) -> "LegacyParticipantType":
        try:
            c = await self.session.contacts.by_legacy_id(legacy_id)
        except ContactIsUser:
            return await self.get_user_participant(occupant_id=occupant_id)
        return await self.get_participant_by_contact(  # type:ignore[call-overload,no-any-return]
            c, create=create, occupant_id=occupant_id
        )

    def remove_participant(
        self,
        p: "LegacyParticipantType",
        kick: bool = False,
        ban: bool = False,
        reason: str | None = None,
    ) -> None:
        """
        Call this when a participant leaves the room

        :param p: The participant
        :param kick: Whether the participant left because they were kicked
        :param ban: Whether the participant left because they were banned
        :param reason: Optionally, a reason why the participant was removed.
        """
        self.log.debug("Removing participant: %s", p)
        if kick and ban:
            raise TypeError("Either kick or ban")
        if kick:
            codes = {307}
        elif ban:
            codes = {301}
        else:
            codes = None
        was = p.stored.affiliation
        p.stored.affiliation = "outcast" if ban else "none"
        p.stored.role = "none"
        presence = p._make_presence(ptype="unavailable", status_codes=codes)
        self.send_affiliation_change(p, was)
        if reason:
            presence["muc"].set_item_attr("reason", reason)
        p._send(presence)
        with self.orm() as orm:
            self.xmpp.store.participants.delete(orm, p.stored.id)
            orm.commit()

    def rename_participant(self, old_nickname: str, new_nickname: str) -> None:
        with self.xmpp.store.session() as orm:
            stored = (
                orm.query(Participant)
                .filter_by(room=self.stored, nickname=old_nickname)
                .one_or_none()
            )
            if stored is None:
                self.log.debug("Tried to rename a participant that we didn't know")
                return
            p = self.participant_from_store(stored)
            if p.nickname == old_nickname:
                p.nickname = new_nickname

    async def __old_school_history(
        self,
        full_jid: JID,
        maxchars: int | None = None,
        maxstanzas: int | None = None,
        seconds: int | None = None,
        since: datetime | None = None,
    ) -> None:
        """
        Old-style history join (internal slidge use)

        :param full_jid:
        :param maxchars:
        :param maxstanzas:
        :param seconds:
        :param since:
        :return:
        """
        # CAN bounded join history v2. This is delivery-on-join, not MAM storage.
        if maxchars == 0 or (maxstanzas is not None and maxstanzas <= 0):
            return
        if maxstanzas is None:
            maxstanzas = 50
        await _can_inbound_yield(0)
        emitted = 0
        if since is None:
            if seconds is None:
                start_date = datetime.now(tz=UTC) - timedelta(days=1)
            else:
                start_date = datetime.now(tz=UTC) - timedelta(seconds=seconds)
        else:
            start_date = since or datetime.now(tz=UTC) - timedelta(days=1)

        history = list(self.archive.get_all(
            start_date=start_date, end_date=None, last_page_n=maxstanzas
        ))
        for h_msg in history:
            msg = h_msg.stanza_component_ns
            msg["delay"]["stamp"] = h_msg.when
            msg["delay"]["from"] = self.jid
            msg.set_to(full_jid)
            self.xmpp.send(msg, False)
            emitted += 1
            if emitted % 5 == 0:
                await _can_inbound_yield(0)

    async def send_mam(self, iq: Iq) -> None:
        await self.__fill_history()

        form_values = iq["mam"]["form"].get_values()

        start_date = str_to_datetime_or_none(form_values.get("start"))
        end_date = str_to_datetime_or_none(form_values.get("end"))

        after_id = form_values.get("after-id")
        before_id = form_values.get("before-id")

        sender = form_values.get("with")

        ids = form_values.get("ids") or ()

        if max_str := iq["mam"]["rsm"]["max"]:
            try:
                max_results = int(max_str)
            except ValueError:
                max_results = None
        else:
            max_results = None

        after_id_rsm = iq["mam"]["rsm"]["after"]
        after_id = after_id_rsm or after_id

        before_rsm = iq["mam"]["rsm"]["before"]
        if before_rsm is not None and max_results is not None:
            last_page_n = max_results
            # - before_rsm is True means the empty element <before />, which means
            #   "last page in chronological order", cf https://xmpp.org/extensions/xep-0059.html#backwards
            # - before_rsm == "an ID" means <before>an ID</before>
            if before_rsm is not True:
                before_id = before_rsm
        else:
            last_page_n = None

        first = None
        last = None
        count = 0

        it = self.archive.get_all(
            start_date,
            end_date,
            before_id,
            after_id,
            ids,
            last_page_n,
            sender,
            bool(iq["mam"]["flip_page"]),
        )

        for history_msg in it:
            last = xmpp_id = history_msg.id
            if first is None:
                first = xmpp_id

            wrapper_msg = self.xmpp.make_message(mfrom=self.jid, mto=iq.get_from())
            wrapper_msg["mam_result"]["queryid"] = iq["mam"]["queryid"]
            wrapper_msg["mam_result"]["id"] = xmpp_id
            wrapper_msg["mam_result"].append(history_msg.forwarded())

            wrapper_msg.send()
            count += 1

            if max_results and count == max_results:
                break

        if max_results:
            try:
                next(it)
            except StopIteration:
                complete = True
            else:
                complete = False
        else:
            complete = True

        reply = iq.reply()
        if not self.STABLE_ARCHIVE:
            reply["mam_fin"]["stable"] = "false"
        if complete:
            reply["mam_fin"]["complete"] = "true"
        reply["mam_fin"]["rsm"]["first"] = first
        reply["mam_fin"]["rsm"]["last"] = last
        reply["mam_fin"]["rsm"]["count"] = str(count)
        reply.send()

    async def send_mam_metadata(self, iq: Iq) -> None:
        await self.__fill_history()
        await self.archive.send_metadata(iq)

    async def kick_resource(self, r: str) -> None:
        """
        Kick a XMPP client of the user. (slidge internal use)

        :param r: The resource to kick
        """
        pto = JID(self.user_jid)
        pto.resource = r
        p = self.xmpp.make_presence(
            pfrom=(await self.get_user_participant()).jid, pto=pto
        )
        p["type"] = "unavailable"
        p["muc"]["affiliation"] = "none"
        p["muc"]["role"] = "none"
        p["muc"]["status_codes"] = {110, 333}
        p.send()

    async def __get_bookmark(self) -> Item | None:
        item = Item()
        item["id"] = self.jid

        iq = Iq(stype="get", sfrom=self.user_jid, sto=self.user_jid)
        iq["pubsub"]["items"]["node"] = self.xmpp.plugin["xep_0402"].stanza.NS
        iq["pubsub"]["items"].append(item)

        try:
            ans = await self.xmpp.plugin["xep_0356"].send_privileged_iq(iq)
            if len(ans["pubsub"]["items"]) != 1:
                return None
            # this below creates the item if it wasn't here already
            # (slixmpp annoying magic)
            item = ans["pubsub"]["items"]["item"]
            item["id"] = self.jid
            return item  # type:ignore[no-any-return]
        except IqTimeout:
            warnings.warn(f"Cannot fetch bookmark for {self.user_jid}: timeout")
            return None
        except IqError as exc:
            warnings.warn(f"Cannot fetch bookmark for {self.user_jid}: {exc}")
            return None
        except PermissionError:
            warnings.warn(
                f"IQ privileges (XEP0356) not granted for {self.user_jid}, we cannot fetch the user bookmarks"
            )
            return None

    async def add_to_bookmarks(
        self,
        auto_join: bool = True,
        preserve: bool = True,
        pin: bool | None = None,
        notify: WhenLiteral | None = None,
    ) -> None:
        """
        Add the MUC to the user's XMPP bookmarks (:xep:`0402`)

        This requires that slidge has the IQ privileged set correctly
        on the XMPP server

        :param auto_join: whether XMPP clients should automatically join
            this MUC on startup. In theory, XMPP clients will receive
            a "push" notification when this is called, and they will
            join if they are online.
        :param preserve: preserve auto-join and bookmarks extensions
            set by the user outside slidge
        :param pin: Pin the group chat bookmark :xep:`0469`. Requires privileged entity.
            If set to ``None`` (default), the bookmark pinning status will be untouched.
        :param notify: Chat notification setting: :xep:`0492`. Requires privileged entity.
            If set to ``None`` (default), the setting will be untouched. Only the "global"
            notification setting is supported (ie, per client type is not possible).
        """
        existing = await self.__get_bookmark() if preserve else None
        user_resource = (await self.get_user_participant()).jid.resource

        new = Item()
        new["id"] = self.jid
        new["conference"]["nick"] = user_resource

        if existing is None:
            change = True
            new["conference"]["autojoin"] = auto_join
        else:
            change = existing["conference"]["nick"] != user_resource
            new["conference"]["autojoin"] = existing["conference"]["autojoin"]

        existing_extensions = (
            existing is not None and "extensions" in existing["conference"]
        )

        # preserving extensions we don't know about is a MUST
        if existing_extensions:
            assert existing is not None
            for el in existing["conference"]["extensions"].xml:
                if el.tag.startswith(f"{{{NOTIFY_NS}}}") and notify is not None:
                    continue
                if el.tag.startswith(f"{{{PINNING_NS}}}") and pin is not None:
                    continue
                new["conference"]["extensions"].append(el)

        if pin is not None:
            if existing_extensions:
                assert existing is not None
                existing_pin = (
                    existing["conference"]["extensions"].get_plugin(
                        "pinned", check=True
                    )
                    is not None
                )
                if existing_pin != pin:
                    change = True
            new["conference"]["extensions"]["pinned"] = pin

        if notify is not None:
            new["conference"]["extensions"].enable("notify")
            if existing_extensions:
                assert existing is not None
                existing_notify = existing["conference"]["extensions"].get_plugin(
                    "notify", check=True
                )
                if existing_notify is None:
                    change = True
                else:
                    if existing_notify.get_config() != notify:
                        change = True
                    for el in existing_notify:
                        new["conference"]["extensions"]["notify"].append(el)
            new["conference"]["extensions"]["notify"].configure(notify)

        if change:
            iq = Iq(stype="set", sfrom=self.user_jid, sto=self.user_jid)
            iq["pubsub"]["publish"]["node"] = self.xmpp.plugin["xep_0402"].stanza.NS
            iq["pubsub"]["publish"].append(new)

            iq["pubsub"]["publish_options"] = _BOOKMARKS_OPTIONS

            try:
                await self.xmpp.plugin["xep_0356"].send_privileged_iq(iq)
            except PermissionError:
                warnings.warn(
                    f"IQ privileges (XEP0356) not granted for {self.user_jid}, we cannot add bookmarks for the user"
                )
                # fallback by forcing invitation
                bookmark_add_fail = True
            except IqError as e:
                warnings.warn(
                    f"Something went wrong while trying to set the bookmarks: {e}"
                )
                # fallback by forcing invitation
                bookmark_add_fail = True
            else:
                bookmark_add_fail = False
        else:
            self.log.debug("Bookmark does not need updating.")
            return

        if bookmark_add_fail:
            self.session.send_gateway_invite(
                self,
                reason="This group could not be added automatically for you, most"
                "likely because this gateway is not configured as a privileged entity. "
                "Contact your administrator.",
            )
        elif existing is None and self.session.user.preferences.get(
            "always_invite_when_adding_bookmarks", True
        ):
            self.session.send_gateway_invite(
                self,
                reason="The gateway is configured to always send invitations for groups.",
            )

    async def on_avatar(self, data: bytes | None, mime: str | None) -> str | None:
        """
        Called when the user tries to set the avatar of the room from an XMPP
        client.

        If the set avatar operation is completed, should return a legacy image
        unique identifier. In this case the MUC avatar will be immediately
        updated on the XMPP side.

        If data is not None and this method returns None, then we assume that
        self.set_avatar() will be called elsewhere, eg triggered by a legacy
        room update event.

        :param data: image data or None if the user meant to remove the avatar
        :param mime: the mime type of the image. Since this is provided by
            the XMPP client, there is no guarantee that this is valid or
            correct.
        :return: A unique avatar identifier, which will trigger
            :py:meth:`slidge.group.room.LegacyMUC.set_avatar`. Alternatively, None, if
            :py:meth:`.LegacyMUC.set_avatar` is meant to be awaited somewhere else.
        """
        raise NotImplementedError

    async def on_set_config(
        self,
        name: str | None,
        description: str | None,
    ) -> None:
        """
        Triggered when the user requests changing the room configuration.
        Only title and description can be changed at the moment.

        The legacy module is responsible for updating :attr:`.title` and/or
        :attr:`LegacyMUC.description` of this instance.

        If :attr:`.HAS_DESCRIPTION` is set to False, description will always
        be ``None``.

        :param name: The new name of the room.
        :param description: The new description of the room.
        """
        raise NotImplementedError

    async def on_destroy_request(self, reason: str | None) -> None:
        """
        Triggered when the user requests room destruction.

        :param reason: Optionally, a reason for the destruction
        """
        raise NotImplementedError

    async def parse_mentions(
        self,
        text: str | None,
        message_xml: ET.Element | None = None,
    ) -> tuple[Mention, ...]:
        if not text:
            return ()
        with self.xmpp.store.session() as orm:
            await self.__fill_participants()
            orm.add(self.stored)
            participants = {
                p.nickname: p for p in self.stored.participants if len(p.nickname) > 1
            }

            if message_xml is not None:
                explicit_mentions = []
                for reference in message_xml.findall(".//{urn:xmpp:reference:0}reference"):
                    if reference.attrib.get("type") != "mention":
                        continue
                    try:
                        start = int(reference.attrib["begin"])
                        end = int(reference.attrib["end"])
                    except (KeyError, ValueError):
                        continue
                    if start < 0 or end <= start or end > len(text):
                        continue
                    mentioned_jid = reference.attrib.get("uri", "").removeprefix("xmpp:")
                    mentioned_jid = mentioned_jid.split("/", 1)[0]
                    for stored in self.stored.participants:
                        participant = self.participant_from_store(stored=stored)
                        if (
                            participant.contact
                            and str(participant.contact.jid.bare) == mentioned_jid
                        ):
                            explicit_mentions.append(
                                Mention(contact=participant.contact, start=start, end=end)
                            )
                            break
                if explicit_mentions:
                    return tuple(explicit_mentions)

            if len(participants) == 0:
                return ()

            result = []
            for match in re.finditer(
                "|".join(
                    sorted(
                        [re.escape(nick) for nick in participants],
                        key=lambda nick: len(nick),
                        reverse=True,
                    )
                ),
                text,
            ):
                span = match.span()
                nick = match.group()
                if span[0] != 0 and text[span[0] - 1] not in _WHITESPACE_OR_PUNCTUATION:
                    continue
                if span[1] == len(text) or text[span[1]] in _WHITESPACE_OR_PUNCTUATION:
                    participant = self.participant_from_store(
                        stored=participants[nick],
                    )
                    if contact := participant.contact:
                        result.append(
                            Mention(contact=contact, start=span[0], end=span[1])
                        )
        return tuple(result)

    async def on_set_subject(self, subject: str) -> None:
        """
        Triggered when the user requests changing the room subject.

        The legacy module is responsible for updating :attr:`.subject` of this
        instance.

        :param subject: The new subject for this room.
        """
        raise NotImplementedError

    async def on_set_thread_subject(self, thread: str, subject: str) -> None:
        """
        Triggered when the user requests changing the subject of a specific thread.

        :param thread: Legacy identifier of the thread
        :param subject: The new subject for this thread.
        """
        raise NotImplementedError

    async def on_moderate(self, legacy_msg_id: str, reason: str | None) -> None:
        """
        Triggered when the user attempts to retract a message that was sent in
        a MUC using :xep:`0425`.

        If retraction is not possible, this should raise the appropriate
        XMPPError with a human-readable message.

        NB: the legacy module is responsible for calling
        :func:`LegacyParticipant.moderate` when this is successful, because
        slidge will acknowledge the moderation IQ, but will not send the
        moderation message from the MUC automatically.

        :param legacy_msg_id: The legacy ID of the message to be retracted
        :param reason: Optionally, a reason for the moderation, given by the
            user-moderator.
        """
        raise NotImplementedError

    async def on_leave(self) -> None:
        """
        Triggered when the user leaves a group via the dedicated slidge command
        or the :xep:`0077` ``<remove />`` mechanism.

        This should be interpreted as definitely leaving the group.
        """
        raise NotImplementedError

    @property
    def participants_filled(self) -> bool:
        try:
            return self.stored.participants_filled
        except DetachedInstanceError:
            with self.orm(expire_on_commit=False) as orm:
                orm.add(self.stored)
                with orm.no_autoflush:
                    orm.refresh(self.stored, ["participants_filled"])
            return self.stored.participants_filled

    def get_archived_messages(self, msg_id: str) -> Iterator[HistoryMessage]:
        """
        Query the slidge archive for messages sent in this group

        :param msg_id: Message ID of the message in question. Can be either a legacy ID
            or an XMPP ID.
        :return: Iterator over messages. A single legacy ID can map to several messages,
            because of multi-attachment messages.
        """
        with self.xmpp.store.session() as orm:
            # CAN release archived reader v1: one message ID, not all history.
            messages = list(self.xmpp.store.mam.get_messages(
                orm, self.stored.id, ids=[str(msg_id)]
            ))
            messages = [HistoryMessage(stored.stanza) for stored in messages]
        yield from messages


def set_origin_id(msg: Message, origin_id: str) -> None:
    sub = ET.Element("{urn:xmpp:sid:0}origin-id")
    sub.attrib["id"] = origin_id
    msg.xml.append(sub)


def int_or_none(x: str) -> int | None:
    try:
        return int(x)
    except ValueError:
        return None


def equals_zero(x: int | None) -> bool:
    if x is None:
        return False
    else:
        return x == 0


def str_to_datetime_or_none(date: str | None) -> datetime | None:
    if date is None:
        return None
    try:
        return str_to_datetime(date)
    except ValueError:
        return None


def bookmarks_form() -> Form:
    form = Form()
    form["type"] = "submit"
    form.add_field(
        "FORM_TYPE",
        value="http://jabber.org/protocol/pubsub#publish-options",
        ftype="hidden",
    )
    form.add_field("pubsub#persist_items", value="1")
    form.add_field("pubsub#max_items", value="max")
    form.add_field("pubsub#send_last_published_item", value="never")
    form.add_field("pubsub#access_model", value="whitelist")
    return form


_BOOKMARKS_OPTIONS = bookmarks_form()
_WHITESPACE_OR_PUNCTUATION = string.whitespace + "!\"'(),.:;?@_"

log = logging.getLogger(__name__)
