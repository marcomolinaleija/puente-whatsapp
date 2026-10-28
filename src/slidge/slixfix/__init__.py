# This module contains patches for slixmpp; some have pending requests upstream
# and should be removed on the next slixmpp release.
import uuid

# ruff: noqa: F401
import slixmpp.plugins
import slixmpp.stanza.roster
from slixmpp import Iq, Message
from slixmpp.exceptions import IqError
from slixmpp.plugins.xep_0050 import XEP_0050, Command
from slixmpp.plugins.xep_0356.permissions import IqPermission
from slixmpp.plugins.xep_0356.privilege import XEP_0356, PrivilegedIqError
from slixmpp.plugins.xep_0469.stanza import NS as PINNED_NS
from slixmpp.plugins.xep_0469.stanza import Pinned
from slixmpp.xmlstream import StanzaBase

from ..util.archive_msg import set_client_namespace
from . import (
    xep_0077,
    xep_0100,
    xep_0153,
    xep_0292,
)


def set_pinned(self, val: bool) -> None:  # noqa
    extensions = self.parent()
    if val:
        extensions.enable("pinned")
    else:
        extensions._del_sub(f"{{{PINNED_NS}}}pinned")


Pinned.set_pinned = set_pinned


def session_bind(self, jid) -> None:  # noqa
    self.xmpp.plugin["xep_0030"].add_feature(Command.namespace)
    # awful hack to for the disco items: we need to comment this line
    # related issue: https://todo.sr.ht/~nicoco/slidge/131
    # self.xmpp['xep_0030'].set_items(node=Command.namespace, items=tuple())


XEP_0050.session_bind = session_bind  # type:ignore


def reply(self, body=None, clear: bool = True):  # noqa
    """
    Overrides slixmpp's Message.reply(), since it strips to sender's resource
    for mtype=groupchat, and we do not want that, because when we raise an XMPPError,
    we actually want to preserve the resource.
    (this is called in RootStanza.exception() to handle XMPPErrors)
    """
    new_message = StanzaBase.reply(self, clear)
    new_message["thread"] = self["thread"]
    new_message["parent_thread"] = self["parent_thread"]

    del new_message["id"]
    if self.stream is not None and self.stream.use_message_ids:
        new_message["id"] = self.stream.new_id()

    if body is not None:
        new_message["body"] = body
    return new_message


Message.reply = reply  # type: ignore

slixmpp.plugins.PLUGINS.extend(
    [
        "xep_0292_provider",
    ]
)
