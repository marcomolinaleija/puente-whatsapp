# type:ignore
import os
import tempfile
import types
from pathlib import Path
from typing import ClassVar
from xml.dom.minidom import parseString

from slixmpp import JID, ElementBase, Iq, Message, Presence
from slixmpp.stanza.error import Error
from slixmpp.test import SlixTest, TestTransport
from slixmpp.xmlstream import highlight, tostring
from sqlalchemy import create_engine, delete

from slidge import (
    BaseGateway,
    BaseSession,
    LegacyBookmarks,
    LegacyContact,
    LegacyMUC,
    LegacyParticipant,
    LegacyRoster,
)

from ..command import Command
from ..core import config
from ..core.session import _sessions
from ..db import SlidgeStore
from ..db.avatar import avatar_cache
from ..db.meta import Base
from ..db.models import Contact, GatewayUser
from ..util import SubclassableOnce


class SlixTestPlus(SlixTest):
    def setUp(self) -> None:
        super().setUp()
        Error.namespace = "jabber:component:accept"

    def next_sent(self, timeout: float = 0.05) -> Message | Iq | Presence | None:
        self.wait_for_send_queue()
        sent = self.xmpp.socket.next_sent(timeout=timeout)
        if sent is None:
            return None
        xml = self.parse_xml(sent)
        self.fix_namespaces(xml, "jabber:component:accept")
        sent = self.xmpp._build_stanza(xml, "jabber:component:accept")
        return sent


class SlidgeTest(SlixTestPlus):
    plugin: types.ModuleType | dict

    class Config:
        jid = "aim.shakespeare.lit"
        secret = "test"
        server = "shakespeare.lit"
        port = 5222
        upload_service = "upload.test"
        home_dir = Path(tempfile.mkdtemp())
        user_jid_validator = ".*"
        admins: ClassVar[list[str]] = []
        upload_requester = None
        ignore_delay_threshold = 300

    @classmethod
    def setUpClass(cls) -> None:
        for k, v in vars(cls.Config).items():
            setattr(config, k.upper(), v)
        if hasattr(cls, "plugin"):
            subclasses = SubclassableOnce._SubclassableOnce__subclasses
            subclasses.update(
                {
                    BaseGateway: find_subclass(cls.plugin, BaseGateway),
                    BaseSession: find_subclass(cls.plugin, BaseSession),
                    LegacyRoster: find_subclass(cls.plugin, LegacyRoster, base_ok=True),
                    LegacyContact: find_subclass(
                        cls.plugin, LegacyContact, base_ok=True
                    ),
                    LegacyMUC: find_subclass(cls.plugin, LegacyMUC, base_ok=True),
                    LegacyBookmarks: find_subclass(
                        cls.plugin, LegacyBookmarks, base_ok=True
                    ),
                }
            )
            for subclass in subclasses.values():
                if subclass is not None:
                    subclass.__abstractmethods__ = set()

    def setUp(self) -> None:
        # workaround for duplicate output of sql alchemy's log, cf
        # https://stackoverflow.com/a/76498428/5902284
        from sqlalchemy import log as sqlalchemy_log
        from sqlalchemy.pool import StaticPool

        sqlalchemy_log._add_default_handler = lambda x: None
        db_url = os.getenv("SLIDGETEST_DB_URL", "sqlite+pysqlite:///:memory:")
        engine = self.db_engine = create_engine(
            db_url, poolclass=StaticPool if db_url.startswith("postgresql+") else None
        )
        Base.metadata.create_all(engine)
        BaseGateway.store = SlidgeStore(engine)
        BaseGateway._test_mode = True

        self.xmpp = BaseGateway.get_self_or_unique_subclass()()

        self.xmpp.TEST_MODE = True
        avatar_cache.store = self.xmpp.store.avatars
        avatar_cache.set_dir(Path(tempfile.mkdtemp()))
        self.xmpp._always_send_everything = True
        engine.echo = True

        self.xmpp.connection_made(TestTransport(self.xmpp))
        self.xmpp.session_bind_event.set()
        # Remove unique ID prefix to make it easier to test
        self.xmpp._id_prefix = ""
        self.xmpp.default_lang = None
        self.xmpp.peer_default_lang = None

        def new_id() -> str:
            self.xmpp._id += 1
            return str(self.xmpp._id)

        self.xmpp._id = 0
        self.xmpp.new_id = new_id

        # Must have the stream header ready for xmpp.process() to work.
        header = self.xmpp.stream_header

        self.xmpp.data_received(header)
        self.wait_for_send_queue()

        self.xmpp.socket.next_sent()
        self.xmpp.socket.next_sent()

        # Some plugins require messages to have ID values. Set
        # this to True in tests related to those plugins.
        self.xmpp.use_message_ids = False
        self.xmpp.use_presence_ids = False
        Error.namespace = "jabber:component:accept"

    def _add_slidge_user(
        self,
        jid: str | JID = "romeo@shakespeare.lit",
        legacy_module_data: dict | None = None,
        preferences: dict | None = None,
    ) -> None:
        with self.xmpp.store.session() as orm:
            user = GatewayUser(
                jid=JID(jid),
                legacy_module_data=legacy_module_data or {},
                preferences=preferences
                or {"sync_avatar": False, "sync_presence": False},
            )
            orm.add(user)
            orm.commit()
        self.run_coro(
            self.xmpp._BaseGateway__dispatcher._on_user_register(Iq(sfrom=JID(jid)))
        )
        welcome = self.next_sent()
        assert welcome["body"]
        stanza = self.next_sent()
        assert "logging in" in stanza["status"].lower(), stanza
        stanza = self.next_sent()
        assert "syncing contacts" in stanza["status"].lower(), stanza
        if self.xmpp.GROUPS:
            stanza = self.next_sent()
            assert "syncing groups" in stanza["status"].lower(), stanza
        probe = self.next_sent()
        assert probe.get_type() == "probe"
        stanza = self.next_sent()
        assert stanza["status"].lower()

    def user_session(self, jid: str | JID = "romeo@shakespeare.lit") -> BaseSession:
        return BaseSession.get_self_or_unique_subclass().from_jid(JID(jid))

    def get_joined_muc(
        self,
        legacy_id: str | int,
        user_jid: str | JID = "romeo@shakespeare.lit",
        resource: str = "gajim",
    ) -> LegacyMUC:
        muc: LegacyMUC = self.run_coro(
            self.user_session(user_jid).bookmarks.by_legacy_id(legacy_id)
        )
        muc.add_user_resource(resource)
        return muc

    def tearDown(self) -> None:
        self.db_engine.echo = False
        super().tearDown()
        Base.metadata.drop_all(self.xmpp.store._engine)
        self.db_engine.dispose()
        _sessions.clear()

    def setup_logged_session(self, n_contacts: int = 0) -> None:
        with self.xmpp.store.session() as orm:
            user = GatewayUser(
                jid=JID("romeo@montague.lit/gajim").bare,
                legacy_module_data={"username": "romeo", "city": ""},
                preferences={"sync_avatar": True, "sync_presence": True},
            )
            orm.add(user)
            orm.commit()

        with self.xmpp.store.session() as session:
            session.execute(delete(Contact))
            session.commit()

        self.run_coro(
            self.xmpp._BaseGateway__dispatcher._on_user_register(
                Iq(sfrom="romeo@montague.lit/gajim")
            )
        )
        welcome = self.next_sent()
        assert welcome["body"], welcome
        stanza = self.next_sent()
        assert "logging in" in stanza["status"].lower(), stanza
        stanza = self.next_sent()
        assert "syncing contacts" in stanza["status"].lower(), stanza
        if BaseGateway.get_self_or_unique_subclass().GROUPS:
            stanza = self.next_sent()
            assert "syncing groups" in stanza["status"].lower(), stanza
        probe = self.next_sent()
        assert probe.get_type() == "probe"
        stanza = self.next_sent()
        assert "yup" in stanza["status"].lower(), stanza
        self.romeo: BaseSession = BaseSession.get_self_or_unique_subclass().from_jid(
            JID("romeo@montague.lit")
        )

        self.juliet: LegacyContact = self.run_coro(
            self.romeo.contacts.by_legacy_id("juliet")
        )
        self.room: LegacyMUC = self.run_coro(self.romeo.bookmarks.by_legacy_id("room"))
        self.first_witch: LegacyParticipant = self.run_coro(
            self.room.get_participant("firstwitch")
        )
        self.send(  # language=XML
            """
            <iq type="get"
                to="romeo@montague.lit"
                id="1"
                from="aim.shakespeare.lit">
              <pubsub xmlns="http://jabber.org/protocol/pubsub">
                <items node="urn:xmpp:avatar:metadata" />
              </pubsub>
            </iq>
            """
        )

    @classmethod
    def tearDownClass(cls) -> None:
        reset_subclasses()


def format_stanza(stanza: ElementBase) -> str:
    return highlight(
        "\n".join(parseString(tostring(stanza.xml)).toprettyxml().split("\n")[1:])
    )


def find_subclass(o, parent, base_ok: bool = False):  # noqa
    try:
        vals = vars(o).values()
    except TypeError:
        vals = o.values()
    for x in vals:
        try:
            if issubclass(x, parent) and x is not parent:
                return x
        except TypeError:
            pass
    else:
        if base_ok:
            return parent
        else:
            raise RuntimeError


def reset_subclasses() -> None:
    """
    Reset registered subclasses between test classes.

    Needed because these classes are meant to only be subclassed once and raise
    exceptions otherwise.
    """
    BaseSession.reset_subclass()
    BaseGateway.reset_subclass()
    LegacyRoster.reset_subclass()
    LegacyContact.reset_subclass()
    LegacyMUC.reset_subclass()
    LegacyBookmarks.reset_subclass()
    LegacyParticipant.reset_subclass()
    # reset_commands()


def reset_commands() -> None:
    Command.subclasses = [
        c for c in Command.subclasses if str(c).startswith("<class 'slidge.core")
    ]
