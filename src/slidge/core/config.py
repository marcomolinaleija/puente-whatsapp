from pathlib import Path

from slixmpp import JID as JIDType

# REQUIRED, so not default value


class _Categories:
    MANDATORY = (0, "Mandatory settings")
    BASE = (10, "Basic configuration")
    ATTACHMENTS = (20, "Attachments")
    LOG = (30, "Logging")
    ADVANCED = (40, "Advanced settings")


LEGACY_MODULE: str
LEGACY_MODULE__DOC = (
    "Importable python module containing (at least) a BaseGateway and a LegacySession subclass. "
    "NB: this is not needed if you use a gateway-specific entrypoint, e.g., `slidgram` or "
    "`python -m slidgram`."
)
LEGACY_MODULE__CATEGORY = _Categories.BASE

SERVER: str = "localhost"
SERVER__DOC = (
    "The XMPP server's host name. Defaults to localhost, which is the "
    "standard way of running slidge, on the same host as the XMPP server. "
    "The 'Jabber Component Protocol' (XEP-0114) does not mention encryption, "
    "so you *should* provide encryption another way, eg via port forwarding, if "
    "you change this."
)
SERVER__SHORT = "s"
SERVER__CATEGORY = _Categories.BASE

SECRET: str
SECRET__DOC = "The gateway component's secret (required to connect to the XMPP server)"
SECRET__CATEGORY = _Categories.MANDATORY

JID: JIDType
JID__DOC = "The gateway component's JID"
JID__SHORT = "j"
JID__CATEGORY = _Categories.MANDATORY

PORT: str = "5347"
PORT__DOC = "The XMPP server's port for incoming component connections"
PORT__SHORT = "p"
PORT__CATEGORY = _Categories.BASE

# Dynamic default (depends on other values)

HOME_DIR: Path
HOME_DIR__DOC = (
    "Directory where slidge will writes it persistent data and cache. "
    "Defaults to /var/lib/slidge/${SLIDGE_JID}. "
)
HOME_DIR__DYNAMIC_DEFAULT = True
HOME_DIR__CATEGORY = _Categories.BASE

DB_URL: str
DB_URL__DOC = (
    "Database URL, see <https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls>. "
    "Defaults to sqlite:///${HOME_DIR}/slidge.sqlite"
)
DB_URL__DYNAMIC_DEFAULT = True
DB_URL__CATEGORY = _Categories.ADVANCED

USER_JID_VALIDATOR: str
USER_JID_VALIDATOR__DOC = (
    "Regular expression to restrict users that can register to the gateway, by JID. "
    "Defaults to .*@${INFERRED_SERVER}. INFERRED_SERVER is derived for the gateway JID, "
    "by removing whatever is before the first encountered dot in it. Example: if "
    "slidge's JID=slidge.example.org, INFERRED_SERVER=example.org."
)
USER_JID_VALIDATOR__DYNAMIC_DEFAULT = True
USER_JID_VALIDATOR__CATEGORY = _Categories.BASE

# Optional, so default value + type hint if default is None

ADMINS: tuple[JIDType, ...] = ()
ADMINS__DOC = "JIDs of the gateway admins"
ADMINS__CATEGORY = _Categories.BASE

UPLOAD_SERVICE: str | None = None
UPLOAD_SERVICE__DOC = (
    "JID of an HTTP upload service the gateway can use. "
    "This is optional, as it should be automatically determined via service"
    "discovery."
)
UPLOAD_SERVICE__CATEGORY = _Categories.ATTACHMENTS

AVATAR_SIZE = 200
AVATAR_SIZE__DOC = (
    "Maximum image size (width and height), image ratio will be preserved"
)
AVATAR_SIZE__CATEGORY = _Categories.ADVANCED

USE_ATTACHMENT_ORIGINAL_URLS: bool = False
USE_ATTACHMENT_ORIGINAL_URLS__DOC = (
    "For legacy plugins in which attachments are publicly downloadable URLs, "
    "let XMPP clients directly download them from this URL. Note that this will "
    "probably leak your client IP to the legacy network."
)
USE_ATTACHMENT_ORIGINAL_URLS__CATEGORY = _Categories.ATTACHMENTS

UPLOAD_REQUESTER: str | None = None
UPLOAD_REQUESTER__DOC = (
    "Set which JID should request the upload slots. Defaults to the user's JID if "
    "IQ/get privileges granted for the 'urn:xmpp:http:upload:0' namespace; the component "
    "JID otherwise."
)
UPLOAD_REQUESTER__CATEGORY = _Categories.ATTACHMENTS

UPLOAD_URL_PREFIX: str | None = None
UPLOAD_URL_PREFIX__DOC = (
    "This is an optional setting to make sure the URL of your upload service is never leaked "
    "to the legacy network in bodies of messages. This can happen under rare circumstances and/or bugs,"
    "when replying to an attachment. Set this to the common prefix of the public URL your attachments get, "
    "eg https://upload.example.org:5281/"
)
UPLOAD_URL_PREFIX__CATEGORY = _Categories.ATTACHMENTS

NO_UPLOAD_PATH: str | None = None
NO_UPLOAD_PATH__DOC = (
    "Instead of using the XMPP server's HTTP upload component, copy files to this dir. "
    "You need to set NO_UPLOAD_URL_PREFIX too if you use this option, and configure "
    "an web server to serve files in this dir."
)
NO_UPLOAD_PATH__CATEGORY = _Categories.ATTACHMENTS

NO_UPLOAD_URL_PREFIX: str | None = None
NO_UPLOAD_URL_PREFIX__DOC = (
    "Base URL that servers files in the dir set in the no-upload-path option, "
    "eg https://example.com:666/slidge-attachments/"
)
NO_UPLOAD_URL_PREFIX__CATEGORY = _Categories.ATTACHMENTS


NO_UPLOAD_FILE_READ_OTHERS: bool = False
NO_UPLOAD_FILE_READ_OTHERS__DOC = (
    "After writing a file in NO_UPLOAD_PATH, change its permission so that 'others' can"
    " read it."
)
NO_UPLOAD_FILE_READ_OTHERS__CATEGORY = _Categories.ATTACHMENTS

IGNORE_DELAY_THRESHOLD = 300
IGNORE_DELAY_THRESHOLD__DOC = (
    "Threshold, in seconds, below which the <delay> information is stripped "
    "out of emitted stanzas."
)
IGNORE_DELAY_THRESHOLD__CATEGORY = _Categories.ADVANCED

PARTIAL_REGISTRATION_TIMEOUT = 3600
PARTIAL_REGISTRATION_TIMEOUT__DOC = (
    "Timeout before registration and login. Only useful for legacy networks where "
    "a single step registration process is not enough."
)
PARTIAL_REGISTRATION_TIMEOUT__CATEGORY = _Categories.ADVANCED

QR_TIMEOUT = 60
QR_TIMEOUT__DOC = "Timeout for QR code flashing confirmation."
QR_TIMEOUT__CATEGORY = _Categories.ADVANCED

FIX_FILENAME_SUFFIX_MIME_TYPE: bool = False
FIX_FILENAME_SUFFIX_MIME_TYPE__DOC = (
    "Fix the Filename suffix based on the Mime Type of the file. Some clients (eg"
    " Conversations) may not inline files that have a wrong suffix for the MIME Type."
    " Therefore the MIME Type of the file is checked, if the suffix is not valid for"
    " that MIME Type, a valid one will be picked."
)
FIX_FILENAME_SUFFIX_MIME_TYPE__CATEGORY = _Categories.ATTACHMENTS

LOG_FILE: Path | None = None
LOG_FILE__DOC = "Log to a file instead of stdout/err"
LOG_FILE__CATEGORY = _Categories.LOG

LOG_FORMAT: str = "%(levelname)s:%(name)s:%(message)s"
LOG_FORMAT__DOC = (
    "Optionally, a format string for logging messages. Refer to "
    "https://docs.python.org/3/library/logging.html#logrecord-attributes "
    "for available options."
)
LOG_FORMAT__CATEGORY = _Categories.LOG

MAM_MAX_DAYS = 7
MAM_MAX_DAYS__DOC = "Maximum number of days for group archive retention."
MAM_MAX_DAYS__CATEGORY = _Categories.BASE

ATTACHMENT_MAXIMUM_FILE_NAME_LENGTH = 200
ATTACHMENT_MAXIMUM_FILE_NAME_LENGTH__DOC = (
    "Some legacy network provide ridiculously long filenames, strip above this limit, "
    "preserving suffix."
)
ATTACHMENT_MAXIMUM_FILE_NAME_LENGTH__CATEGORY = _Categories.ATTACHMENTS

CONVERT_STICKERS: bool = False
CONVERT_STICKERS__DOC = (
    "Convert lottie vector stickers (from the legacy side) to webp animations."
)
CONVERT_STICKERS__CATEGORY = _Categories.ATTACHMENTS

AVATAR_RESAMPLING_THREADS = 2
AVATAR_RESAMPLING_THREADS__DOC = (
    "Number of additional threads to use for avatar resampling. Even in a single-core "
    "context, this makes avatar resampling non-blocking."
)
AVATAR_RESAMPLING_THREADS__CATEGORY = _Categories.ADVANCED

DEV_MODE = False
DEV_MODE__DOC = (
    "Enables an interactive python shell via chat commands, for admins."
    "Not safe to use in prod, but great during dev."
)
DEV_MODE__CATEGORY = _Categories.ADVANCED


STRIP_LEADING_EMOJI_ADHOC = False
STRIP_LEADING_EMOJI_ADHOC__DOC = (
    "Strip the leading emoji in ad-hoc command names, if present, in case you "
    "are a emoji-hater."
)
STRIP_LEADING_EMOJI_ADHOC__CATEGORY = _Categories.ADVANCED

COMPONENT_NAME: str | None = None
COMPONENT_NAME__DOC = (
    "Overrides the default component name with a custom one. This is seen in service discovery and as the nickname "
    "of the component in chat windows."
)
COMPONENT_NAME__CATEGORY = _Categories.ADVANCED

WELCOME_MESSAGE: str | None = None
WELCOME_MESSAGE__DOC = (
    "Overrides the default welcome message received by newly registered users."
)
WELCOME_MESSAGE__CATEGORY = _Categories.ADVANCED
