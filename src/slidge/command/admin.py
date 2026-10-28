# Commands only accessible for slidge admins
import functools
import importlib
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from slixmpp import JID
from slixmpp.exceptions import XMPPError

from ..core import config
from ..db.models import GatewayUser
from ..util.types import AnySession
from .base import (
    NODE_PREFIX,
    Command,
    CommandAccess,
    Confirmation,
    FormField,
    FormSession,
    FormValues,
    TableResult,
)
from .categories import ADMINISTRATION

NODE_PREFIX = NODE_PREFIX + "admin/"

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class AdminCommand(Command[AnySession]):
    ACCESS = CommandAccess.ADMIN_ONLY
    CATEGORY = ADMINISTRATION


class ListUsers(AdminCommand):
    NAME = "👤 List registered users"
    HELP = "List the users registered to this gateway"
    CHAT_COMMAND = "list_users"
    NODE = NODE_PREFIX + CHAT_COMMAND

    async def run(
        self, _session: "AnySession | None", _ifrom: JID, *_: str
    ) -> TableResult:
        items = []
        with self.xmpp.store.session() as orm:
            for u in orm.query(GatewayUser).all():
                d = u.registration_date
                joined = "" if d is None else d.isoformat(timespec="seconds")
                items.append({"jid": u.jid.bare, "joined": joined})
        return TableResult(
            description="List of registered users",
            fields=[FormField("jid", type="jid-single"), FormField("joined")],
            items=items,  # type:ignore
        )


class SlidgeInfo(AdminCommand):
    NAME = "ℹ️ Server information"  # noqa:RUF001
    HELP = "List the users registered to this gateway"
    CHAT_COMMAND = "info"
    NODE = NODE_PREFIX + CHAT_COMMAND
    ACCESS = CommandAccess.ANY

    async def run(self, _session: "AnySession | None", _ifrom: JID, *_: str) -> str:
        start = self.xmpp.datetime_started
        uptime = datetime.now() - start

        if uptime.days:
            days_ago = f"{uptime.days} day{'s' if uptime.days != 1 else ''}"
        else:
            days_ago = None
        hours, seconds = divmod(uptime.seconds, 3600)

        if hours:
            hours_ago = f"{hours} hour"
            if hours != 1:
                hours_ago += "s"
        else:
            hours_ago = None

        minutes, seconds = divmod(seconds, 60)
        if minutes:
            minutes_ago = f"{minutes} minute"
            if minutes != 1:
                minutes_ago += "s"
        else:
            minutes_ago = None

        if any((days_ago, hours_ago, minutes_ago)):
            seconds_ago = None
        else:
            seconds_ago = f"{seconds} second"
            if seconds != 1:
                seconds_ago += "s"

        ago = ", ".join(
            [a for a in (days_ago, hours_ago, minutes_ago, seconds_ago) if a]
        )

        legacy_module = importlib.import_module(config.LEGACY_MODULE)
        version = getattr(legacy_module, "__version__", "No version")

        import slidge

        return (
            f"{self.xmpp.COMPONENT_NAME} (slidge core {slidge.__version__},"
            f" {config.LEGACY_MODULE} {version})\n"
            f"Up since {start:%Y-%m-%d %H:%M} ({ago} ago)"
        )


class DeleteUser(AdminCommand):
    NAME = "❌ Delete a user"
    HELP = "Unregister a user from the gateway"
    CHAT_COMMAND = "delete_user"
    NODE = NODE_PREFIX + CHAT_COMMAND

    async def run(
        self, _session: "AnySession | None", _ifrom: JID, *_: str
    ) -> FormSession[AnySession]:
        return FormSession(
            title="Remove a slidge user",
            instructions="Enter the bare JID of the user you want to delete",
            fields=[FormField("jid", type="jid-single", label="JID", required=True)],
            handler=self.delete,
        )

    async def delete(
        self,
        form_values: FormValues,
        _session: "AnySession | None",
        _ifrom: JID,
    ) -> Confirmation:
        jid: JID = form_values.get("jid")  # type:ignore
        with self.xmpp.store.session() as orm:
            user = orm.query(GatewayUser).where(GatewayUser.jid == jid).one_or_none()
        if user is None:
            raise XMPPError("item-not-found", text=f"There is no user '{jid}'")

        return Confirmation(
            prompt=f"Are you sure you want to unregister '{jid}' from slidge?",
            success=f"User {jid} has been deleted",
            handler=functools.partial(self.finish, jid=jid),
        )

    async def finish(
        self, _session: "AnySession | None", _ifrom: JID, jid: JID
    ) -> None:
        with self.xmpp.store.session() as orm:
            user = orm.query(GatewayUser).where(GatewayUser.jid == jid).one_or_none()
        if user is None:
            raise XMPPError("bad-request", f"{jid} has no account here!")
        await self.xmpp.unregister_user(user, "You have been unregistered by an admin")


class ChangeLoglevel(AdminCommand):
    NAME = "📋 Change the verbosity of the logs"
    HELP = "Set the logging level"
    CHAT_COMMAND = "loglevel"
    NODE = NODE_PREFIX + CHAT_COMMAND

    async def run(
        self, _session: "AnySession | None", _ifrom: JID, *_: str
    ) -> FormSession[AnySession]:
        return FormSession(
            title=self.NAME,
            instructions=self.HELP,
            fields=[
                FormField(
                    "level",
                    label="Log level",
                    required=True,
                    type="list-single",
                    options=[
                        {"label": "WARNING (quiet)", "value": str(logging.WARNING)},
                        {"label": "INFO (normal)", "value": str(logging.INFO)},
                        {"label": "DEBUG (verbose)", "value": str(logging.DEBUG)},
                    ],
                )
            ],
            handler=self.finish,
        )

    @staticmethod
    async def finish(
        form_values: FormValues,
        _session: "AnySession | None",
        _ifrom: JID,
    ) -> None:
        logging.getLogger().setLevel(int(form_values["level"]))  # type:ignore


class Exec(AdminCommand):
    NAME = HELP = "Exec arbitrary python code. SHOULD NEVER BE AVAILABLE IN PROD."
    CHAT_COMMAND = "!"
    NODE = "exec"
    ACCESS = CommandAccess.ADMIN_ONLY

    prev_snapshot = None

    context: ClassVar[dict[str, Any]] = {}

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)

    async def run(self, session: "AnySession | None", ifrom: JID, *args: str) -> str:
        from contextlib import redirect_stdout
        from io import StringIO

        f = StringIO()
        with redirect_stdout(f):
            exec(" ".join(args), self.context)

        out = f.getvalue()
        if out:
            return f"```\n{out}\n```"
        else:
            return "No output"
