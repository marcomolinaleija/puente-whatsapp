"""
Typing stuff
"""

import contextlib
import re
import warnings
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, fields
from datetime import datetime
from enum import IntEnum
from functools import cached_property
from pathlib import Path
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Literal,
    NamedTuple,
    TypeAlias,
    TypedDict,
    TypeVar,
    Union,
)

import aiohttp
from slixmpp import Message, Presence
from slixmpp.types import PresenceShows, PresenceTypes, ResourceDict  # noqa: F401

if TYPE_CHECKING:
    from ..contact import LegacyContact, LegacyRoster
    from ..core.gateway import BaseGateway
    from ..core.session import BaseSession
    from ..group import LegacyBookmarks, LegacyMUC
    from ..group.participant import LegacyParticipant

AnySession: TypeAlias = "BaseSession[Any]"
AnyGateway: TypeAlias = "BaseGateway[AnySession]"
AnyMUC: TypeAlias = "LegacyMUC[Any]"
AnyBookmarks: TypeAlias = "LegacyBookmarks[Any]"
AnyRoster: TypeAlias = "LegacyRoster[Any]"
AnyParticipant: TypeAlias = "LegacyParticipant[Any]"

LegacyContactType = TypeVar("LegacyContactType", bound="LegacyContact")
LegacyMUCType = TypeVar("LegacyMUCType", bound=AnyMUC)
LegacyParticipantType = TypeVar("LegacyParticipantType", bound=AnyParticipant)

SessionType = TypeVar("SessionType", bound=AnySession)
AnyRecipient = Union["LegacyContact", AnyMUC]
RecipientType = TypeVar("RecipientType", bound=AnyRecipient)
Sender = Union["LegacyContact", "AnyParticipant"]

ChatState = Literal["active", "composing", "gone", "inactive", "paused"]
ProcessingHint = Literal["no-store", "markable", "store"]
Marker = Literal["acknowledged", "received", "displayed"]
FieldType = Literal[
    "boolean",
    "fixed",
    "text-single",
    "text-multi",
    "jid-single",
    "jid-multi",
    "list-single",
    "list-multi",
    "text-private",
]
MucAffiliation = Literal["owner", "admin", "member", "outcast", "none"]
MucRole = Literal["visitor", "participant", "moderator", "none"]
# https://xmpp.org/registrar/disco-categories.html#client
ClientType = Literal[
    "bot", "console", "game", "handheld", "pc", "phone", "sms", "tablet", "web"
]
AttachmentDisposition = Literal["attachment", "inline"]


@dataclass
class MessageReference:
    """
    A "message reply", ie a "quoted message" (:xep:`0461`)

    At the very minimum, the legacy message ID attribute must be set, but to
    ensure that the quote is displayed in all XMPP clients, the author must also
    be set (use the string "user" if the slidge user is the author of the referenced
    message).
    The body is used as a fallback for XMPP clients that do not support :xep:`0461`
    of that failed to find the referenced message.
    """

    legacy_id: str
    author: Union[Literal["user"], AnyParticipant, "LegacyContact"] | None = None
    body: str | None = None


@dataclass
class LegacyAttachment:
    """
    A file attachment to a message

    At the minimum, one of the ``path``, ``steam``, ``data`` or ``url`` attribute
    has to be set

    To be used with :meth:`.LegacyContact.send_files` or
    :meth:`.LegacyParticipant.send_files`
    """

    path: Path | str | None = None
    name: str | None = None
    stream: IO[bytes] | None = None
    aio_stream: AsyncIterator[bytes] | None = None
    data: bytes | None = None
    content_type: str | None = None
    legacy_file_id: str | None = None
    url: str | None = None
    caption: str | None = None
    """
    A caption for this specific image. For a global caption for a list of attachments,
    use the ``body`` parameter of :meth:`.AttachmentMixin.send_files`
    """
    disposition: AttachmentDisposition | None = None
    is_sticker: bool = False
    size: int | None = None

    def __post_init__(self) -> None:
        if all(
            x is None
            for x in (self.path, self.stream, self.data, self.url, self.aio_stream)
        ):
            raise TypeError("There is not data in this attachment", self)

        if isinstance(self.path, str):
            self.path = Path(self.path)

        if self.is_sticker:
            if self.disposition == "attachment":
                warnings.warn(
                    "Sticker declared as 'attachment' disposition, changing it to 'inline'"
                )
            self.disposition = "inline"

    def format_for_user(self) -> str:
        if self.name:
            name = self.name
        elif self.path:
            name = self.path.name  # type:ignore[union-attr]
        elif self.url:
            name = self.url
        else:
            name = ""

        if self.caption:
            name = f"{name}: {self.caption}" if name else self.caption

        return name

    def __str__(self) -> str:
        attrs = ", ".join(
            f"{f.name}={getattr(self, f.name)!r}"
            for f in fields(self)
            if getattr(self, f.name) is not None and f.name != "data"
        )
        if self.data is not None:
            data_str = f"data=<{len(self.data)} bytes>"
            to_join = (attrs, data_str) if attrs else (data_str,)
            attrs = ", ".join(to_join)
        return f"Attachment({attrs})"


class MucType(IntEnum):
    """
    The type of group, private, public, anonymous or not.
    """

    GROUP = 0
    """
    A private group, members-only and non-anonymous, eg a family group.
    """
    CHANNEL = 1
    """
    A public group, aka an anonymous channel.
    """
    CHANNEL_NON_ANONYMOUS = 2
    """
    A public group where participants' legacy IDs are visible to everybody.
    """


PseudoPresenceShow = PresenceShows | Literal[""]


MessageOrPresenceTypeVar = TypeVar("MessageOrPresenceTypeVar", bound=Message | Presence)


class LinkPreview(NamedTuple):
    """
    Embedded metadata from :xep:`0511`.

    See <https://ogp.me/>_.
    """

    about: str
    """
    URL of the link.
    """
    title: str | None
    """
    Title of the linked page.
    """
    description: str | None
    """
    A description of the page.
    """
    url: str | None
    """
    The canonical URL of the link.
    """
    image: str | Path | bytes | None
    """
    An image representing the link. If it is a string, it should represent a URL to an image.
    """
    type: str | None
    """
    Type of the link destination.
    """
    site_name: str | None
    """
    Name of the web site.
    """

    @property
    def is_empty(self) -> bool:
        return not any(x for x in self)


class Mention(NamedTuple):
    contact: "LegacyContact"
    start: int
    end: int


class Hat(NamedTuple):
    uri: str
    title: str
    hue: float | None = None


class UserPreferences(TypedDict):
    sync_avatar: bool
    sync_presence: bool


class MamMetadata(NamedTuple):
    id: str
    sent_on: datetime


class HoleBound(NamedTuple):
    id: str
    timestamp: datetime


class CachedPresence(NamedTuple):
    last_seen: datetime | None = None
    ptype: PresenceTypes | None = None
    pstatus: str | None = None
    pshow: PresenceShows | None = None


class Avatar(NamedTuple):
    path: Path | None = None
    unique_id: str | None = None
    url: str | None = None
    data: bytes | None = None


class SpaceMetadata(NamedTuple):
    creator_legacy_id: str | None = None
    name: str | None = None
    description: str | None = None
    member_count: int | None = None
    owner_legacy_ids: Iterable[str] = []


@dataclass
class Reply:
    msg_id: str
    to: "LegacyContact | LegacyParticipant[Any] | None"
    fallback: str | None = None

    @cached_property
    def fallback_no_quote_mark(self) -> str | None:
        """
        Return multi-line text without leading quote marks (i.e. the ">" character).
        """
        if not self.fallback:
            return None
        return re.sub(_STRIP_QUOTE_RE, "", self.fallback).strip()


_STRIP_QUOTE_RE = re.compile(r"^>\s*", flags=re.MULTILINE)


@dataclass
class XMPPAttachment:
    url: str
    is_sticker: bool = False
    cid: str | None = None
    content_type: str | None = None

    @contextlib.asynccontextmanager
    async def get(self) -> AsyncIterator[aiohttp.ClientResponse]:
        async with (
            aiohttp.ClientSession() as session,
            session.get(self.url) as response,
        ):
            yield response


@dataclass
class _AbstractXMPPMessage:
    body: str | None = None
    link_previews: tuple[LinkPreview, ...] = ()
    attachments: tuple[XMPPAttachment, ...] = ()
    mentions: tuple[Mention, ...] = ()
    replace: str | None = None
    reply: Reply | None = None
    thread: str | None = None
    is_forwarded: bool = False


class XMPPAttachmentMessage(_AbstractXMPPMessage):
    body: str | None
    attachments: tuple[XMPPAttachment, *tuple[XMPPAttachment, ...]]


class XMPPTextMessage(_AbstractXMPPMessage):
    body: str
    attachments: tuple[()]


XMPPMessage = XMPPAttachmentMessage | XMPPTextMessage


@dataclass
class Sticker:
    path: Path
    content_type: str | None
    hashes: dict[str, str]
    fallback: str | None = None
    reply: Reply | None = None
    thread: str | None = None
