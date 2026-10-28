from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from slixmpp import JID, Iq
from slixmpp.exceptions import XMPPError

from ...db import GatewayUser
from .. import config
from .util import DispatcherMixin

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class RegistrationMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: AnyGateway) -> None:
        super().__init__(xmpp)
        xmpp.plugin["xep_0077"].api.register(
            self.xmpp.make_registration_form,  # type:ignore[arg-type]
            "make_registration_form",
        )
        xmpp.plugin["xep_0077"].api.register(self._user_get, "user_get")  # type:ignore[arg-type]
        xmpp.plugin["xep_0077"].api.register(self._user_validate, "user_validate")  # type:ignore[arg-type]
        xmpp.plugin["xep_0077"].api.register(self._user_modify, "user_modify")  # type:ignore[arg-type]
        # kept for slixmpp internal API compat
        # TODO: either fully use slixmpp internal API or rewrite registration without it at all
        xmpp.plugin["xep_0077"].api.register(lambda *a: None, "user_remove")

        xmpp.add_event_handler("user_register", self._on_user_register)
        xmpp.add_event_handler("user_unregister", self._on_user_unregister)

    def get_user(self, jid: JID) -> GatewayUser | None:
        session = self.xmpp.get_session_from_jid(jid)
        if session is None:
            return None
        return session.user

    async def _user_get(
        self,
        _gateway_jid: JID,
        _node: str,
        ifrom: JID,
        iq: Iq,
    ) -> GatewayUser | None:
        if ifrom is None:
            ifrom = iq.get_from()
        return self.get_user(ifrom)

    async def _user_validate(
        self,
        _gateway_jid: JID,
        _node: str,
        ifrom: JID,
        iq: Iq,
    ) -> None:
        xmpp = self.xmpp
        log.debug("User validate: %s", ifrom.bare)
        form_dict = {f.var: iq.get(f.var) for f in xmpp.REGISTRATION_FIELDS}
        xmpp.raise_if_not_allowed_jid(ifrom)
        try:
            legacy_module_data = await xmpp.user_prevalidate(ifrom, form_dict)
        except ValueError as e:
            raise XMPPError("bad-request", str(e))
        except XMPPError:
            raise
        except Exception as e:
            raise XMPPError("internal-server-error", str(e))
        if legacy_module_data is None:
            legacy_module_data = form_dict
        with self.xmpp.store.session() as orm:
            user = GatewayUser(
                jid=ifrom.bare,
                legacy_module_data=legacy_module_data,
            )
            orm.add(user)
            orm.commit()
            log.info("New user: %s", user)

    async def _user_modify(
        self,
        _gateway_jid: JID,
        _node: str,
        ifrom: JID,
        form_dict: dict[str, str | None],
    ) -> None:
        try:
            await self.xmpp.user_prevalidate(ifrom, form_dict)
        except ValueError as e:
            raise XMPPError("bad-request", str(e))
        except XMPPError:
            raise
        except Exception as e:
            raise XMPPError("internal-server-error", str(e))
        log.debug("Modify user: %s", ifrom)
        with self.xmpp.store.session() as orm:
            user = orm.query(GatewayUser).one_or_none()
        if user is None:
            raise XMPPError("internal-server-error", "User not found")
        user.legacy_module_data.update(form_dict)
        self.xmpp.store.users.update(user)

    async def _on_user_register(self, iq: Iq) -> None:
        session = await self._get_session(iq, wait_for_ready=False)
        for jid in config.ADMINS:
            self.xmpp.send_message(
                mto=jid,
                mbody=f"{iq.get_from()} has registered",
                mtype="chat",
                mfrom=self.xmpp.boundjid.bare,
            )
        session.send_gateway_message(self.xmpp.WELCOME_MESSAGE)
        await self.xmpp.login_wrap(session)

    async def _on_user_unregister(self, iq: Iq) -> None:
        await self.xmpp.kill_session(iq.get_from())


log = logging.getLogger(__name__)
