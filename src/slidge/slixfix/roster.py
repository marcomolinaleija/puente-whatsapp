import logging
from collections.abc import Iterable
from typing import Any, Generic, TypeVar

from slixmpp import JID
from slixmpp.types import JidStr, OptJidStr, RosterState

GatewayType = TypeVar("GatewayType")


class YesSet(set):
    """
    A pseudo-set which always test True for membership
    """

    def __contains__(self, item: object) -> bool:
        log.debug("Test in")
        return True


class RosterBackend(Generic[GatewayType]):
    """
    A pseudo-roster for the gateway component.

    If a user is in the user store, this will behave as if the user is part of the
    roster with subscription "both", and "none" otherwise.

    This is rudimentary but the only sane way I could come up with so far.
    """

    xmpp: GatewayType

    def __init__(self, xmpp: GatewayType) -> None:
        self.xmpp = xmpp

    def entries(
        self, owner: OptJidStr, db_state: dict[str, Any] | None = None
    ) -> Iterable[str]:
        return YesSet()

    def save(
        self, owner: JidStr, jid: JidStr, state: RosterState, db_state: dict[str, Any]
    ) -> None:
        pass

    def load(
        self, owner: JidStr, jid: JidStr, db_state: dict[str, Any]
    ) -> RosterState | None:
        session = self.xmpp.get_session_from_jid(JID(jid))
        if session is None:
            return {
                "name": "",
                "groups": [],
                "from": False,
                "to": False,
                "pending_in": False,
                "pending_out": False,
                "whitelisted": False,
                "subscription": "both",
            }
        else:
            return {
                "name": "",
                "groups": [],
                "from": True,
                "to": True,
                "pending_in": False,
                "pending_out": False,
                "whitelisted": False,
                "subscription": "none",
            }


log = logging.getLogger(__name__)
