from abc import ABC
from typing import TYPE_CHECKING, Any

from slixmpp import JID

from ...util.types import AnySession, MessageOrPresenceTypeVar

if TYPE_CHECKING:
    from ...util.types import AnyGateway


class Base(ABC):
    session: AnySession = NotImplemented
    xmpp: "AnyGateway" = NotImplemented

    jid: JID = NotImplemented
    name: str | None = NotImplemented

    @property
    def user_jid(self) -> JID:
        return self.session.user_jid

    @property
    def user_pk(self) -> int:
        return self.session.user_pk


class BaseSender(Base):
    is_participant: bool = NotImplemented

    def _send(
        self,
        stanza: MessageOrPresenceTypeVar,
        **send_kwargs: Any,  # noqa:ANN401
    ) -> MessageOrPresenceTypeVar:
        raise NotImplementedError
