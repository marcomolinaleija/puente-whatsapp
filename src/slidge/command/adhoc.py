import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, ParamSpec, TypeVar, cast

from slixmpp import JID, Iq
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0004 import Form as SlixForm
from slixmpp.plugins.xep_0030.stanza.items import DiscoItems
from slixmpp.plugins.xep_0050.adhoc import CommandType

from slidge.util.types import AnyMUC, AnyRecipient, AnySession

from ..contact import LegacyContact
from ..core import config
from ..util.util import strip_leading_emoji
from . import Command, CommandResponseType, Confirmation, Form, TableResult
from .base import (
    CommandResponseRecipientType,
    CommandResponseSessionType,
    ContactCommand,
    FormField,
    MUCCommand,
)
from .categories import CommandCategory

if TYPE_CHECKING:
    from ..core.gateway import BaseGateway


GatewayType = TypeVar("GatewayType", bound="BaseGateway[Any]")
AdhocSessionType = dict[str, Any]
T = TypeVar("T")
P = ParamSpec("P")


class AdhocProvider(Generic[GatewayType]):
    """
    A slixmpp-like plugin to handle adhoc commands, with less boilerplate and
    untyped dict values than slixmpp.
    """

    FORM_TIMEOUT = 120  # seconds
    xmpp: GatewayType

    def __init__(self, xmpp: GatewayType) -> None:
        self.xmpp = xmpp
        self._commands = dict[str, Command[AnySession]]()
        self._categories = dict[str, list[Command[AnySession]]]()
        xmpp.plugin["xep_0030"].set_node_handler(
            "get_items",
            jid=xmpp.boundjid,
            node=self.xmpp.plugin["xep_0050"].stanza.Command.namespace,
            handler=self.get_items,
        )
        self.xmpp.plugin["xep_0050"].api.register(self.__get_command, "get_command")
        self.__timeouts: dict[str, asyncio.TimerHandle] = {}

    async def __get_command(
        self,
        jid: JID | str | None = None,
        node: str | None = None,
        ifrom: JID | None = None,
        args: None = None,
    ) -> CommandType | None:
        if node is None:
            return None
        if jid is None:
            jid = self.xmpp.boundjid.bare
        if not isinstance(jid, JID):
            jid = JID(jid)
        if jid == self.xmpp.boundjid.bare:
            return self.xmpp.plugin["xep_0050"].commands.get((jid.full, node))
        if ifrom is None:
            raise XMPPError("undefined-condition")
        session = self.xmpp.get_session_from_jid(ifrom)
        if session is None:
            raise XMPPError("subscription-required")
        recipient = await session.get_contact_or_group_or_participant(jid)
        if recipient is None:
            return None
        if recipient.is_participant:
            if recipient.contact is None:
                return None
            recipient = recipient.contact
        command = recipient.commands.get(node)
        if command is None:
            return None
        name = strip_leading_emoji_if_needed(command.NAME)
        handler = partial(self.__wrap_initial_handler, command, recipient=recipient)
        return name, handler, None, None  # type:ignore

    async def __wrap_initial_handler(
        self,
        command: Command[AnySession]
        | type[ContactCommand[LegacyContact]]
        | type[MUCCommand[AnyMUC]],
        iq: Iq,
        adhoc_session: AdhocSessionType,
        recipient: AnyRecipient | None = None,
    ) -> AdhocSessionType:
        ifrom = iq.get_from()
        if recipient is None:
            cmd = cast(Command[AnySession], command)
            session = cmd.raise_if_not_authorized(ifrom)
            result1: CommandResponseSessionType[Any] = await self.__wrap_handler(
                cmd.run, session, ifrom
            )
            return await self.__handle_result(session, result1, adhoc_session)
        else:
            cmd2 = cast(
                type[ContactCommand[LegacyContact]] | type[MUCCommand[AnyMUC]], command
            )
            result2: CommandResponseRecipientType[Any] = await self.__wrap_handler(
                cmd2.run,  # type:ignore[arg-type]
                recipient,
            )
            return await self.__handle_result(
                recipient.session, result2, adhoc_session, recipient
            )

    async def __handle_category_list(
        self, category: CommandCategory, iq: Iq, adhoc_session: AdhocSessionType
    ) -> AdhocSessionType:
        try:
            session = self.xmpp.get_session_from_stanza(iq)
        except XMPPError:
            session = None
        commands: dict[str, Command[AnySession]] = {}
        for command in self._categories[category.node]:
            try:
                command.raise_if_not_authorized(iq.get_from())
            except XMPPError:
                continue
            commands[command.NODE] = command
        if len(commands) == 0:
            raise XMPPError(
                "not-authorized", "There is no command you can run in this category"
            )
        return await self.__handle_result(
            session,
            Form(
                category.name,
                "",
                [
                    FormField(
                        var="command",
                        label="Command",
                        type="list-single",
                        options=[
                            {
                                "label": strip_leading_emoji_if_needed(command.NAME),
                                "value": command.NODE,
                            }
                            for command in commands.values()
                        ],
                    )
                ],
                partial(self.__handle_category_choice, commands),
            ),
            adhoc_session,
        )

    async def __handle_category_choice(
        self,
        commands: dict[str, Command[AnySession]],
        form_values: dict[str, str],
        session: "AnySession | None",
        jid: JID,
    ) -> CommandResponseSessionType[Any]:
        command = commands[form_values["command"]]
        result: CommandResponseSessionType[Any] = await self.__wrap_handler(
            command.run, session, jid
        )
        return result

    async def __handle_result(
        self,
        session: "AnySession | None",
        result: CommandResponseType,
        adhoc_session: AdhocSessionType,
        recipient: AnyRecipient | None = None,
    ) -> AdhocSessionType:
        if isinstance(result, str) or result is None:
            adhoc_session["has_next"] = False
            adhoc_session["next"] = None
            adhoc_session["payload"] = None
            adhoc_session["notes"] = [("info", result or "Success!")]
            return adhoc_session

        if isinstance(result, Form):
            adhoc_session["next"] = partial(
                self.__wrap_form_handler, session, result, recipient
            )
            adhoc_session["has_next"] = True
            adhoc_session["payload"] = result.get_xml()
            if result.timeout_handler is not None:
                self.__timeouts[adhoc_session["id"]] = self.xmpp.loop.call_later(
                    self.FORM_TIMEOUT,
                    partial(
                        self.__wrap_timeout, result.timeout_handler, adhoc_session["id"]
                    ),
                )
            return adhoc_session

        if isinstance(result, Confirmation):
            adhoc_session["next"] = partial(
                self.__wrap_confirmation, session, result, recipient
            )
            adhoc_session["has_next"] = True
            adhoc_session["payload"] = result.get_form()
            return adhoc_session

        if isinstance(result, TableResult):
            adhoc_session["next"] = None
            adhoc_session["has_next"] = False
            adhoc_session["payload"] = result.get_xml()
            return adhoc_session

        raise XMPPError("internal-server-error", text="OOPS!")

    def __wrap_timeout(self, handler: Callable[[], None], session_id: str) -> None:
        try:
            del self.xmpp.plugin["xep_0050"].sessions[session_id]
        except KeyError:
            log.error("Timeout but session could not be found: %s", session_id)
            handler()

    @staticmethod
    async def __wrap_handler(
        f: Callable[P, Awaitable[T] | T],
        *a: P.args,
        **k: P.kwargs,
    ) -> T:
        try:
            if inspect.iscoroutinefunction(f):
                return await f(*a, **k)  # type:ignore[no-any-return]
            elif hasattr(f, "func") and inspect.iscoroutinefunction(f.func):
                return await f(*a, **k)  # type:ignore[misc,no-any-return]
            else:
                return f(*a, **k)  # type:ignore[return-value]
        except XMPPError:
            raise
        except Exception as e:
            log.debug("Exception in %s", f, exc_info=e)
            raise XMPPError("internal-server-error", text=str(e))

    async def __wrap_form_handler(
        self,
        session: "AnySession | None",
        result: Form,
        recipient: AnyRecipient | None,
        form: SlixForm,
        adhoc_session: AdhocSessionType,
    ) -> AdhocSessionType:
        timer = self.__timeouts.pop(adhoc_session["id"], None)
        if timer is not None:
            print("canceled", adhoc_session["id"])
            timer.cancel()
        form_values = result.get_values(form)
        if recipient is None:
            new_result = await self.__wrap_handler(
                result.handler,
                form_values,
                session,
                adhoc_session["from"],
                *result.handler_args,
                **result.handler_kwargs,
            )
        else:
            new_result = await self.__wrap_handler(
                result.handler,
                recipient,
                form_values,
                *result.handler_args,
                **result.handler_kwargs,
            )
        return await self.__handle_result(session, new_result, adhoc_session, recipient)

    async def __wrap_confirmation(
        self,
        session: "AnySession | None",
        confirmation: Confirmation,
        recipient: AnyRecipient | None,
        form: SlixForm,
        adhoc_session: AdhocSessionType,
    ) -> AdhocSessionType:
        if form.get_values().get("confirm"):
            if recipient is None:
                result = await self.__wrap_handler(
                    confirmation.handler,
                    session,
                    adhoc_session["from"],
                    *confirmation.handler_args,
                    **confirmation.handler_kwargs,
                )
                if confirmation.success:
                    result = confirmation.success
            else:
                result = await self.__wrap_handler(
                    confirmation.handler,
                    recipient,
                    *confirmation.handler_args,
                    **confirmation.handler_kwargs,
                )
        else:
            result = "You canceled the operation"

        return await self.__handle_result(session, result, adhoc_session, recipient)

    def register(self, command: Command[AnySession], jid: JID | None = None) -> None:
        """
        Register a command as a adhoc command.

        this does not need to be called manually, ``BaseGateway`` takes care of
        that.

        :param command:
        :param jid:
        """
        if jid is None:
            jid = self.xmpp.boundjid
        elif not isinstance(jid, JID):
            jid = JID(jid)

        if (category := command.CATEGORY) is None:
            if command.NODE in self._commands:
                raise RuntimeError(
                    "There is already a command for the node '%s'", command.NODE
                )
            self._commands[command.NODE] = command
            self.xmpp.plugin["xep_0050"].add_command(
                jid=jid,
                node=command.NODE,
                name=strip_leading_emoji_if_needed(command.NAME),
                handler=partial(self.__wrap_initial_handler, command),
            )
        else:
            if isinstance(category, str):
                category = CommandCategory(category, category)
            node = category.node
            name = category.name
            if node not in self._categories:
                self._categories[node] = list[Command[AnySession]]()
                self.xmpp.plugin["xep_0050"].add_command(
                    jid=jid,
                    node=node,
                    name=strip_leading_emoji_if_needed(name),
                    handler=partial(self.__handle_category_list, category),
                )
            self._categories[node].append(command)

    async def get_items(self, jid: JID, node: str, iq: Iq) -> DiscoItems:
        """
        Get items for a disco query

        :param jid: the entity that should return its items
        :param node: which command node is requested
        :param iq:  the disco query IQ
        :return: commands accessible to the given JID will be listed
        """
        ifrom = iq.get_from()
        ifrom_str = str(ifrom)
        if (
            not self.xmpp.jid_validator.match(ifrom_str)
            and ifrom_str not in config.ADMINS
        ):
            raise XMPPError(
                "forbidden",
                "You are not authorized to execute adhoc commands on this gateway. "
                "If this is unexpected, ask your administrator to verify that "
                "'user-jid-validator' is correctly set in slidge's configuration.",
            )

        all_items = self.xmpp.plugin["xep_0030"].static.get_items(jid, node, None, None)
        log.debug("Static items: %r", all_items)
        if not all_items:
            return DiscoItems()

        session = self.xmpp.get_session_from_jid(ifrom)

        filtered_items = DiscoItems()
        filtered_items["node"] = self.xmpp.plugin["xep_0050"].stanza.Command.namespace
        for item in all_items:
            authorized = True
            if item["node"] in self._categories:
                for command in self._categories[item["node"]]:
                    try:
                        command.raise_if_not_authorized(
                            ifrom, fetch_session=False, session=session
                        )
                    except XMPPError:
                        authorized = False
                    else:
                        authorized = True
                        break
            else:
                try:
                    self._commands[item["node"]].raise_if_not_authorized(
                        ifrom, fetch_session=False, session=session
                    )
                except XMPPError:
                    authorized = False

            if authorized:
                filtered_items.append(item)

        return filtered_items


def strip_leading_emoji_if_needed(text: str) -> str:
    if config.STRIP_LEADING_EMOJI_ADHOC:
        return strip_leading_emoji(text)
    return text


log = logging.getLogger(__name__)
