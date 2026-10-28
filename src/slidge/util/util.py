import logging
import mimetypes
import re
from collections.abc import Callable, Collection, Coroutine
from functools import wraps
from pathlib import Path
from time import time
from typing import (
    Any,
    ClassVar,
    Concatenate,
    NamedTuple,
    ParamSpec,
    Protocol,
    TypeVar,
)
from xml.etree import ElementTree as ET

try:
    import emoji
except ImportError:
    EMOJI_LIB_AVAILABLE = False
else:
    EMOJI_LIB_AVAILABLE = True

from slixmpp.types import ExtPresenceShows, ResourceDict

from .types import Mention

try:
    import magic
except ImportError as e:
    magic = None  # type:ignore
    logging.warning(
        (
            "Libmagic is not available: %s. "
            "It's OK if you don't use fix-filename-suffix-mime-type."
        ),
        e,
    )


def fix_suffix(
    path: Path, mime_type: str | None, file_name: str | None
) -> tuple[str, str]:
    guessed = magic.from_file(path, mime=True)
    if guessed == mime_type:
        log.debug("Magic and given MIME match")
    else:
        log.debug("Magic (%s) and given MIME (%s) differ", guessed, mime_type)
        mime_type = guessed

    valid_suffix_list = mimetypes.guess_all_extensions(mime_type, strict=False)

    name = Path(file_name) if file_name else Path(path.name)

    suffix = name.suffix

    if suffix in valid_suffix_list:
        log.debug("Suffix %s is in %s", suffix, valid_suffix_list)
        return str(name), guessed

    valid_suffix = mimetypes.guess_extension(mime_type.split(";")[0], strict=False)
    if valid_suffix is None:
        log.debug("No valid suffix found")
        return str(name), guessed

    log.debug("Changing suffix of %s to %s", file_name or path.name, valid_suffix)
    return str(name.with_suffix(valid_suffix)), guessed


class SubclassableOnce:
    # To allow importing everything, including plugins, during tests
    TEST_MODE: bool = False
    __subclasses: ClassVar[
        dict[type["SubclassableOnce"], type["SubclassableOnce"] | None]
    ] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        if SubclassableOnce not in cls.__bases__:
            base = SubclassableOnce.__find_direct_child(cls)
            existing = SubclassableOnce.__subclasses.get(base)
            if existing is not None and not SubclassableOnce.TEST_MODE:
                raise RuntimeError("This class must be subclassed once at most!")
            cls.__subclasses[base] = cls
        super().__init_subclass__(**kwargs)

    @staticmethod
    def __find_direct_child(cls: type["SubclassableOnce"]) -> type["SubclassableOnce"]:
        for base in cls.__bases__:
            if issubclass(base, SubclassableOnce):
                return base
        else:
            raise RuntimeError("wut")

    @classmethod
    def get_self_or_unique_subclass(cls) -> "type[SubclassableOnce]":
        try:
            return cls.get_unique_subclass()
        except AttributeError:
            return cls

    @classmethod
    def get_unique_subclass(cls) -> "type[SubclassableOnce]":
        existing = SubclassableOnce.__subclasses.get(cls)
        if existing is None:
            raise AttributeError("Could not find any subclass", cls)
        return existing

    @classmethod
    def reset_subclass(cls) -> None:
        log.debug("Resetting subclass of %s", cls)
        cls.__subclasses[cls] = None


def is_valid_phone_number(phone: str | None) -> bool:
    if phone is None:
        return False
    match = re.match(r"\+\d.*", phone)
    if match is None:
        return False
    return match[0] == phone


def strip_illegal_chars(s: str, repl: str = "") -> str:
    return ILLEGAL_XML_CHARS_RE.sub(repl, s)


# from https://stackoverflow.com/a/64570125/5902284 and Link Mauve
ILLEGAL = [
    (0x00, 0x08),
    (0x0B, 0x0C),
    (0x0E, 0x1F),
    (0x7F, 0x84),
    (0x86, 0x9F),
    (0xFDD0, 0xFDDF),
    (0xFFFE, 0xFFFF),
    (0x1FFFE, 0x1FFFF),
    (0x2FFFE, 0x2FFFF),
    (0x3FFFE, 0x3FFFF),
    (0x4FFFE, 0x4FFFF),
    (0x5FFFE, 0x5FFFF),
    (0x6FFFE, 0x6FFFF),
    (0x7FFFE, 0x7FFFF),
    (0x8FFFE, 0x8FFFF),
    (0x9FFFE, 0x9FFFF),
    (0xAFFFE, 0xAFFFF),
    (0xBFFFE, 0xBFFFF),
    (0xCFFFE, 0xCFFFF),
    (0xDFFFE, 0xDFFFF),
    (0xEFFFE, 0xEFFFF),
    (0xFFFFE, 0xFFFFF),
    (0x10FFFE, 0x10FFFF),
]

ILLEGAL_RANGES = [rf"{chr(low)}-{chr(high)}" for (low, high) in ILLEGAL]
XML_ILLEGAL_CHARACTER_REGEX = "[" + "".join(ILLEGAL_RANGES) + "]"
ILLEGAL_XML_CHARS_RE = re.compile(XML_ILLEGAL_CHARACTER_REGEX)


# from https://stackoverflow.com/a/35804945/5902284
def addLoggingLevel(
    levelName: str = "TRACE",
    levelNum: int = logging.DEBUG - 5,
    methodName: str | None = None,
) -> None:
    """
    Comprehensively adds a new logging level to the `logging` module and the
    currently configured logging class.

    `levelName` becomes an attribute of the `logging` module with the value
    `levelNum`. `methodName` becomes a convenience method for both `logging`
    itself and the class returned by `logging.getLoggerClass()` (usually just
    `logging.Logger`). If `methodName` is not specified, `levelName.lower()` is
    used.

    To avoid accidental clobberings of existing attributes, this method will
    raise an `AttributeError` if the level name is already an attribute of the
    `logging` module or if the method name is already present

    Example
    -------
    >>> addLoggingLevel('TRACE', logging.DEBUG - 5)
    >>> logging.getLogger(__name__).setLevel("TRACE")
    >>> logging.getLogger(__name__).trace('that worked')
    >>> logging.trace('so did this')
    >>> logging.TRACE
    5

    """
    if not methodName:
        methodName = levelName.lower()

    if hasattr(logging, levelName):
        log.debug(f"{levelName} already defined in logging module")
        return
    if hasattr(logging, methodName):
        log.debug(f"{methodName} already defined in logging module")
        return
    if hasattr(logging.getLoggerClass(), methodName):
        log.debug(f"{methodName} already defined in logger class")
        return

    # This method was inspired by the answers to Stack Overflow post
    # http://stackoverflow.com/q/2183233/2988730, especially
    # http://stackoverflow.com/a/13638084/2988730
    def logForLevel(self, message, *args, **kwargs) -> None:  # type:ignore[no-untyped-def]  # noqa
        if self.isEnabledFor(levelNum):
            self._log(levelNum, message, args, **kwargs)

    def logToRoot(message, *args, **kwargs) -> None:  # type:ignore[no-untyped-def]  # noqa
        logging.log(levelNum, message, *args, **kwargs)

    logging.addLevelName(levelNum, levelName)
    setattr(logging, levelName, levelNum)
    setattr(logging.getLoggerClass(), methodName, logForLevel)
    setattr(logging, methodName, logToRoot)


class SlidgeLogger(logging.Logger):
    def trace(self) -> None:
        pass


log = logging.getLogger(__name__)


def merge_resources(resources: dict[str, ResourceDict]) -> ResourceDict | None:
    if len(resources) == 0:
        return None

    if len(resources) == 1:
        return next(iter(resources.values()))

    by_priority = sorted(resources.values(), key=lambda r: r["priority"], reverse=True)

    if any(r["show"] == "" for r in resources.values()):
        # if a client is "available", we're "available"
        show: ExtPresenceShows = ""
    else:
        for r in by_priority:
            if r["show"]:
                show = r["show"]
                break
        else:
            raise RuntimeError()

    # if there are different statuses, we use the highest priority one,
    # but we ignore resources without status, even with high priority
    status = ""
    for r in by_priority:
        if r["status"]:
            status = r["status"]
            break

    return {
        "show": show,
        "status": status,
        "priority": 0,
    }


_EMOJI_VARIATION_SELECTOR = "\ufe0f"


def remove_emoji_variation_selector_16(emoji: str) -> str:
    # this is required for compatibility with dino, and maybe other future clients?
    return emoji.removesuffix(_EMOJI_VARIATION_SELECTOR)


NamedTupleT = TypeVar("NamedTupleT", bound=NamedTuple)


def dict_to_named_tuple(data: dict[str, Any], cls: type[NamedTupleT]) -> NamedTupleT:
    return cls(*(data.get(f) for f in cls._fields))  # type:ignore[arg-type]


def replace_mentions(
    text: str,
    mentions: Collection[Mention] | None,
    mapping: Callable[[Mention], str],
) -> str:
    if not mentions:
        return text

    cursor = 0
    pieces = []
    for mention in mentions:
        try:
            new_text = mapping(mention)
        except Exception as exc:
            log.debug("Attempting slidge <= 0.3.3 compatibility: %s", exc)
            new_text = mapping(mention.contact)  # type:ignore
        pieces.extend([text[cursor : mention.start], new_text])
        cursor = mention.end
    pieces.append(text[cursor:])
    return "".join(pieces)


class HasLogger(Protocol):
    log: logging.Logger


P = ParamSpec("P")
T = TypeVar("T")
Self = TypeVar("Self", bound=HasLogger)
TimeItWrapped = Callable[Concatenate[Self, P], Coroutine[Any, Any, T]]


def timeit(func: TimeItWrapped[Self, P, T]) -> TimeItWrapped[Self, P, T]:
    @wraps(func)
    async def wrapped(self: Self, /, *args: P.args, **kwargs: P.kwargs) -> T:
        start = time()
        r = await func(self, *args, **kwargs)
        self.log.debug("%s took %s ms", func.__name__, round((time() - start) * 1000))
        return r

    return wrapped


def strip_leading_emoji(text: str) -> str:
    if not EMOJI_LIB_AVAILABLE:
        return text
    words = text.split(" ")
    # is_emoji returns False for 🛷️ for obscure reasons,
    # purely_emoji seems better
    if len(words) > 1 and emoji.purely_emoji(words[0]):
        return " ".join(words[1:])
    return text


async def noop_coro() -> None:
    pass


def add_quote_prefix(text: str) -> str:
    """
    Return multi-line text with leading quote marks (i.e. the ">" character).
    """
    return "\n".join(("> " + x).strip() for x in text.split("\n")).strip()


def fix_namespaces(
    xml: ET.Element,
    old: str,
    new: str,
) -> None:
    """
    Hack to fix namespaces between jabber:component and jabber:client

    Acts in-place.

    :param xml:
    :param old:
    :param new:
    """
    xml.tag = xml.tag.replace(f"{{{old}}}", f"{{{new}}}")
    for child in xml:
        fix_namespaces(child, old, new)
