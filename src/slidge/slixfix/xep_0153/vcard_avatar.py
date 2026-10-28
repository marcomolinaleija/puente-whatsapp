from slixmpp.plugins.base import BasePlugin
from slixmpp.plugins.xep_0153 import VCardTempUpdate, stanza
from slixmpp.stanza import Presence
from slixmpp.xmlstream import register_stanza_plugin


class XEP_0153(BasePlugin):
    name = "xep_0153"
    description = "XEP-0153: vCard-Based Avatars (slidge, just for MUCs)"
    dependencies = {"xep_0054"}
    stanza = stanza

    def plugin_init(self) -> None:
        register_stanza_plugin(Presence, VCardTempUpdate)
