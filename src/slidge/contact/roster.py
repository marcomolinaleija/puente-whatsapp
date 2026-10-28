import asyncio
import logging
import warnings
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Generic

from slixmpp import JID
from slixmpp.exceptions import IqError, IqTimeout, XMPPError
from sqlalchemy.orm import Session
from sqlalchemy.orm import Session as OrmSession

from ..db.models import Contact, GatewayUser
from ..util import SubclassableOnce
from ..util.jid_escaping import EscapeMixin
from ..util.lock import NamedLockMixin
from ..util.types import AnySession, LegacyContactType
from ..util.util import timeit

if TYPE_CHECKING:
    pass


class ContactIsUser(Exception):
    pass


class LegacyRoster(
    Generic[LegacyContactType],
    NamedLockMixin,
    EscapeMixin,
    SubclassableOnce,
):
    """
    Virtual roster of a gateway user that allows to represent all
    of their contacts as singleton instances (if used properly and not too bugged).

    Every :class:`.BaseSession` instance will have its own :class:`.LegacyRoster` instance
    accessible via the :attr:`.BaseSession.contacts` attribute.

    Typically, you will mostly use the :meth:`.LegacyRoster.by_legacy_id` function to
    retrieve a contact instance.

    You might need to override :meth:`.LegacyRoster.legacy_id_to_jid_username` and/or
    :meth:`.LegacyRoster.jid_username_to_legacy_id` to incorporate some custom logic
    if you need some characters when translation JID user parts and legacy IDs.
    """

    _contact_cls: type[LegacyContactType]

    def __init__(self, session: AnySession) -> None:
        super().__init__()

        self.log = logging.getLogger(f"{session.user_jid.bare}:roster")
        self.user_legacy_id: str | None = None
        self.ready: asyncio.Future[bool] = session.xmpp.loop.create_future()

        self.session = session
        self.__filling = False

    @property
    def user(self) -> GatewayUser:
        return self.session.user

    def orm(self) -> Session:
        return self.session.xmpp.store.session()

    def from_store(self, stored: Contact) -> LegacyContactType:
        return self._contact_cls(self.session, stored=stored)

    def __repr__(self) -> str:
        return f"<Roster of {self.session.user_jid}>"

    def __iter__(self) -> Iterator[LegacyContactType]:
        with self.orm() as orm:
            contacts = orm.query(Contact).filter_by(user=self.user, updated=True).all()
        for stored in contacts:
            yield self.from_store(stored)

    def known_contacts(self, only_friends: bool = True) -> dict[str, LegacyContactType]:
        if only_friends:
            return {c.jid.bare: c for c in self if c.is_friend}
        return {c.jid.bare: c for c in self}

    async def by_jid(self, contact_jid: JID) -> LegacyContactType:
        # """
        # Retrieve a contact by their JID
        #
        # If the contact was not instantiated before, it will be created
        # using :meth:`slidge.LegacyRoster.jid_username_to_legacy_id` to infer their
        # legacy user ID.
        #
        # :param contact_jid:
        # :return:
        # """
        username = contact_jid.node
        if not username:
            raise XMPPError(
                "bad-request", "Contacts must have a local part in their JID"
            )
        contact_jid = JID(contact_jid.bare)
        async with self.lock(("username", username)):
            legacy_id = await self.jid_username_to_legacy_id(username)
            if legacy_id == self.user_legacy_id:
                raise ContactIsUser
            if self.get_lock(("legacy_id", legacy_id)):
                self.log.debug("Already updating %s via by_legacy_id()", contact_jid)
                return await self.by_legacy_id(legacy_id)

            with self.orm() as orm:
                stored = (
                    orm.query(Contact)
                    .filter_by(user=self.user, jid=contact_jid)
                    .one_or_none()
                )
            if stored is None:
                stored = Contact(
                    user_account_id=self.session.user_pk,
                    legacy_id=legacy_id,
                    jid=contact_jid,
                )
            return await self.__update_if_needed(stored)

    async def __update_if_needed(self, stored: Contact) -> LegacyContactType:
        contact = self.from_store(stored)
        if contact.stored.updated:
            return contact

        with contact.updating_info():
            await contact.update_info()
            if contact.is_friend and not self.__filling:
                await contact.add_to_roster()

        if contact.cached_presence is not None:
            contact._store_last_presence(contact.cached_presence)
        return contact

    def by_jid_only_if_exists(self, contact_jid: JID) -> LegacyContactType | None:
        with self.orm() as orm:
            stored = (
                orm.query(Contact)
                .filter_by(user=self.user, jid=contact_jid)
                .one_or_none()
            )
            if stored is not None and stored.updated:
                return self.from_store(stored)
        return None

    @timeit
    async def by_legacy_id(self, /, legacy_id: str) -> LegacyContactType:
        """
        Retrieve a contact by their legacy_id

        If the contact was not instantiated before, it will be created
        using :meth:`slidge.LegacyRoster.legacy_id_to_jid_username` to infer their
        legacy user ID.

        :param legacy_id:
        :return:
        """
        if legacy_id == self.user_legacy_id:
            raise ContactIsUser
        async with self.lock(("legacy_id", legacy_id)):
            username = await self.legacy_id_to_jid_username(legacy_id)
            if self.get_lock(("username", username)):
                self.log.debug("Already updating %s via by_jid()", username)

                return await self.by_jid(
                    JID(username + "@" + self.session.xmpp.boundjid.bare)
                )

            with self.orm() as orm:
                stored = (
                    orm.query(Contact)
                    .filter_by(user=self.user, legacy_id=str(legacy_id))
                    .one_or_none()
                )
            if stored is None:
                stored = Contact(
                    user_account_id=self.session.user_pk,
                    legacy_id=str(legacy_id),
                    jid=JID(f"{username}@{self.session.xmpp.boundjid.bare}"),
                )
            return await self.__update_if_needed(stored)

    @timeit
    async def _fill(self, orm: OrmSession) -> None:
        try:
            if hasattr(self.session.xmpp, "TEST_MODE"):
                # dirty hack to avoid mocking xmpp server replies to this
                # during tests
                raise PermissionError
            iq = await self.session.xmpp["xep_0356"].get_roster(
                self.session.user_jid.bare
            )
            user_roster = iq["roster"]["items"]
        except (PermissionError, IqError, IqTimeout):
            user_roster = None

        self.__filling = True
        async for contact in self.fill():
            if user_roster is None:
                continue
            item = contact.get_roster_item()
            old = user_roster.get(contact.jid.bare)
            if old is not None and all(
                old[k] == item[contact.jid.bare].get(k)
                for k in ("subscription", "groups", "name")
            ):
                self.log.debug("No need to update roster")
                continue
            self.log.debug("Updating roster")
            if not contact.is_friend:
                continue
            if not self.session.user.preferences.get("roster_push", True):
                continue
            try:
                await self.session.xmpp["xep_0356"].set_roster(
                    self.session.user_jid.bare,
                    item,
                )
            except (PermissionError, IqError, IqTimeout) as e:
                warnings.warn(f"Could not add to roster: {e}")
            else:
                contact.added_to_roster = True
                contact.send_last_presence(force=True)
        orm.commit()
        self.__filling = False

    async def fill(self) -> AsyncIterator[LegacyContactType]:
        """
        Populate slidge's "virtual roster".

        This should yield contacts that are meant to be added to the user's
        roster, typically by using ``await self.by_legacy_id(contact_id)``.
        Setting the contact nicknames, avatar, etc. should be in
        :meth:`LegacyContact.update_info()`

        It's not mandatory to override this method, but it is recommended way
        to populate "friends" of the user. Calling
        ``await (await self.by_legacy_id(contact_id)).add_to_roster()``
        accomplishes the same thing, but doing it in here allows to batch
        DB queries and is better performance-wise.

        """
        return
        yield


log = logging.getLogger(__name__)
