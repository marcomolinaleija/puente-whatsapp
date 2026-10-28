# Handle slidge commands by exchanging chat messages with the gateway components.

# Ad-hoc methods should provide a better UX, but some clients do not support them,
# so this is mostly a fallback.
import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    Never,
    ParamSpec,
    TypeVar,
    cast,
    overload,
)
from urllib.parse import quote as url_quote

from slixmpp import JID, CoroutineCallback, Message, StanzaPath
from slixmpp.exceptions import XMPPError
from slixmpp.types import JidStr, MessageTypes

from slidge.command.base import (
    CommandResponseRecipientType,
    CommandResponseSessionType,
    ConfirmationRecipient,
    ConfirmationSession,
    FormRecipient,
    FormSession,
)
from slidge.contact import LegacyContact
from slidge.group import LegacyMUC
from slidge.util.types import AnyMUC, AnyRecipient, AnySession

from . import Command, CommandResponseType, Confirmation, Form, TableResult
from .categories import CommandCategory

if TYPE_CHECKING:
    from ..core.gateway import BaseGateway

GatewayType = TypeVar("GatewayType", bound="BaseGateway[Any]")
T = TypeVar("T")
P = ParamSpec("P")


class ChatCommandProvider(Generic[GatewayType]):
    UNKNOWN = "Wut? I don't know that command: {}"
    xmpp: GatewayType

    def __init__(self, xmpp: GatewayType) -> None:
        self.xmpp = xmpp
        self._keywords = list[str]()
        self._commands: dict[str, Command[AnySession]] = {}
        self._input_futures = dict[str, asyncio.Future[str]]()
        self.xmpp.register_handler(
            CoroutineCallback(
                "chat_command_handler",
                StanzaPath(f"message@to={self.xmpp.boundjid.bare}"),
                self._handle_message,  # type: ignore
            )
        )

    def register(self, command: Command[AnySession]) -> None:
        """
        Register a command to be used via chat messages with the gateway

        Plugins should not call this, any class subclassing Command should be
        automatically added by slidge core.

        :param command: the new command
        """
        t = command.CHAT_COMMAND
        if t in self._commands:
            raise RuntimeError("There is already a command triggered by '%s'", t)
        self._commands[t] = command

    @overload
    async def input(self, jid: JidStr, text: str | None = None) -> str: ...

    @overload
    async def input(
        self, jid: JidStr, text: str | None = None, *, blocking: Literal[False] = ...
    ) -> asyncio.Future[str]: ...

    @overload
    async def input(
        self,
        jid: JidStr,
        text: str | None = None,
        *,
        mtype: MessageTypes = "chat",
        timeout: int = 60,
        blocking: Literal[True] = True,
        **msg_kwargs: Any,  # noqa:ANN401
    ) -> str: ...

    async def input(
        self,
        jid: JidStr,
        text: str | None = None,
        *,
        mtype: MessageTypes = "chat",
        timeout: int = 60,
        blocking: bool = True,
        **msg_kwargs: Any,
    ) -> str | asyncio.Future[str]:
        """
        Request arbitrary user input using a simple chat message, and await the result.

        You shouldn't need to call directly bust instead use :meth:`.BaseSession.input`
        to directly target a user.

        NB: When using this, the next message that the user sent to the component will
        not be transmitted to :meth:`.BaseGateway.on_gateway_message`, but rather intercepted.
        Await the coroutine to get its content.

        :param jid: The JID we want input from
        :param text: A prompt to display for the user
        :param mtype: Message type
        :param timeout:
        :param blocking: If set to False, timeout has no effect and an :class:`asyncio.Future`
            is returned instead of a str
        :return: The user's reply
        """
        jid = JID(jid)
        if text is not None:
            self.xmpp.send_message(
                mto=jid,
                mbody=text,
                mtype=mtype,
                mfrom=self.xmpp.boundjid.bare,
                **msg_kwargs,
            )
        f: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._input_futures[jid.bare] = f
        if not blocking:
            return f
        try:
            await asyncio.wait_for(f, timeout)
        except TimeoutError:
            self.xmpp.send_message(
                mto=jid,
                mbody="You took too much time to reply",
                mtype=mtype,
                mfrom=self.xmpp.boundjid.bare,
            )
            del self._input_futures[jid.bare]
            raise XMPPError("remote-server-timeout", "You took too much time to reply")

        return f.result()

    async def _handle_message(self, msg: Message) -> None:
        if not msg["body"]:
            return

        if not msg.get_from().node:
            return  # ignore component and server messages

        f = self._input_futures.pop(msg.get_from().bare, None)
        if f is not None:
            f.set_result(msg["body"])
            return

        c = msg["body"]
        first_word, *rest = c.split(" ")
        first_word = first_word.lower()

        if first_word == "help":
            return self._handle_help(msg, *rest)

        if first_word in ("contact", "room"):
            return await self._handle_recipient(first_word, msg, *rest)

        mfrom = msg.get_from()

        command = self._commands.get(first_word)
        if command is None:
            self._not_found(msg, first_word)
            return

        try:
            session = command.raise_if_not_authorized(mfrom)
        except XMPPError as e:
            reply = msg.reply()
            reply["body"] = e.text
            reply.send()
            raise

        result: CommandResponseSessionType[Any] = await self.__wrap_handler(
            msg, command.run, session, mfrom, *rest
        )
        self.xmpp.delivery_receipt.ack(msg)
        await self._handle_result(result, msg, session)

    def __make_uri(self, body: str) -> str:
        return f"xmpp:{self.xmpp.boundjid.bare}?message;body={body}"

    async def _handle_result(
        self,
        result: CommandResponseSessionType[Any] | CommandResponseRecipientType[Any],
        msg: Message,
        session: "AnySession | None",
        recipient: AnyRecipient | None = None,
    ) -> CommandResponseSessionType[Any] | CommandResponseRecipientType[Any]:
        if isinstance(result, str) or result is None:
            reply = msg.reply()
            reply["body"] = result or "End of command."
            reply.send()
            return None

        if isinstance(result, Form):
            if recipient is None:
                result = cast(FormSession[AnySession], result)
            else:
                result = cast(FormRecipient[AnyRecipient], result)
            try:
                return await self.__handle_form(  # type:ignore[return-value]
                    result, msg, session, recipient=recipient
                )
            except XMPPError as e:
                if (
                    result.timeout_handler is None
                    or e.condition != "remote-server-timeout"
                ):
                    raise e
                return result.timeout_handler()

        if isinstance(result, Confirmation):
            yes_or_no = await self.input(msg.get_from(), result.prompt)
            if not yes_or_no.lower().startswith("y"):
                reply = msg.reply()
                reply["body"] = "Canceled"
                reply.send()
                return None
            if recipient is None:
                result = cast(ConfirmationSession[AnySession], result)
                result = await self.__wrap_handler(
                    msg,
                    result.handler,
                    session,
                    msg.get_from(),
                    *result.handler_args,
                    **result.handler_kwargs,
                )
            else:
                result = cast(ConfirmationRecipient[AnyRecipient], result)
                result = await self.__wrap_handler(
                    msg,
                    result.handler,
                    recipient,
                    *result.handler_args,
                    **result.handler_kwargs,
                )
            return await self._handle_result(result, msg, session, recipient=recipient)

        if isinstance(result, TableResult):
            if len(result.items) == 0:
                msg.reply("Empty results").send()
                return None

            body = result.description + "\n"
            for item in result.items:
                for f in result.fields:
                    if f.type == "jid-single":
                        j = JID(item[f.var])
                        value = f"xmpp:{percent_encode(j)}"
                        if result.jids_are_mucs:
                            value += "?join"
                    else:
                        value = item[f.var]  # type:ignore
                    body += f"\n{f.label or f.var}: {value}"
            msg.reply(body).send()
            return None

        raise RuntimeError

    async def __handle_form(
        self,
        result: Form,
        msg: Message,
        session: "AnySession | None",
        recipient: AnyRecipient | None = None,
    ) -> CommandResponseType:
        form_values = {}
        for t in result.title, result.instructions:
            if t:
                msg.reply(t).send()
        for f in result.fields:
            if f.type == "fixed":
                msg.reply(f"{f.label or f.var}: {f.value}").send()
            else:
                if f.type == "list-multi":
                    msg.reply(
                        "Multiple selection allowed, use new lines as a separator, ie, "
                        "one selected item per line. To select no item, reply with a space "
                        "(the punctuation)."
                    ).send()
                if f.options:
                    for o in f.options:
                        msg.reply(f"{o['label']}: {self.__make_uri(o['value'])}").send()
                if f.value:
                    msg.reply(f"Default: {f.value}").send()
                if f.type == "boolean":
                    msg.reply("yes: " + self.__make_uri("yes")).send()
                    msg.reply("no: " + self.__make_uri("no")).send()

                ans = await self.xmpp.input(
                    msg.get_from(),
                    (f.label or f.var) + "? (or 'abort')",
                    mtype="chat",
                )
                if ans.lower() == "abort":
                    return await self._handle_result("Command aborted", msg, session)
                if f.type == "boolean":
                    ans = "true" if ans.lower() == "yes" else "false"

                if f.type.endswith("multi"):
                    choices = [] if ans == " " else ans.split("\n")
                    form_values[f.var] = f.validate(choices)
                else:
                    form_values[f.var] = f.validate(ans)
        if recipient is None:
            new_result = await self.__wrap_handler(
                msg,
                result.handler,
                form_values,
                session,
                msg.get_from(),
                *result.handler_args,
                **result.handler_kwargs,
            )
            new_result = cast(CommandResponseSessionType[Any], new_result)
        else:
            new_result = await self.__wrap_handler(
                msg,
                result.handler,
                recipient,
                form_values,
                *result.handler_args,
                **result.handler_kwargs,
            )
            new_result = cast(CommandResponseRecipientType[Any], new_result)

        return await self._handle_result(new_result, msg, session, recipient=recipient)

    @staticmethod
    async def __wrap_handler(
        msg: Message,
        f: Callable[P, Awaitable[T] | T],
        *a: P.args,
        **k: P.kwargs,
    ) -> T | None:
        try:
            if inspect.iscoroutinefunction(f):
                return await f(*a, **k)  # type:ignore[no-any-return]
            elif hasattr(f, "func") and inspect.iscoroutinefunction(f.func):
                return await f(*a, **k)  # type:ignore[misc,no-any-return]
            else:
                return f(*a, **k)  # type:ignore[return-value]
        except Exception as e:
            log.debug("Error in %s", f, exc_info=e)
            reply = msg.reply()
            reply["body"] = f"Error: {e}"
            reply.send()
            return None

    def _handle_help(self, msg: Message, *rest: str) -> None:
        if len(rest) == 0:
            reply = msg.reply()
            reply["body"] = self._help(msg.get_from())
            reply.send()
        elif len(rest) == 1 and (command := self._commands.get(rest[0])):
            reply = msg.reply()
            reply["body"] = f"{command.CHAT_COMMAND}: {command.NAME}\n{command.HELP}"
            reply.send()
        else:
            self._not_found(msg, str(rest))

    def _help(self, mfrom: JID) -> str:
        session = self.xmpp.get_session_from_jid(mfrom)

        msg = "Available commands:"
        for c in sorted(
            self._commands.values(),
            key=lambda co: (
                (
                    co.CATEGORY
                    if isinstance(co.CATEGORY, str)
                    else (
                        co.CATEGORY.name
                        if isinstance(co.CATEGORY, CommandCategory)
                        else ""
                    )
                ),
                co.CHAT_COMMAND,
            ),
        ):
            try:
                c.raise_if_not_authorized(mfrom, fetch_session=False, session=session)
            except XMPPError:
                continue
            msg += f"\n{c.CHAT_COMMAND} -- {c.NAME}"
        return msg

    def _not_found(self, msg: Message, word: str) -> Never:
        e = self.UNKNOWN.format(word)
        msg.reply(e).send()
        raise XMPPError("item-not-found", e)

    async def _handle_recipient(
        self, recipient_str: Literal["contact", "room"], msg: Message, *args: str
    ) -> None:
        session = self.xmpp.get_session_from_jid(msg.get_from())

        recipient_cls = LegacyContact if recipient_str == "contact" else LegacyMUC

        if session is None:
            raise XMPPError("subscription-required")

        if len(args) == 0 or args[0] == "help":
            self.xmpp.delivery_receipt.ack(msg)
            self._help_recipient(msg, recipient_cls)
            return

        if len(args) == 1:
            self._help_recipient(msg, recipient_cls)
            raise XMPPError(
                "bad-request",
                f"Contact commands require at least two parameters: {recipient_str}_jid_username and command_name",
            )

        jid_username, command_name, *rest = args

        command = recipient_cls.commands_chat.get(command_name)
        if command is None:
            raise XMPPError("item-not-found")

        if recipient_cls is LegacyContact:
            legacy_id = await session.contacts.jid_username_to_legacy_id(jid_username)
            recipient = await session.contacts.by_legacy_id(legacy_id)
        else:
            legacy_id = await session.bookmarks.jid_username_to_legacy_id(jid_username)
            recipient = await session.bookmarks.by_legacy_id(legacy_id)

        result = await self.__wrap_handler(msg, command.run, recipient, *rest)  # type:ignore[arg-type,func-returns-value]
        self.xmpp.delivery_receipt.ack(msg)
        await self._handle_result(result, msg, session, recipient)

    def _help_recipient(
        self, msg: Message, recipient_cls: "type[LegacyContact | AnyMUC]"
    ) -> None:
        msg.reply(
            "Available commands:\n"
            + "\n".join(
                f"{co.CHAT_COMMAND} ({co.NAME}): {co.HELP}"
                for co in recipient_cls.commands_chat.values()
            )
        ).send()


def percent_encode(jid: JID) -> str:
    return f"{url_quote(jid.user)}@{jid.server}"


log = logging.getLogger(__name__)
