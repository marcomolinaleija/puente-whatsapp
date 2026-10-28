from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    TypedDict,
    TypeVar,
    Union,
)

from slixmpp import JID
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0004 import Form as SlixForm
from slixmpp.plugins.xep_0004.stanza.field import FormField as SlixFormField
from slixmpp.types import JidStr

from slidge.contact import LegacyContact
from slidge.group import LegacyMUC

from ..core import config
from ..util.types import (
    AnyMUC,
    AnySession,
    FieldType,
    LegacyContactType,
    LegacyMUCType,
    SessionType,
)

NODE_PREFIX = "https://slidge.im/command/core/"

if TYPE_CHECKING:
    from ..util.types import AnyGateway
    from .categories import CommandCategory


HandlerType = (
    Callable[[AnySession, JID], "CommandResponseType"]
    | Callable[[AnySession, JID], Awaitable["CommandResponseType"]]
)

FormValues = dict[str, str | JID | bool | list[str] | list[JID]]


@dataclass
class TableResult:
    """
    Structured data as the result of a command
    """

    fields: Sequence["FormField"]
    """
    The 'columns names' of the table.
    """
    items: Sequence[dict[str, str | JID]]
    """
    The rows of the table. Each row is a dict where keys are the fields ``var``
    attribute.
    """
    description: str
    """
    A description of the content of the table.
    """

    jids_are_mucs: bool = False

    def get_xml(self) -> SlixForm:
        """
        Get a slixmpp "form" (with <reported> header)to represent the data

        :return: some XML
        """
        form = SlixForm()
        form["type"] = "result"
        form["title"] = self.description
        for f in self.fields:
            form.add_reported(f.var, label=f.label, type=f.type)
        for item in self.items:
            form.add_item({k: str(v) for k, v in item.items()})
        return form


@dataclass
class SearchResult(TableResult):
    """
    Results of the search command (search for contacts via Jabber Search)

    Return type of :meth:`BaseSession.search`.
    """

    description: str = "Contact search results"


@dataclass
class Confirmation:
    """
    A confirmation 'dialog'
    """

    prompt: str
    """
    The text presented to the command triggering user
    """
    handler: Any
    """
    An async function that should return a ResponseType
    """
    success: str | None = None
    """
    Text in case of success, used if handler does not return anything
    """
    handler_args: Iterable[Any] = field(default_factory=list)
    """
    arguments passed to the handler
    """
    handler_kwargs: dict[str, Any] = field(default_factory=dict)
    """
    keyword arguments passed to the handler
    """

    def get_form(self) -> SlixForm:
        """
        Get the slixmpp form

        :return: some xml
        """
        form = SlixForm()
        form["type"] = "form"
        form["title"] = self.prompt
        form.append(
            FormField(
                "confirm", type="boolean", value="true", label="Confirm"
            ).get_xml()
        )
        return form


@dataclass
class ConfirmationSession(Confirmation, Generic[SessionType]):
    handler: Callable[
        [SessionType | None, JID],
        Awaitable["CommandResponseSessionType[SessionType]"],
    ]


RecipientType = TypeVar("RecipientType", bound=LegacyContact | LegacyMUC[Any])


@dataclass
class ConfirmationRecipient(Confirmation, Generic[RecipientType]):
    handler: Callable[
        [RecipientType],
        Awaitable["CommandResponseRecipientType[RecipientType]"],
    ]


@dataclass
class Form:
    """
    A form, to request user input
    """

    title: str
    instructions: str
    fields: Sequence["FormField"]
    handler: Any
    handler_args: Iterable[Any] = field(default_factory=list)
    handler_kwargs: dict[str, Any] = field(default_factory=dict)
    timeout_handler: Callable[[], None] | None = None

    def get_values(
        self, slix_form: SlixForm
    ) -> dict[str, list[str] | list[JID] | str | JID | bool | None]:
        """
        Parse form submission

        :param slix_form: the xml received as the submission of a form
        :return: A dict where keys=field.var and values are either strings
            or JIDs (if field.type=jid-single)
        """
        str_values: dict[str, str] = slix_form.get_values()
        values = {}
        for f in self.fields:
            values[f.var] = f.validate(str_values.get(f.var))
        return values

    def get_xml(self) -> SlixForm:
        """
        Get the slixmpp "form"

        :return: some XML
        """
        form = SlixForm()
        form["type"] = "form"
        form["title"] = self.title
        form["instructions"] = self.instructions
        for fi in self.fields:
            form.append(fi.get_xml())
        return form


class FormSession(Form, Generic[SessionType]):
    handler: Callable[
        [FormValues, SessionType | None, JID],
        Awaitable["CommandResponseSessionType[SessionType]"],
    ]


@dataclass
class FormRecipient(Form, Generic[RecipientType]):
    handler: Callable[
        [RecipientType, FormValues],
        Awaitable["CommandResponseRecipientType[RecipientType]"],
    ]


class CommandAccess(int, Enum):
    """
    Defines who can access a given Command
    """

    ADMIN_ONLY = 0
    USER = 1
    USER_LOGGED = 2
    USER_NON_LOGGED = 3
    NON_USER = 4
    ANY = 5


class Option(TypedDict):
    """
    Options to be used for ``FormField``s of type ``list-*``
    """

    label: str
    value: str


# TODO: support forms validation XEP-0122
@dataclass
class FormField:
    """
    Represents a field of the form that a user will see when registering to the gateway
    via their XMPP client.
    """

    var: str = ""
    """
    Internal name of the field, will be used to retrieve via :py:attr:`slidge.GatewayUser.registration_form`
    """
    label: str | None = None
    """Description of the field that the user will see"""
    required: bool = False
    """Whether this field is mandatory or not"""
    private: bool = False
    """
    For sensitive info that should not be displayed on screen while the user types.
    Forces field_type to "text-private"
    """
    type: FieldType = "text-single"
    """Type of the field, see `XEP-0004 <https://xmpp.org/extensions/xep-0004.html#protocol-fieldtypes>`_"""
    value: str = ""
    """Pre-filled value. Will be automatically pre-filled if a registered user modifies their subscription"""
    options: list[Option] | None = None

    image_url: str | None = None
    """An image associated to this field, eg, a QR code"""

    def __post_init__(self) -> None:
        if self.private:
            self.type = "text-private"

    def __acceptable_options(self) -> list[str]:
        if self.options is None:
            raise RuntimeError
        return [x["value"] for x in self.options]

    def validate(
        self, value: str | list[str] | None
    ) -> list[str] | list[JID] | str | JID | bool | None:
        """
        Raise appropriate XMPPError if a given value is valid for this field

        :param value: The value to test
        :return: The same value OR a JID if ``self.type=jid-single``
        """
        if isinstance(value, list) and not self.type.endswith("multi"):
            raise XMPPError("not-acceptable", "A single value was expected")

        if self.type in ("list-multi", "jid-multi", "text-multi"):
            if not value:
                value = []
            if isinstance(value, list):
                if self.type == "text-multi":
                    return value
                return self.__validate_list_multi(value)
            else:
                raise XMPPError("not-acceptable", "Multiple values was expected")

        assert isinstance(value, (str, bool, JID)) or value is None

        if self.required and value is None:
            raise XMPPError("not-acceptable", f"Missing field: '{self.label}'")

        if value is None:
            return None

        if self.type == "jid-single":
            try:
                return JID(value)
            except ValueError:
                raise XMPPError("not-acceptable", f"Not a valid JID: '{value}'")

        elif self.type == "list-single":
            if value not in self.__acceptable_options():
                raise XMPPError("not-acceptable", f"Not a valid option: '{value}'")

        elif self.type == "boolean":
            return value.lower() in ("1", "true") if isinstance(value, str) else value

        return value

    def __validate_list_multi(self, value: list[str]) -> list[str] | list[JID]:
        for v in value:
            if v not in self.__acceptable_options():
                raise XMPPError("not-acceptable", f"Not a valid option: '{v}'")
        if self.type == "list-multi":
            return value
        return [JID(v) for v in value]

    def get_xml(self) -> SlixFormField:
        """
        Get the field in slixmpp format

        :return: some XML
        """
        f = SlixFormField()
        f["var"] = self.var
        f["label"] = self.label
        f["required"] = self.required
        f["type"] = self.type
        if self.options:
            for o in self.options:
                f.add_option(**o)
        f["value"] = self.value
        if self.image_url:
            f["media"].add_uri(self.image_url, itype="image/png")
        return f


CommandResponseType = TableResult | Confirmation | Form | str | None

CommandResponseSessionType = (
    TableResult
    | ConfirmationSession[SessionType]
    | FormSession[SessionType]
    | str
    | None
)

CommandResponseRecipientType = (
    TableResult
    | ConfirmationRecipient[RecipientType]
    | FormRecipient[RecipientType]
    | str
    | None
)


class _CommandMixin(ABC):
    NAME: str = NotImplemented
    """
    Friendly name of the command, eg: "do something with stuff"
    """
    HELP: str = NotImplemented
    """
    Long description of what the command does
    """
    NODE: str = NotImplemented
    """
    Name of the node used for ad-hoc commands
    """
    CHAT_COMMAND: str = NotImplemented
    """
    Text to send to the gateway to trigger the command via a message
    """


class Command(_CommandMixin, Generic[SessionType]):
    """
    Abstract base class to implement gateway commands (chatbot and ad-hoc)
    """

    ACCESS: "CommandAccess" = NotImplemented
    """
    Who can use this command
    """

    CATEGORY: Union[str, "CommandCategory"] | None = None
    """
    If used, the command will be under this top-level category.
    Use the same string for several commands to group them.
    This hierarchy only used for the adhoc interface, not the chat command
    interface.
    """

    subclasses: ClassVar[list[type["Command[SessionType]"]]] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        self.xmpp = xmpp

    def __init_subclass__(
        cls,
        **kwargs: Any,  # noqa:ANN401
    ) -> None:
        # store subclasses so subclassing is enough for the command to be
        # picked up by slidge
        cls.subclasses.append(cls)

    async def run(
        self,
        session: SessionType | None,
        ifrom: JID,
        *args: str,
    ) -> CommandResponseSessionType[SessionType]:
        """
        Entry point of the command

        :param session: If triggered by a registered user, its slidge Session
        :param ifrom: JID of the command-triggering entity
        :param args: When triggered via chatbot type message, additional words
            after the CHAT_COMMAND string was passed

        :return: Either a TableResult, a Form, a Confirmation, a text, or None
        """
        raise XMPPError("feature-not-implemented")

    def _get_session(self, jid: JID) -> SessionType | None:
        return self.xmpp.get_session_from_jid(jid)  # type:ignore

    def __can_use_command(self, jid: JID) -> bool:
        j = jid.bare
        return bool(self.xmpp.jid_validator.match(j) or j in config.ADMINS)

    def raise_if_not_authorized(
        self,
        jid: JID,
        fetch_session: bool = True,
        session: SessionType | None = None,
    ) -> SessionType | None:
        """
        Raise an appropriate error is jid is not authorized to use the command

        :param jid: jid of the entity trying to access the command
        :param fetch_session:
        :param session:

        :return:session of JID if it exists
        """
        if not self.__can_use_command(jid):
            raise XMPPError(
                "bad-request", "Your JID is not allowed to use this gateway."
            )
        if fetch_session:
            session = self._get_session(jid)

        if self.ACCESS == CommandAccess.ADMIN_ONLY and not is_admin(jid):
            raise XMPPError("not-authorized")
        elif self.ACCESS == CommandAccess.NON_USER and session is not None:
            raise XMPPError(
                "bad-request", "This is only available for non-users. Unregister first."
            )
        elif self.ACCESS == CommandAccess.USER and session is None:
            raise XMPPError(
                "forbidden",
                "This is only available for users that are registered to this gateway",
            )
        elif self.ACCESS == CommandAccess.USER_NON_LOGGED:
            if session is None or session.logged:
                raise XMPPError(
                    "forbidden",
                    (
                        "This is only available for users that are not logged to the"
                        " legacy service"
                    ),
                )
        elif self.ACCESS == CommandAccess.USER_LOGGED and (
            session is None or not session.logged
        ):
            raise XMPPError(
                "forbidden",
                ("This is only available when you are logged in to the legacy service"),
            )
        return session


T = TypeVar("T", bound="LegacyContact | AnyMUC")


class _RecipientCommand(_CommandMixin, Generic[T]):
    @staticmethod
    @abstractmethod
    async def run(recipient: T, *args: str) -> CommandResponseRecipientType[T]:
        """
        Entrypoint for a recipient-specific command.

        The first argument is a :class:`LegacyContact` or :class:`LegacyMUC`
        instance. ``*args`` are extra args passed when using the chatbot.
        """
        raise NotImplementedError


class ContactCommand(_RecipientCommand[LegacyContactType], Generic[LegacyContactType]):
    """
    A command that will be avaible on a contact.

    It implicitly requires the user to be registered and logged.
    It is never instantiated, so all methods must be static methods.
    Its entrypoint is the ``run()`` static method.
    """

    recipient_cls = LegacyContact

    def __init_subclass__(
        cls,
        **kwargs: Any,  # noqa:ANN401
    ) -> None:
        cls.recipient_cls.commands[cls.NODE] = cls  # type:ignore[assignment]
        cls.recipient_cls.commands_chat[cls.CHAT_COMMAND] = cls  # type:ignore[assignment]


class MUCCommand(_RecipientCommand[LegacyMUCType], Generic[LegacyMUCType]):
    """
    A command that will be avaible on a MUC.

    It implicitly requires the user to be registered and logged.
    It is never instantiated, so all methods must be static methods.
    Its entrypoint is the ``run()`` static method.
    """

    recipient_cls = LegacyMUC

    def __init_subclass__(
        cls,
        **kwargs: Any,  # noqa:ANN401
    ) -> None:
        cls.recipient_cls.commands[cls.NODE] = cls
        cls.recipient_cls.commands_chat[cls.CHAT_COMMAND] = cls


def is_admin(jid: JidStr) -> bool:
    return JID(jid).bare in config.ADMINS
