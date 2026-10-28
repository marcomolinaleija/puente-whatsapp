import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from slixmpp import JID, Iq, Message, Presence
from slixmpp.exceptions import XMPPError
from slixmpp.xmlstream import StanzaBase

from ...contact.roster import ContactIsUser
from ...util.types import (
    AnyMUC,
    AnyRecipient,
    AnySession,
    RecipientType,
)

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class Ignore(BaseException):
    pass


class DispatcherMixin:
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        self.xmpp = xmpp  # type:ignore[misc]

    async def _get_session(
        self,
        stanza: Message | Presence | Iq,
        timeout: int | None = 10,
        wait_for_ready: bool = True,
        logged: bool = False,
    ) -> AnySession:
        xmpp = self.xmpp
        if stanza.get_from().server == xmpp.boundjid.bare:
            log.debug("Ignoring echo")
            raise Ignore
        if (
            isinstance(stanza, Message)
            and stanza.get_type() == "chat"
            and stanza.get_to() == xmpp.boundjid.bare
        ):
            log.debug("Ignoring message to component")
            raise Ignore
        session = await self._get_session_from_jid(
            stanza.get_from(), timeout, wait_for_ready, logged
        )
        if isinstance(stanza, Message) and _ignore(session, stanza):
            raise Ignore
        return session

    async def _get_session_from_jid(
        self,
        jid: JID,
        timeout: int | None = 10,
        wait_for_ready: bool = True,
        logged: bool = False,
    ) -> AnySession:
        session = self.xmpp.get_session_from_jid(jid)
        if session is None:
            raise XMPPError("registration-required")
        if logged:
            session.raise_if_not_logged()
        if wait_for_ready:
            await session.wait_for_ready(timeout)
        return session

    async def get_muc_from_stanza(self, iq: Iq | Message | Presence) -> AnyMUC:
        ito = iq.get_to()
        if ito == self.xmpp.boundjid.bare:
            raise XMPPError("bad-request", text="This is only handled for MUCs")

        session = await self._get_session(iq, logged=True)
        muc = await session.bookmarks.by_jid(ito)
        return muc  # type:ignore[no-any-return]

    def _xmpp_msg_id_to_legacy(
        self,
        xmpp_id: str,
        recipient: AnyRecipient,
        origin: bool = False,
    ) -> str:
        with self.xmpp.store.session() as orm:
            sent = self.xmpp.store.id_map.get_legacy(
                orm, recipient.stored.id, xmpp_id, recipient.is_group, origin
            )
            if sent is not None:
                return sent

        return xmpp_id

    async def _get_recipient_and_thread(
        self, msg: Message
    ) -> tuple[AnyRecipient, str | None]:
        session = await self._get_session(msg)
        e: AnyRecipient = await get_recipient(session, msg)
        legacy_thread = await self._xmpp_to_legacy_thread(session, msg, e)
        return e, legacy_thread

    async def _xmpp_to_legacy_thread(
        self, session: AnySession, msg: Message, recipient: RecipientType
    ) -> str | None:
        xmpp_thread = msg["thread"]
        if not xmpp_thread:
            return None

        if session.MESSAGE_IDS_ARE_THREAD_IDS:
            return self._xmpp_msg_id_to_legacy(xmpp_thread, recipient)

        with session.xmpp.store.session() as orm:
            legacy_thread_str = session.xmpp.store.id_map.get_thread(
                orm, recipient.stored.id, xmpp_thread, recipient.is_group
            )
            if legacy_thread_str is not None:
                return legacy_thread_str
        async with session.thread_creation_lock:
            legacy_thread = await recipient.create_thread(xmpp_thread)
            with session.xmpp.store.session() as orm:
                session.xmpp.store.id_map.set_thread(
                    orm,
                    recipient.stored.id,
                    str(legacy_thread),
                    xmpp_thread,
                    recipient.is_group,
                )
                orm.commit()
            return legacy_thread


def _ignore(session: AnySession, msg: Message) -> bool:
    i = msg.get_id()
    if i.startswith("slidge-carbon-"):
        return True
    if i not in session.ignore_messages:
        return False
    session.log.debug("Ignored sent carbon: %s", i)
    session.ignore_messages.remove(i)
    return True


async def get_recipient(session: AnySession, m: Message) -> AnyRecipient:
    session.raise_if_not_logged()
    if m.get_type() == "groupchat":
        muc = await session.bookmarks.by_jid(m.get_to())
        r = m.get_from().resource
        if r not in muc.get_user_resources():
            session.create_task(muc.kick_resource(r))
            raise XMPPError("not-acceptable", "You are not connected to this chat")
        return muc  # type:ignore[no-any-return]
    else:
        return await session.contacts.by_jid(m.get_to())  # type:ignore[no-any-return]


SelfType = TypeVar("SelfType")
StanzaType = TypeVar("StanzaType", bound=StanzaBase)
HandlerType = Callable[[SelfType, StanzaType], Awaitable[None]]
P = ParamSpec("P")


def exceptions_to_xmpp_errors(cb: HandlerType[Any, Any]) -> HandlerType[Any, Any]:
    @wraps(cb)
    async def wrapped(self: SelfType, stanza: StanzaType) -> None:
        try:
            await cb(self, stanza)
        except Ignore:
            pass
        except XMPPError:
            raise
        except NotImplementedError:
            log.debug("NotImplementedError raised in %s", cb)
            raise XMPPError(
                "feature-not-implemented",
                f"{cb.__name__} is not implemented by the legacy module",
                clear=False,
            )
        except ContactIsUser:
            raise XMPPError(
                "bad-request", "Actions with your bridged self are not allowed."
            )
        except Exception as e:
            log.error(
                "Failed to handle incoming stanza: %s - %s", self, stanza, exc_info=e
            )
            raise XMPPError("internal-server-error", str(e))

    return wrapped


log = logging.getLogger(__name__)
