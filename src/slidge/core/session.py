import abc
import asyncio
import logging
from asyncio.tasks import Task
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Generic, NamedTuple, Self, cast

import aiohttp
import sqlalchemy as sa
from slixmpp import JID, Iq, Message, Presence
from slixmpp.exceptions import XMPPError
from slixmpp.types import PresenceShows, ResourceDict

from slidge.db.meta import JSONSerializable

from ..command import SearchResult
from ..contact import LegacyContact
from ..db.models import Contact, GatewayUser
from ..util import SubclassableOnce
from ..util.lock import NamedLockMixin
from ..util.types import (
    AnyBookmarks,
    AnyMUC,
    AnyParticipant,
    AnyRoster,
    AnySession,
    LegacyContactType,
    PseudoPresenceShow,
)
from ..util.util import noop_coro

if TYPE_CHECKING:
    from .gateway import BaseGateway


class CachedPresence(NamedTuple):
    status: str | None
    show: str | None
    kwargs: dict[str, Any]


class BaseSession(
    Generic[LegacyContactType], NamedLockMixin, SubclassableOnce, abc.ABC
):
    """
    The session of a registered :term:`User`.

    Represents a gateway user logged in to the legacy network and performing actions.

    Will be instantiated automatically on slidge startup for each registered user,
    or upon registration for new (validated) users.

    Must be subclassed for a functional :term:`Legacy Module`.
    """

    """
    Since we cannot set the XMPP ID of messages sent by XMPP clients, we need to keep a mapping
    between XMPP IDs and legacy message IDs if we want to further refer to a message that was sent
    by the user. This also applies to 'carboned' messages, ie, messages sent by the user from
    the official client of a legacy network.
    """

    xmpp: "BaseGateway[Self]"
    """
    The gateway instance singleton. Use it for low-level XMPP calls or custom methods that are not
    session-specific.
    """

    MESSAGE_IDS_ARE_THREAD_IDS = False
    """
    Set this to True if the legacy service uses message IDs as thread IDs,
    eg Mattermost, where you can only 'create a thread' by replying to the message,
    in which case the message ID is also a thread ID (and all messages are potential
    threads).
    """
    SPECIAL_MSG_ID_PREFIX: str | None = None
    """
    If you set this, XMPP message IDs starting with this won't be converted to legacy ID,
    but passed as is to :meth:`LegacyContact.on_react`, and usual checks for emoji restriction won't be
    applied.
    This can be used to implement voting in polls in a hacky way.
    """

    _roster_cls: type[AnyRoster]
    _bookmarks_cls: type[AnyBookmarks]

    def __init__(self, user: GatewayUser) -> None:
        super().__init__()
        self.user = user
        """
        The :term:`slidge user <User>`.
        """
        self.log = logging.getLogger(user.jid.bare)

        self.ignore_messages = set[str]()

        self.contacts: AnyRoster = self._roster_cls(self)
        self.is_logging_in = False
        self._logged = False
        self.__reset_ready()

        self.bookmarks = self._bookmarks_cls(self)

        self.thread_creation_lock = asyncio.Lock()

        self.__cached_presence: CachedPresence | None = None

        self.__tasks = set[asyncio.Task[Any]]()

    @property
    def user_jid(self) -> JID:
        return self.user.jid

    @property
    def user_pk(self) -> int:
        return self.user.id

    @property
    def http(self) -> aiohttp.ClientSession:
        return self.xmpp.http

    def __remove_task(self, fut: Task[Any]) -> None:
        self.log.debug("Removing fut %s", fut)
        self.__tasks.remove(fut)

    def create_task(
        self, coro: Coroutine[Any, Any, Any], name: str | None = None
    ) -> asyncio.Task[Any]:
        task = self.xmpp.loop.create_task(coro, name=name)
        self.__tasks.add(task)
        self.log.debug("Creating task %s", task)
        task.add_done_callback(lambda _: self.__remove_task(task))
        return task

    def cancel_all_tasks(self) -> None:
        for task in self.__tasks:
            task.cancel()

    @abc.abstractmethod
    async def login(self) -> str | None:
        """
        Logs in the gateway user to the legacy network.

        Triggered when the gateway start and on user registration.
        It is recommended that this function returns once the user is logged in,
        so if you need to await forever (for instance to listen to incoming events),
        it's a good idea to wrap your listener in an asyncio.Task.

        :return: Optionally, a text to use as the gateway status, e.g., "Connected as 'dude@legacy.network'"
        """
        raise NotImplementedError

    async def logout(self) -> None:
        """
        Logs out the gateway user from the legacy network.

        Called on gateway shutdown.
        """
        raise NotImplementedError

    async def on_presence(
        self,
        resource: str,
        show: PseudoPresenceShow,
        status: str,
        resources: dict[str, ResourceDict],
        merged_resource: ResourceDict | None,
    ) -> None:
        """
        Called when the gateway component receives a presence, ie, when
        one of the user's clients goes online of offline, or changes its
        status.

        :param resource: The XMPP client identifier, arbitrary string.
        :param show: The presence ``<show>``, if available. If the resource is
            just 'available' without any ``<show>`` element, this is an empty
            str.
        :param status: A status message, like a deeply profound quote, eg,
            "Roses are red, violets are blue, [INSERT JOKE]".
        :param resources: A summary of all the resources for this user.
        :param merged_resource: A global presence for the user account,
            following rules described in :meth:`merge_resources`
        """
        raise NotImplementedError

    async def on_search(self, form_values: dict[str, str]) -> SearchResult | None:
        """
        Triggered when the user uses Jabber Search (:xep:`0055`) on the component

        Form values is a dict in which keys are defined in :attr:`.BaseGateway.SEARCH_FIELDS`

        :param form_values: search query, defined for a specific plugin by overriding
            in :attr:`.BaseGateway.SEARCH_FIELDS`
        :return:
        """
        raise NotImplementedError

    async def on_avatar(
        self,
        bytes_: bytes | None,
        hash_: str | None,
        type_: str | None,
        width: int | None,
        height: int | None,
    ) -> None:
        """
        Triggered when the user uses modifies their avatar via :xep:`0084`.

        :param bytes_: The data of the avatar. According to the spec, this
            should always be a PNG, but some implementations do not respect
            that. If `None` it means the user has unpublished their avatar.
        :param hash_: The SHA1 hash of the avatar data. This is an identifier of
            the avatar.
        :param type_: The MIME type of the avatar.
        :param width: The width of the avatar image.
        :param height: The height of the avatar image.
        """
        raise NotImplementedError

    async def on_create_group(
        self, name: str, contacts: list[LegacyContactType]
    ) -> str:
        """
        Triggered when the user request the creation of a group via the
        dedicated :term:`Command`.

        :param name: Name of the group
        :param contacts: list of contacts that should be members of the group
        """
        raise NotImplementedError

    async def on_leave_space(self, space_legacy_id: str) -> None:
        """
        Triggered when the user sends a request to leave a :xep:`0503` space.

        :param space_legacy_id: The legacy ID of the space to leave
        """
        raise NotImplementedError

    async def on_preferences(
        self, previous: dict[str, Any], new: dict[str, Any]
    ) -> None:
        """
        This is called when the user updates their preferences.

        Override this if you need set custom preferences field and need to trigger
        something when a preference has changed.
        """
        raise NotImplementedError

    def __reset_ready(self) -> None:
        self.ready = self.xmpp.loop.create_future()

    @property
    def logged(self) -> bool:
        return self._logged

    @logged.setter
    def logged(self, v: bool) -> None:
        self.is_logging_in = False
        self._logged = v
        if self.ready.done():
            if v:
                return
            self.__reset_ready()
            self.shutdown(logout=False)
            with self.xmpp.store.session() as orm:
                self.xmpp.store.mam.reset_source(orm)
                self.xmpp.store.rooms.reset_updated(orm)
                self.xmpp.store.contacts.reset_updated(orm)
                orm.commit()
        else:
            if v:
                self.ready.set_result(True)

    def __repr__(self) -> str:
        return f"<Session of {self.user_jid}>"

    def shutdown(self, logout: bool = True) -> asyncio.Task[None]:
        for m in self.bookmarks:
            m.shutdown()
        with self.xmpp.store.session() as orm:
            for jid in orm.execute(
                sa.select(Contact.jid).filter_by(user=self.user, is_friend=True)
            ).scalars():
                pres = self.xmpp.make_presence(
                    pfrom=jid,
                    pto=self.user_jid,
                    ptype="unavailable",
                    pstatus="Gateway has shut down.",
                )
                pres.send()
        if logout:
            return self.xmpp.loop.create_task(self.__logout())
        else:
            return self.xmpp.loop.create_task(noop_coro())

    async def __logout(self) -> None:
        try:
            await self.logout()
        except NotImplementedError:
            pass
        except KeyboardInterrupt:
            pass

    def raise_if_not_logged(self) -> None:
        if not self.logged:
            raise XMPPError(
                "internal-server-error",
                text="You are not logged to the legacy network",
            )

    @classmethod
    def _from_user_or_none(cls, user: GatewayUser | None) -> Self:
        if user is None:
            log.debug("user not found")
            raise XMPPError(text="User not found", condition="subscription-required")

        session = _sessions.get(user.jid.bare)
        if session is None:
            _sessions[user.jid.bare] = session = cls(user)
        assert isinstance(session, cls)
        return session

    @classmethod
    def from_user(cls, user: GatewayUser) -> Self:
        return cls._from_user_or_none(user)

    @classmethod
    def from_stanza(cls, s: Message | Iq | Presence) -> Self:
        # """
        # Get a user's :class:`.LegacySession` using the "from" field of a stanza
        #
        # Meant to be called from :class:`BaseGateway` only.
        #
        # :param s:
        # :return:
        # """
        return cls.from_jid(s.get_from())

    @classmethod
    def from_jid(cls, jid: JID) -> Self:
        # """
        # Get a user's :class:`.LegacySession` using its jid
        #
        # Meant to be called from :class:`BaseGateway` only.
        #
        # :param jid:
        # :return:
        # """
        session = _sessions.get(jid.bare)
        if session is not None:
            assert isinstance(session, cls)
            return session
        with cls.xmpp.store.session() as orm:
            user = orm.query(GatewayUser).filter_by(jid=jid.bare).one_or_none()
        return cls._from_user_or_none(user)

    @classmethod
    async def kill_by_jid(cls, jid: JID) -> None:
        # """
        # Terminate a user session.
        #
        # Meant to be called from :class:`BaseGateway` only.
        #
        # :param jid:
        # :return:
        # """
        log.debug("Killing session of %s", jid)
        for user_jid, session in _sessions.items():
            if user_jid == jid.bare:
                break
        else:
            log.debug("Did not find a session for %s", jid)
            return
        for c in session.contacts:
            c.unsubscribe()
        for m in session.bookmarks:
            m.shutdown()

        try:
            session = _sessions.pop(jid.bare)
        except KeyError:
            log.warning("User not found during unregistration")
            return

        session.cancel_all_tasks()

        await cls.xmpp.unregister(cast(Self, session))
        with cls.xmpp.store.session() as orm:
            orm.delete(session.user)
            orm.commit()

    def __ack(self, msg: Message) -> None:
        if not self.xmpp.PROPER_RECEIPTS:
            self.xmpp.delivery_receipt.ack(msg)

    def send_gateway_status(
        self,
        status: str | None = None,
        show: PresenceShows | None = None,
        **kwargs: Any,  # noqa
    ) -> None:
        """
        Send a presence from the gateway to the user.

        Can be used to indicate the user session status, ie "SMS code required", "connected", …

        :param status: A status message
        :param show: Presence stanza 'show' element. I suggest using "dnd" to show
            that the gateway is not fully functional
        """
        self.__cached_presence = CachedPresence(status, show, kwargs)
        self.xmpp.send_presence(
            pto=self.user_jid.bare, pstatus=status, pshow=show, **kwargs
        )

    def send_cached_presence(self, to: JID) -> None:
        if not self.__cached_presence:
            self.xmpp.send_presence(pto=to, ptype="unavailable")
            return
        self.xmpp.send_presence(
            pto=to,
            pstatus=self.__cached_presence.status,
            pshow=self.__cached_presence.show,
            **self.__cached_presence.kwargs,
        )

    def send_gateway_message(
        self,
        text: str,
        **msg_kwargs: Any,  # noqa
    ) -> None:
        """
        Send a message from the gateway component to the user.

        Can be used to indicate the user session status, ie "SMS code required", "connected", …

        :param text: A text
        """
        self.xmpp.send_text(text, mto=self.user_jid, **msg_kwargs)

    def send_gateway_invite(
        self,
        muc: AnyMUC,
        reason: str | None = None,
        password: str | None = None,
    ) -> None:
        """
        Send an invitation to join a MUC, emanating from the gateway component.

        :param muc:
        :param reason:
        :param password:
        """
        self.xmpp.invite_to(muc, reason=reason, password=password, mto=self.user_jid)

    async def input(self, text: str, **msg_kwargs: Any) -> str:  # noqa
        """
        Request user input via direct messages from the gateway component.

        Wraps call to :meth:`.BaseSession.input`

        :param text: The prompt to send to the user
        :param msg_kwargs: Extra attributes
        :return:
        """
        return await self.xmpp.input(self.user_jid, text, **msg_kwargs)

    async def send_qr(self, text: str) -> None:
        """
        Sends a QR code generated from 'text' via HTTP Upload and send the URL to
        ``self.user``

        :param text: Text to encode as a QR code
        """
        await self.xmpp.send_qr(text, mto=self.user_jid)

    async def get_contact_or_group_or_participant(
        self, jid: JID, create: bool = True
    ) -> "LegacyContact | AnyMUC | AnyParticipant | None":
        if (contact := self.contacts.by_jid_only_if_exists(jid)) is not None:
            return contact  # type:ignore[no-any-return]
        if (muc := self.bookmarks.by_jid_only_if_exists(JID(jid.bare))) is not None:
            return await self.__get_muc_or_participant(muc, jid)
        else:
            muc = None

        if not create:
            return None

        try:
            return await self.contacts.by_jid(jid)  # type:ignore[no-any-return]
        except XMPPError:
            if muc is None:
                try:
                    muc = await self.bookmarks.by_jid(jid)
                except XMPPError:
                    return None
            return await self.__get_muc_or_participant(muc, jid)

    @staticmethod
    async def __get_muc_or_participant(
        muc: AnyMUC, jid: JID
    ) -> "AnyMUC | AnyParticipant | None":
        if nick := jid.resource:
            return await muc.get_participant(nick, create=False, fill_first=True)
        return muc

    async def wait_for_ready(self, timeout: int | float | None = 10) -> None:
        # """
        # Wait until session, contacts and bookmarks are ready
        #
        # (slidge internal use)
        #
        # :param timeout:
        # :return:
        # """
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), timeout)
            await asyncio.wait_for(asyncio.shield(self.contacts.ready), timeout)
            await asyncio.wait_for(asyncio.shield(self.bookmarks.ready), timeout)
        except TimeoutError:
            raise XMPPError(
                "recipient-unavailable",
                "Legacy session is not fully initialized, retry later",
            )

    def legacy_module_data_update(self, data: JSONSerializable) -> None:
        user = self.user
        user.legacy_module_data.update(data)
        self.xmpp.store.users.update(user)

    def legacy_module_data_set(self, data: JSONSerializable) -> None:
        user = self.user
        user.legacy_module_data = data
        self.xmpp.store.users.update(user)

    def legacy_module_data_clear(self) -> None:
        user = self.user
        user.legacy_module_data.clear()
        self.xmpp.store.users.update(user)


# keys = user.jid.bare
_sessions: dict[str, AnySession] = {}
log = logging.getLogger(__name__)
