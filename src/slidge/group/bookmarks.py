import abc
import logging
from collections.abc import Iterator
from typing import Any, Generic, Literal, overload

from slixmpp import JID
from slixmpp.exceptions import XMPPError
from sqlalchemy.orm import Session as OrmSession

from slidge.contact import LegacyContact

from ..db.models import Contact, Room, Space
from ..util import SubclassableOnce
from ..util.jid_escaping import EscapeMixin
from ..util.lock import NamedLockMixin
from ..util.types import (
    AnyMUC,
    AnySession,
    LegacyMUCType,
    SpaceMetadata,
)


class LegacyBookmarks(
    Generic[LegacyMUCType],
    EscapeMixin,
    NamedLockMixin,
    SubclassableOnce,
):
    """
    This is instantiated once per :class:`~slidge.BaseSession`
    """

    _muc_cls: type[LegacyMUCType]

    def __init__(self, session: AnySession) -> None:
        self.session = session
        self.xmpp = session.xmpp
        self.user_jid = session.user_jid

        self._user_nick: str = self.session.user_jid.node

        super().__init__()
        self.log = logging.getLogger(f"{self.user_jid.bare}:bookmarks")
        self.ready = self.session.xmpp.loop.create_future()
        if not self.xmpp.GROUPS:
            self.ready.set_result(True)

    @property
    def user_nick(self) -> str:
        return self._user_nick

    @user_nick.setter
    def user_nick(self, nick: str) -> None:
        self._user_nick = nick

    def orm(
        self,
        **kwargs: Any,  # noqa:ANN401
    ) -> OrmSession:
        return self.session.xmpp.store.session(**kwargs)

    def from_store(self, stored: Room) -> LegacyMUCType:
        return self._muc_cls(self.session, stored)

    def __iter__(self) -> Iterator[LegacyMUCType]:
        with self.xmpp.store.session() as orm:
            rooms = (
                orm.query(Room).filter_by(user=self.session.user, updated=True).all()
            )
        for stored in rooms:
            yield self.from_store(stored)

    def __repr__(self) -> str:
        return f"<Bookmarks of {self.user_jid}>"

    async def legacy_id_to_jid_local_part(self, legacy_id: str) -> str:
        return await self.legacy_id_to_jid_username(legacy_id)

    async def jid_local_part_to_legacy_id(self, local_part: str) -> str:
        return await self.jid_username_to_legacy_id(local_part)

    async def by_jid(self, jid: JID) -> LegacyMUCType:
        if jid.resource:
            jid = JID(jid.bare)
        async with self.lock(("bare", jid.bare)):
            legacy_id = await self.jid_local_part_to_legacy_id(jid.node)
            if self.get_lock(("legacy_id", legacy_id)):
                self.session.log.debug("Already updating %s via by_legacy_id()", jid)
                return await self.by_legacy_id(legacy_id)

            with self.session.xmpp.store.session() as orm:
                stored = (
                    orm.query(Room)
                    .filter_by(user_account_id=self.session.user_pk, jid=jid)
                    .one_or_none()
                )
            if stored is None:
                stored = Room(
                    user_account_id=self.session.user_pk,
                    jid=jid,
                    legacy_id=legacy_id,
                )
            return await self.__update_if_needed(stored)

    def by_jid_only_if_exists(self, jid: JID) -> LegacyMUCType | None:
        with self.xmpp.store.session(expire_on_commit=False) as orm:
            stored = (
                orm.query(Room).filter_by(user=self.session.user, jid=jid).one_or_none()
            )
            if stored is not None and stored.updated:
                return self.from_store(stored)
        return None

    @overload
    async def by_legacy_id(self, legacy_id: str) -> "LegacyMUCType": ...

    @overload
    async def by_legacy_id(
        self, legacy_id: str, create: Literal[False]
    ) -> "LegacyMUCType | None": ...

    @overload
    async def by_legacy_id(
        self, legacy_id: str, create: Literal[True]
    ) -> "LegacyMUCType": ...

    async def by_legacy_id(
        self, legacy_id: str, create: bool = False
    ) -> LegacyMUCType | None:
        async with self.lock(("legacy_id", legacy_id)):
            local = await self.legacy_id_to_jid_local_part(legacy_id)
            jid = JID(f"{local}@{self.xmpp.boundjid}")
            if self.get_lock(("bare", jid.bare)):
                self.session.log.debug("Already updating %s via by_jid()", jid)
                if create:
                    return await self.by_jid(jid)
                else:
                    return self.by_jid_only_if_exists(jid)

            with self.xmpp.store.session() as orm:
                stored = (
                    orm.query(Room)
                    .filter_by(
                        user_account_id=self.session.user_pk,
                        legacy_id=str(legacy_id),
                    )
                    .one_or_none()
                )
            if stored is None:
                stored = Room(
                    user_account_id=self.session.user_pk,
                    jid=jid,
                    legacy_id=str(legacy_id),
                )
            return await self.__update_if_needed(stored)

    async def __update_if_needed(self, stored: Room) -> LegacyMUCType:
        muc = self.from_store(stored)
        if muc.stored.updated:
            return muc

        with muc.updating_info():
            try:
                await muc.update_info()
            except NotImplementedError:
                pass
            except XMPPError:
                raise
            except Exception as e:
                raise XMPPError("internal-server-error", str(e))
        muc.archive.room = muc.stored
        if self.ready.done() and muc.stored.space_id:
            with self.orm() as orm:
                orm.add(muc.stored)
                self.xmpp.pubsub.broadcast_space(self.session, muc.stored.space)
        return muc

    async def fill(self) -> None:
        """
        Establish a user's known groups.

        This has to be overridden in plugins with group support and at the
        minimum, this should ``await self.by_legacy_id(group_id)`` for all
        the groups a user is part of.

        Slidge internals will call this on successful :meth:`BaseSession.login`

        """
        if self.xmpp.GROUPS:
            raise NotImplementedError(
                "The plugin advertised support for groups but"
                " LegacyBookmarks.fill() was not overridden."
            )

    async def remove(
        self,
        muc: AnyMUC,
        reason: str = "You left this group from the official client.",
        kick: bool = True,
    ) -> None:
        """
        Delete everything about a specific group.

        This should be called when the user leaves the group from the official
        app.

        :param muc: The MUC to remove.
        :param reason: Optionally, a reason why this group was removed.
        :param kick: Whether the user should be kicked from this group. Set this
            to False in case you do this somewhere else in your code, eg, on
            receiving the confirmation that the group was deleted.
        """
        if kick:
            user_participant = await muc.get_user_participant()
            user_participant.kick(reason)
        with self.xmpp.store.session() as orm:
            orm.add(muc.stored)
            orm.refresh(muc.stored)
            orm.delete(muc.stored)
            orm.commit()

    async def update_space_if_needed(self, space: Space) -> Space:
        async with self.lock(("space", space.legacy_id)):
            with self.orm() as orm:
                orm.add(space)
                orm.refresh(space)
                if space.updated:
                    return space
            meta: SpaceMetadata = await self.fetch_space_metadata(space.legacy_id)
        return await self.__update_space_metadata(space, meta)

    async def __update_space_metadata(self, space: Space, meta: SpaceMetadata) -> Space:
        creator = (
            await self.__get_stored_contact(meta.creator_legacy_id)
            if meta.creator_legacy_id
            else None
        )
        owners: list[Contact] = []
        for legacy_id in set(meta.owner_legacy_ids):
            if legacy_id == meta.creator_legacy_id:
                # We don't want to fetch any contact twice here to avoid:
                # Can't attach instance <Contact at xxx>; another instance [...] is already present in this session.
                continue
            owner = await self.__get_stored_contact(legacy_id)
            if owner is not None:
                owners.append(owner)
        if (
            creator is not None
            and meta.creator_legacy_id
            and meta.creator_legacy_id in meta.owner_legacy_ids
        ):
            owners.append(creator)
        with self.orm(expire_on_commit=False) as orm:
            if creator is not None:
                creator = orm.merge(creator)
            owners = [orm.merge(owner) for owner in owners]
            space = orm.merge(space)
            space.name = meta.name or space.legacy_id
            space.creator = creator
            space.owners = owners
            space.description = meta.description
            space.member_count = meta.member_count
            space.updated = True
            orm.commit()
        return space

    async def __get_stored_contact(self, legacy_id_str: str) -> Contact | None:
        try:
            contact: LegacyContact = await self.session.contacts.by_legacy_id(
                legacy_id_str
            )
        except Exception as e:
            self.log.warning("Could not get contact: %r", e)
            return None
        return contact.stored

    @abc.abstractmethod
    async def fetch_space_metadata(self, legacy_id: str) -> SpaceMetadata:
        """
        Fetch metadata associated to a space.

        This is called once per slidge runtime. It should return metadata
        associated to the space identified by its ``legacy_id``.
        If there are updates to this metata, they should be communicated to
        slidge by calling :func:`LegacyBookmarks.update_space_metadata`.

        :param legacy_id: Identifier of the space.

        :return: Metadata associated to the space.
        """
        raise NotImplementedError

    async def update_spaces_info(self) -> None:
        with self.orm() as orm:
            spaces = self.session.xmpp.store.spaces.get_unupdated(
                orm, self.session.user_pk
            )
        for space in spaces:
            await self.update_space_if_needed(space)

    async def space_legacy_id_to_node(self, legacy_id: str) -> str:
        return str(legacy_id)

    async def space_node_to_legacy_id(self, node: str) -> str:
        return node

    async def update_space_metadata(
        self,
        legacy_id: str,
        metadata: SpaceMetadata,
    ) -> None:
        """
        Updates metadata associated to a space.

        :param legacy_id: Identifier of the space.
        :param name: Metadata associated to this space.
        """
        with self.orm(expire_on_commit=False) as orm:
            space = self.session.xmpp.store.spaces.add_or_get(
                orm,
                self.session.user_pk,
                str(legacy_id),
            )
        space = await self.__update_space_metadata(space, metadata)
        self.xmpp.pubsub.broadcast_space_metadata(
            self.session, space, await self.space_legacy_id_to_node(legacy_id)
        )
