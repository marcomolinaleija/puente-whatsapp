"""
This module extends slixmpp.ComponentXMPP to make writing new LegacyClients easier
"""

import abc
import asyncio
import contextlib
import logging
import re
import tempfile
from collections.abc import Callable, Sequence
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Concatenate, Generic, ParamSpec, TypeVar, cast

import aiohttp
import qrcode
from slixmpp import JID, ComponentXMPP, Iq
from slixmpp.exceptions import IqError, IqTimeout, XMPPError
from slixmpp.plugins.xep_0004.stanza.form import Form
from slixmpp.plugins.xep_0060.stanza import OwnerAffiliation
from slixmpp.plugins.xep_0356.privilege import PrivilegedIqError
from slixmpp.types import MessageTypes
from slixmpp.xmlstream.xmlstream import NotConnectedError
from sqlalchemy.orm import Session as OrmSession

import slidge.command.categories
from slidge.command.adhoc import AdhocProvider
from slidge.command.admin import Exec
from slidge.command.base import Command, FormField
from slidge.command.chat_command import ChatCommandProvider
from slidge.command.register import RegistrationType
from slidge.contact import LegacyContact
from slidge.core import config
from slidge.core.dispatcher.session_dispatcher import SessionDispatcher
from slidge.core.mixins.avatar import convert_avatar
from slidge.core.mixins.message import MessageMixin
from slidge.core.pubsub import PubSubComponent
from slidge.db import GatewayUser, SlidgeStore
from slidge.db.avatar import CachedAvatar, avatar_cache
from slidge.db.meta import JSONSerializable
from slidge.slixfix.delivery_receipt import DeliveryReceipt
from slidge.slixfix.roster import RosterBackend
from slidge.util import SubclassableOnce
from slidge.util.types import AnyGateway, Avatar, MessageOrPresenceTypeVar, SessionType

T = TypeVar("T")
P = ParamSpec("P")


class BaseGateway(
    ComponentXMPP,
    MessageMixin,
    SubclassableOnce,
    Generic[SessionType],
    abc.ABC,
):
    """
    The gateway component, handling registrations and un-registrations.

    On slidge launch, a singleton is instantiated, and it will be made available
    to public classes such :class:`.LegacyContact` or :class:`.BaseSession` as the
    ``.xmpp`` attribute.

    Must be subclassed by a legacy module to set up various aspects of the XMPP
    component behaviour, such as its display name or welcome message, via
    class attributes :attr:`.COMPONENT_NAME` :attr:`.WELCOME_MESSAGE`.

    Abstract methods related to the registration process must be overriden
    for a functional :term:`Legacy Module`:

    - :meth:`.validate`
    - :meth:`.validate_two_factor_code`
    - :meth:`.get_qr_text`
    - :meth:`.confirm_qr`

    NB: Not all of these must be overridden, it depends on the
    :attr:`REGISTRATION_TYPE`.

    The other methods, such as :meth:`.send_text` or :meth:`.react` are the same
    as those of :class:`.LegacyContact` and :class:`.LegacyParticipant`, because
    the component itself is also a "messaging actor", ie, an :term:`XMPP Entity`.
    For these methods, you need to specify the JID of the recipient with the
    `mto` parameter.

    Since it inherits from :class:`slixmpp.componentxmpp.ComponentXMPP`,you also
    have a hand on low-level XMPP interactions via slixmpp methods, e.g.:

    .. code-block:: python

        self.send_presence(
            pfrom="somebody@component.example.com",
            pto="someonwelse@anotherexample.com",
        )

    However, you should not need to do so often since the classes of the plugin
    API provides higher level abstractions around most commonly needed use-cases, such
    as sending messages, or displaying a custom status.

    """

    COMPONENT_NAME: str = NotImplemented
    """Name of the component, as seen in service discovery by XMPP clients"""
    COMPONENT_TYPE: str = ""
    """Type of the gateway, should follow https://xmpp.org/registrar/disco-categories.html"""
    COMPONENT_AVATAR: Avatar | Path | str | None = None
    """
    Path, bytes or URL used by the component as an avatar.
    """

    REGISTRATION_FIELDS: Sequence[FormField] = [
        FormField(var="username", label="User name", required=True),
        FormField(var="password", label="Password", required=True, private=True),
    ]
    """
    Iterable of fields presented to the gateway user when registering using :xep:`0077`
    `extended <https://xmpp.org/extensions/xep-0077.html#extensibility>`_ by :xep:`0004`.
    """
    REGISTRATION_INSTRUCTIONS: str = "Enter your credentials"
    """
    The text presented to a user who wants to register (or modify) their
    :term:`legacy <Legacy>` account configuration.
    """
    REGISTRATION_TYPE: RegistrationType = RegistrationType.SINGLE_STEP_FORM
    """
    This attribute determines how users register to the gateway, ie, how they
    login to the :term:`legacy network <Legacy Network>`.
    The credentials are then stored persistently, so this process should happen
    once per user (unless they unregister).

    The registration process always start with a basic data form (:xep:`0004`)
    presented to the user.
    But the legacy login flow might require something more sophisticated, see
    :class:`.RegistrationType` for more details.
    """

    REGISTRATION_2FA_TITLE = "Enter your 2FA code"
    REGISTRATION_2FA_INSTRUCTIONS = (
        "You should have received something via email or SMS, or something"
    )
    REGISTRATION_QR_INSTRUCTIONS = "Flash this code or follow this link"

    PREFERENCES: ClassVar[list[FormField]] = [
        FormField(
            var="sync_presence",
            label="Propagate your XMPP presence to the legacy network.",
            value="true",
            required=True,
            type="boolean",
        ),
        FormField(
            var="sync_avatar",
            label="Propagate your XMPP avatar to the legacy network.",
            value="true",
            required=True,
            type="boolean",
        ),
        FormField(
            var="always_invite_when_adding_bookmarks",
            label="Send an invitation to join MUCs after adding them to the bookmarks.",
            value="true",
            required=True,
            type="boolean",
        ),
        FormField(
            var="last_seen_fallback",
            label="Use contact presence status message to show when they were last seen.",
            value="true",
            required=True,
            type="boolean",
        ),
        FormField(
            var="roster_push",
            label="Add contacts to your roster.",
            value="true",
            required=True,
            type="boolean",
        ),
        FormField(
            var="reaction_fallback",
            label="Receive fallback messages for reactions (for legacy XMPP clients)",
            value="false",
            required=True,
            type="boolean",
        ),
    ]

    ROSTER_GROUP: str = "slidge"
    """
    Name of the group assigned to a :class:`.LegacyContact` automagically
    added to the :term:`User`'s roster with :meth:`.LegacyContact.add_to_roster`.
    """
    WELCOME_MESSAGE = (
        "Thank you for registering. Type 'help' to list the available commands, "
        "or just start messaging away!"
    )
    """
    A welcome message displayed to users on registration.
    This is useful notably for clients that don't consider component JIDs as a
    valid recipient in their UI, yet still open a functional chat window on
    incoming messages from components.
    """

    SEARCH_FIELDS: Sequence[FormField] = [
        FormField(var="first", label="First name", required=True),
        FormField(var="last", label="Last name", required=True),
        FormField(var="phone", label="Phone number", required=False),
    ]
    """
    Fields used for searching items via the component, through :xep:`0055` (jabber search).
    A common use case is to allow users to search for legacy contacts by something else than
    their usernames, eg their phone number.

    Plugins should implement search by overriding :meth:`.BaseSession.search`
    (restricted to registered users).

    If there is only one field, it can also be used via the ``jabber:iq:gateway`` protocol
    described in :xep:`0100`. Limitation: this only works if the search request returns
    one result item, and if this item has a 'jid' var.
    """
    SEARCH_TITLE: str = "Search for legacy contacts"
    """
    Title of the search form.
    """
    SEARCH_INSTRUCTIONS: str = ""
    """
    Instructions of the search form.
    """

    MARK_ALL_MESSAGES = False
    """
    Set this to True for :term:`legacy networks <Legacy Network>` that expects
    read marks for *all* messages and not just the latest one that was read
    (as most XMPP clients will only send a read mark for the latest msg).
    """

    PROPER_RECEIPTS = False
    """
    Set this to True if the legacy service provides a real equivalent of message delivery receipts
    (:xep:`0184`), meaning that there is an event thrown when the actual device of a contact receives
    a message. Make sure to call Contact.received() adequately if this is set to True.
    """

    GROUPS = False
    """
    This must be set to True if this gateway supports groups.
    """
    SPACES = False
    """
    This must be set to True if this gateway supports spaces, cf :xep:`0503`.
    """

    mtype: MessageTypes = "chat"
    is_group = False
    _can_send_carbon = False
    store: SlidgeStore
    _session_cls: type[SessionType]

    http: aiohttp.ClientSession
    avatar: CachedAvatar | None = None

    def __init__(self) -> None:
        if config.COMPONENT_NAME:
            self.COMPONENT_NAME = config.COMPONENT_NAME
        if config.WELCOME_MESSAGE:
            self.WELCOME_MESSAGE = config.WELCOME_MESSAGE
        self.log = log
        self.datetime_started = datetime.now()
        # FIXME: ugly hack to work with the BaseSender mixin :/
        self.xmpp = cast("AnyGateway", self)
        self.default_ns = "jabber:component:accept"
        super().__init__(
            config.JID,
            config.SECRET,
            config.SERVER,
            config.PORT,
            plugin_whitelist=SLIXMPP_PLUGINS,
            plugin_config={
                "xep_0077": {
                    "form_fields": None,
                    "form_instructions": self.REGISTRATION_INSTRUCTIONS,
                    "enable_subscription": self.REGISTRATION_TYPE
                    == RegistrationType.SINGLE_STEP_FORM,
                },
                "xep_0100": {
                    "component_name": self.COMPONENT_NAME,
                    "type": self.COMPONENT_TYPE,
                },
                "xep_0184": {
                    "auto_ack": False,
                    "auto_request": False,
                },
                "xep_0363": {
                    "upload_service": config.UPLOAD_SERVICE,
                },
            },
            fix_error_ns=True,
        )
        self.loop.set_exception_handler(self.__exception_handler)
        self.loop.create_task(self.__set_http())
        self.has_crashed: bool = False
        self.use_origin_id = False

        if config.USER_JID_VALIDATOR is None:
            config.USER_JID_VALIDATOR = f".*@{self.infer_real_domain()}"
            log.info(
                "No USER_JID_VALIDATOR was set, using '%s'.",
                config.USER_JID_VALIDATOR,
            )
        self.jid_validator: re.Pattern[str] = re.compile(config.USER_JID_VALIDATOR)
        self.qr_pending_registrations = dict[
            str, asyncio.Future[JSONSerializable | None]
        ]()

        self.register_plugins()
        self.__setup_legacy_module_subclasses()

        self.get_session_from_stanza = self._session_cls.from_stanza
        self.get_session_from_user = self._session_cls.from_user

        self.__register_slixmpp_events()
        self.__register_slixmpp_api()
        self.roster.set_backend(RosterBackend(self))

        self.register_plugin("pubsub", {"component_name": self.COMPONENT_NAME})
        self.pubsub: PubSubComponent = self.plugin["pubsub"]  # type:ignore[typeddict-item]
        self.delivery_receipt = DeliveryReceipt(self)

        # with this we receive user avatar updates
        self.plugin["xep_0030"].add_feature("urn:xmpp:avatar:metadata+notify")

        self.plugin["xep_0030"].add_feature("urn:xmpp:chat-markers:0")

        if self.GROUPS:
            self.plugin["xep_0030"].add_feature("http://jabber.org/protocol/muc")
            self.plugin["xep_0030"].add_feature(self.plugin["xep_0463"].stanza.NS)
            self.plugin["xep_0030"].add_feature("urn:xmpp:mam:2")
            self.plugin["xep_0030"].add_feature("urn:xmpp:mam:2#extended")
            self.plugin["xep_0030"].add_feature(self.plugin["xep_0421"].namespace)
            self.plugin["xep_0030"].add_feature(self.plugin["xep_0317"].stanza.NS)
            self.plugin["xep_0030"].add_identity(
                category="conference",
                name=self.COMPONENT_NAME,
                itype="text",
                jid=self.boundjid,
            )
        if self.SPACES:
            self.plugin["xep_0030"].add_feature("urn:xmpp:spaces:0")

        self.__adhoc_handler = AdhocProvider(self)
        self.__chat_commands_handler = ChatCommandProvider(self)

        self.__dispatcher = SessionDispatcher(self)

        self.__register_commands()

        MessageMixin.__init__(self)  # ComponentXMPP does not call super().__init__()

    def __setup_legacy_module_subclasses(self) -> None:
        from ..contact.roster import LegacyRoster
        from ..group.bookmarks import LegacyBookmarks
        from ..group.participant import LegacyParticipant
        from ..group.room import LegacyMUC
        from .session import BaseSession

        self._session_cls = BaseSession.get_unique_subclass()  # type:ignore
        contact_cls = LegacyContact.get_self_or_unique_subclass()
        muc_cls = LegacyMUC.get_self_or_unique_subclass()
        participant_cls = LegacyParticipant.get_self_or_unique_subclass()
        bookmarks_cls = LegacyBookmarks.get_self_or_unique_subclass()
        roster_cls = LegacyRoster.get_self_or_unique_subclass()

        if contact_cls.REACTIONS_SINGLE_EMOJI:  # type:ignore[attr-defined]
            form = Form()
            form["type"] = "result"
            form.add_field(
                "FORM_TYPE", "hidden", value="urn:xmpp:reactions:0:restrictions"
            )
            form.add_field("max_reactions_per_user", value="1", type="text-single")
            form.add_field("scope", value="domain")
            self.plugin["xep_0128"].add_extended_info(data=form)

        self._session_cls.xmpp = self
        contact_cls.xmpp = self  # type:ignore[attr-defined]
        muc_cls.xmpp = self  # type:ignore[attr-defined]

        self._session_cls._bookmarks_cls = bookmarks_cls  # type:ignore[assignment]
        self._session_cls._roster_cls = roster_cls  # type:ignore[assignment]
        LegacyRoster._contact_cls = contact_cls  # type:ignore[misc]
        LegacyBookmarks._muc_cls = muc_cls  # type:ignore[misc]
        LegacyMUC._participant_cls = participant_cls  # type:ignore[misc]

    async def kill_session(self, jid: JID) -> None:
        await self._session_cls.kill_by_jid(jid)

    async def __set_http(self) -> None:
        self.http = aiohttp.ClientSession()
        if getattr(self, "_test_mode", False):
            return
        avatar_cache.http = self.http

    def __register_commands(self) -> None:
        for cls in Command.subclasses:  # type:ignore[misc]
            if any(x is NotImplemented for x in [cls.CHAT_COMMAND, cls.NODE, cls.NAME]):
                log.debug("Not adding command '%s' because it looks abstract", cls)
                continue
            if cls is Exec:
                if config.DEV_MODE:
                    log.warning(r"/!\ DEV MODE ENABLED /!\\")
                else:
                    continue
            if cls.CATEGORY == slidge.command.categories.GROUPS and not self.GROUPS:
                continue
            if cls.CATEGORY == slidge.command.categories.SPACES and not self.SPACES:
                continue
            c = cls(cast(AnyGateway, self))
            log.debug("Registering %s", cls)
            self.__adhoc_handler.register(c)
            self.__chat_commands_handler.register(c)

    def __exception_handler(
        self, loop: asyncio.AbstractEventLoop, context: dict[Any, Any]
    ) -> None:
        """
        Called when a task created by loop.create_task() raises an Exception

        :param loop:
        :param context:
        :return:
        """
        log.debug("Context in the exception handler: %s", context)
        exc = context.get("exception")
        if exc is None:
            log.debug("No exception in this context: %s", context)
        elif isinstance(exc, SystemExit):
            log.debug("SystemExit called in an asyncio task")
        else:
            log.error("Crash in an asyncio task: %s", context)
            log.exception("Crash in task", exc_info=exc)
            self.has_crashed = True
            loop.stop()

    def __register_slixmpp_events(self) -> None:
        self.del_event_handler("presence_subscribe", self._handle_subscribe)
        self.del_event_handler("presence_unsubscribe", self._handle_unsubscribe)
        self.del_event_handler("presence_subscribed", self._handle_subscribed)
        self.del_event_handler("presence_unsubscribed", self._handle_unsubscribed)
        self.del_event_handler(
            "roster_subscription_request", self._handle_new_subscription
        )
        self.del_event_handler("presence_probe", self._handle_probe)
        self.add_event_handler("session_start", self.__on_session_start)

    def __register_slixmpp_api(self) -> None:
        def with_session(
            func: Callable[Concatenate[OrmSession, P], T], commit: bool = True
        ) -> Callable[P, T]:
            def wrapped(*a: P.args, **kw: P.kwargs) -> T:
                with self.store.session() as orm:
                    res = func(orm, *a, **kw)
                    if commit:
                        orm.commit()
                    return res

            return wrapped

        self.plugin["xep_0231"].api.register(
            with_session(self.store.bob.get_bob, False), "get_bob"
        )
        self.plugin["xep_0231"].api.register(
            with_session(self.store.bob.set_bob), "set_bob"
        )
        self.plugin["xep_0231"].api.register(
            with_session(self.store.bob.del_bob), "del_bob"
        )

    @property  # type:ignore[override]
    def jid(self) -> JID:
        # Override to avoid slixmpp deprecation warnings.
        return self.boundjid

    @jid.setter
    def jid(self, jid: JID) -> None:
        raise RuntimeError

    async def __on_session_start(self, event: object) -> None:
        log.debug("Gateway session start: %s", event)

        await self.__setup_attachments()

        # prevents XMPP clients from considering the gateway as an HTTP upload
        disco = self.plugin["xep_0030"]
        await disco.del_feature(feature="urn:xmpp:http:upload:0", jid=self.boundjid)
        await self.plugin["xep_0115"].update_caps(jid=self.boundjid)

        if self.COMPONENT_AVATAR is not None:
            log.debug("Setting gateway avatar…")
            avatar = convert_avatar(self.COMPONENT_AVATAR, "!!---slidge---special---")
            assert avatar is not None
            try:
                cached_avatar = await avatar_cache.convert_or_get(avatar)
            except Exception as e:
                log.exception("Could not set the component avatar.", exc_info=e)
                cached_avatar = None
            else:
                assert cached_avatar is not None
                self.avatar = cached_avatar
        else:
            cached_avatar = None

        with self.store.session() as orm:
            users = orm.query(GatewayUser).all()
        for user in users:
            # TODO: before this, we should check if the user has removed us from their roster
            #       while we were offline and trigger unregister from there. Presence probe does not seem
            #       to work in this case, there must be another way. privileged entity could be used
            #       as last resort.
            try:
                await self["xep_0100"].add_component_to_roster(user.jid)
                await self.__add_component_to_mds_whitelist(user.jid)
            except (IqError, IqTimeout) as e:
                # TODO: remove the user when this happens? or at least
                # this can happen when the user has unsubscribed from the XMPP server
                log.warning(
                    "Error with user %s, not logging them automatically",
                    user,
                    exc_info=e,
                )
                continue
            session = self._session_cls.from_user(user)
            session.create_task(self.login_wrap(session))
            if cached_avatar is not None:
                await self.pubsub.broadcast_avatar(
                    self.boundjid.bare, session.user_jid, cached_avatar
                )

        log.info("Slidge has successfully started")

    async def __setup_attachments(self) -> None:
        if config.NO_UPLOAD_PATH:
            if config.NO_UPLOAD_URL_PREFIX is None:
                raise RuntimeError(
                    "If you set NO_UPLOAD_PATH you must set NO_UPLOAD_URL_PREFIX too."
                )
        elif not config.UPLOAD_SERVICE:
            try:
                info_iq = await self.xmpp.plugin["xep_0363"].find_upload_service(
                    self.infer_real_domain()
                )
            except XMPPError:
                info_iq = None
                log.exception(
                    "The upload service could not be automatically determine. "
                    "Attachments to XMPP will not work. "
                    "Either specify 'upload-service' or 'no-upload-path' to fix that."
                )
            if info_iq is None:
                if self.REGISTRATION_TYPE == RegistrationType.QRCODE:
                    log.warning(
                        "No method was configured for attachment and slidge "
                        "could not automatically determine the JID of a usable upload service. "
                        "Users likely won't be able to register since this network uses a "
                        "QR-code based registration flow."
                    )
                if not config.USE_ATTACHMENT_ORIGINAL_URLS:
                    log.warning(
                        "Setting USE_ATTACHMENT_ORIGINAL_URLS to True since no method was configured "
                        "for attachments and no upload service was found. NB: this does not work for all "
                        "networks, especially for the E2EE attachments."
                    )
                    config.USE_ATTACHMENT_ORIGINAL_URLS = True
            else:
                log.info("Auto-discovered upload service: %s", info_iq["from"])
                config.UPLOAD_SERVICE = info_iq["from"]

    def infer_real_domain(self) -> JID:
        return JID(re.sub(r"^.*?\.", "", self.xmpp.boundjid.bare))

    async def __add_component_to_mds_whitelist(self, user_jid: JID) -> None:
        # Uses privileged entity to add ourselves to the whitelist of the PEP
        # MDS node so we receive MDS events
        iq_creation = Iq(sto=user_jid.bare, sfrom=user_jid, stype="set")
        iq_creation["pubsub"]["create"]["node"] = self.plugin["xep_0490"].stanza.NS

        try:
            await self.plugin["xep_0356"].send_privileged_iq(iq_creation)
        except PermissionError:
            log.warning(
                "IQ privileges not granted for pubsub namespace, we cannot "
                "create the MDS node of %s",
                user_jid,
            )
        except PrivilegedIqError as exc:
            nested = exc.nested_error()
            # conflict this means the node already exists, we can ignore that
            if nested is not None and nested.condition != "conflict":
                log.exception(
                    "Could not create the MDS node of %s", user_jid, exc_info=exc
                )
        except Exception as e:
            log.exception(
                "Error while trying to create to the MDS node of %s",
                user_jid,
                exc_info=e,
            )

        iq_affiliation = Iq(sto=user_jid.bare, sfrom=user_jid, stype="set")
        iq_affiliation["pubsub_owner"]["affiliations"]["node"] = self.plugin[
            "xep_0490"
        ].stanza.NS

        aff = OwnerAffiliation()
        aff["jid"] = self.boundjid.bare
        aff["affiliation"] = "member"
        iq_affiliation["pubsub_owner"]["affiliations"].append(aff)

        try:
            await self.plugin["xep_0356"].send_privileged_iq(iq_affiliation)
        except PermissionError:
            log.warning(
                "IQ privileges not granted for pubsub#owner namespace, we cannot "
                "listen to the MDS events of %s",
                user_jid,
            )
        except Exception as e:
            log.exception(
                "Error while trying to subscribe to the MDS node of %s",
                user_jid,
                exc_info=e,
            )

    async def login_wrap(self, session: SessionType) -> str:
        session.send_gateway_status("Logging in…", show="dnd")
        session.is_logging_in = True
        try:
            status = await session.login()
        except Exception as e:
            log.warning("Login problem for %s", session.user_jid, exc_info=e)
            session.send_gateway_status(f"Could not login: {e}", show="dnd")
            msg = (
                "You are not connected to this gateway! "
                f"Maybe this message will tell you why: {e}"
            )
            session.send_gateway_message(msg)
            session.logged = False
            session.send_gateway_status("Login failed", show="dnd")
            return msg

        log.info("Login success for %s", session.user_jid)
        session.logged = True
        session.send_gateway_status("Syncing contacts…", show="dnd")
        with self.store.session() as orm:
            await session.contacts._fill(orm)
        if not (r := session.contacts.ready).done():
            r.set_result(True)
        if self.GROUPS:
            session.send_gateway_status("Syncing groups…", show="dnd")
            await session.bookmarks.fill()
            if not (r := session.bookmarks.ready).done():
                r.set_result(True)
        self.send_presence(pto=session.user.jid.bare, ptype="probe")
        if status is None:
            status = "Logged in"
        session.send_gateway_status(status, show="chat")

        if session.user.preferences.get("sync_avatar", False):
            session.create_task(self.fetch_user_avatar(session))
        else:
            with self.store.session(expire_on_commit=False) as orm:
                session.user.avatar_hash = None
                orm.add(session.user)
                orm.commit()
        return status

    async def fetch_user_avatar(self, session: SessionType) -> None:
        try:
            iq = await self.xmpp.plugin["xep_0060"].get_items(
                session.user_jid.bare,
                self.xmpp.plugin["xep_0084"].stanza.MetaData.namespace,
                ifrom=self.boundjid.bare,
            )
        except IqTimeout:
            self.log.warning("Iq timeout trying to fetch user avatar")
            return
        except IqError as e:
            self.log.debug("Iq error when trying to fetch user avatar: %s", e)
            if e.condition == "item-not-found":
                try:
                    await session.on_avatar(None, None, None, None, None)
                except NotImplementedError:
                    pass
                else:
                    with self.store.session(expire_on_commit=False) as orm:
                        session.user.avatar_hash = None
                        orm.add(session.user)
                        orm.commit()
            return
        await self.__dispatcher.on_avatar_metadata_info(
            session, iq["pubsub"]["items"]["item"]["avatar_metadata"]["info"]
        )

    def _send(
        self,
        stanza: MessageOrPresenceTypeVar,
        **send_kwargs: Any,  # noqa:ANN401
    ) -> MessageOrPresenceTypeVar:
        stanza.set_from(self.boundjid.bare)
        if mto := send_kwargs.get("mto"):
            stanza.set_to(mto)
        stanza.send()
        return stanza

    def raise_if_not_allowed_jid(self, jid: JID) -> None:
        if not self.jid_validator.match(jid.bare):
            raise XMPPError(
                condition="not-allowed",
                text="Your account is not allowed to use this gateway. "
                "The admin controls that with the USER_JID_VALIDATOR option.",
            )

    def send_raw(self, data: str | bytes) -> None:
        # overridden from XMLStream to strip base64-encoded data from the logs
        # to make them more readable.
        if log.isEnabledFor(level=logging.DEBUG):
            stripped = copy(data) if isinstance(data, str) else data.decode("utf-8")
            # there is probably a way to do that in a single RE,
            # but since it's only for debugging, the perf penalty
            # does not matter much
            for el in LOG_STRIP_ELEMENTS:
                stripped = re.sub(
                    f"(<{el}.*?>)(.*)(</{el}>)",
                    "\1[STRIPPED]\3",
                    stripped,
                    flags=re.DOTALL | re.IGNORECASE,
                )
            log.debug("SEND: %s", stripped)
        if not self.transport:
            raise NotConnectedError()
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.transport.write(data)

    def get_session_from_jid(self, j: JID) -> SessionType | None:
        try:
            return self._session_cls.from_jid(j)
        except XMPPError:
            return None

    def exception(self, exception: Exception) -> None:
        # """
        # Called when a task created by slixmpp's internal (eg, on slix events) raises an Exception.
        #
        # Stop the event loop and exit on unhandled exception.
        #
        # The default :class:`slixmpp.basexmpp.BaseXMPP` behaviour is just to
        # log the exception, but we want to avoid undefined behaviour.
        #
        # :param exception: An unhandled :class:`Exception` object.
        # """
        if isinstance(exception, IqError):
            iq = exception.iq
            log.error("%s: %s", iq["error"]["condition"], iq["error"]["text"])
            log.warning("You should catch IqError exceptions")
        elif isinstance(exception, IqTimeout):
            iq = exception.iq
            log.error("Request timed out: %s", iq)
            log.warning("You should catch IqTimeout exceptions")
        elif isinstance(exception, SyntaxError):
            # Hide stream parsing errors that occur when the
            # stream is disconnected (they've been handled, we
            # don't need to make a mess in the logs).
            pass
        else:
            if exception:
                log.exception(exception)
            self.loop.stop()
            exit(1)

    async def make_registration_form(
        self, _jid: JID, _node: str, _ifrom: JID, iq: Iq
    ) -> Iq:
        self.raise_if_not_allowed_jid(iq.get_from())
        reg = iq["register"]
        with self.store.session() as orm:
            user = (
                orm.query(GatewayUser).filter_by(jid=iq.get_from().bare).one_or_none()
            )
        log.debug("User found: %s", user)

        form = reg["form"]
        form.add_field(
            "FORM_TYPE",
            ftype="hidden",
            value="jabber:iq:register",
        )
        form["title"] = f"Registration to '{self.COMPONENT_NAME}'"
        form["instructions"] = self.REGISTRATION_INSTRUCTIONS

        if user is not None:
            reg["registered"] = False
            form.add_field(
                "remove",
                label="Remove my registration",
                required=True,
                ftype="boolean",
                value=False,
            )

        for field in self.REGISTRATION_FIELDS:
            if field.var in reg.interfaces:
                val = None if user is None else user.get(field.var)
                if val is None:
                    reg.add_field(field.var)
                else:
                    reg[field.var] = val

        reg["instructions"] = self.REGISTRATION_INSTRUCTIONS

        for field in self.REGISTRATION_FIELDS:
            form.add_field(
                field.var,
                label=field.label,
                required=field.required,
                ftype=field.type,
                options=field.options,
                value=field.value if user is None else user.get(field.var, field.value),
            )

        reply = iq.reply()
        reply.set_payload(reg)
        return reply  # type:ignore[no-any-return]

    async def user_prevalidate(
        self, ifrom: JID, form_dict: dict[str, str | None]
    ) -> JSONSerializable | None:
        # Pre validate a registration form using the content of self.REGISTRATION_FIELDS
        # before passing it to the plugin custom validation logic.
        for field in self.REGISTRATION_FIELDS:
            if field.required and not form_dict.get(field.var):
                raise ValueError(f"Missing field: '{field.label}'")

        return await self.validate(ifrom, form_dict)

    @abc.abstractmethod
    async def validate(
        self, user_jid: JID, registration_form: dict[str, str | None]
    ) -> JSONSerializable | None:
        """
        Validate a user's initial registration form.

        Should raise the appropriate :class:`slixmpp.exceptions.XMPPError`
        if the registration does not allow to continue the registration process.

        If :py:attr:`REGISTRATION_TYPE` is a
        :attr:`.RegistrationType.SINGLE_STEP_FORM`,
        this method should raise something if it wasn't possible to successfully
        log in to the legacy service with the registration form content.

        It is also used for other types of :py:attr:`REGISTRATION_TYPE` too, since
        the first step is always a form. If :attr:`.REGISTRATION_FIELDS` is an
        empty list (ie, it declares no :class:`.FormField`), the "form" is
        effectively a confirmation dialog displaying
        :attr:`.REGISTRATION_INSTRUCTIONS`.

        :param user_jid: JID of the user that has just registered
        :param registration_form: A dict where keys are the :attr:`.FormField.var` attributes
            of the :attr:`.BaseGateway.REGISTRATION_FIELDS` iterable.
            This dict can be modified and will be accessible as the ``legacy_module_data``
            of the

        :return : A dict that will be stored as the persistent "legacy_module_data"
            for this user. If you don't return anything here, the whole registration_form
            content will be stored.
        """
        raise NotImplementedError

    async def validate_two_factor_code(
        self, user: GatewayUser, code: str
    ) -> JSONSerializable | None:
        """
        Called when the user enters their 2FA code.

        Should raise the appropriate :class:`slixmpp.exceptions.XMPPError`
        if the login fails, and return successfully otherwise.

        Only used when :attr:`REGISTRATION_TYPE` is
        :attr:`.RegistrationType.TWO_FACTOR_CODE`.

        :param user: The :class:`.GatewayUser` whose registration is pending
            Use their :attr:`.GatewayUser.bare_jid` and/or
            :attr:`.registration_form` attributes to get what you need.
        :param code: The code they entered, either via "chatbot" message or
            adhoc command

        :return : A dict which keys and values will be added to the persistent "legacy_module_data"
            for this user.
        """
        raise NotImplementedError

    async def get_qr_text(self, user: GatewayUser) -> str:
        """
        This is where slidge gets the QR code content for the QR-based
        registration process. It will turn it into a QR code image and send it
        to the not-yet-fully-registered :class:`.GatewayUser`.

        Only used in when :attr:`BaseGateway.REGISTRATION_TYPE` is
        :attr:`.RegistrationType.QRCODE`.

        :param user: The :class:`.GatewayUser` whose registration is pending
            Use their :attr:`.GatewayUser.bare_jid` and/or
            :attr:`.registration_form` attributes to get what you need.
        """
        raise NotImplementedError

    async def confirm_qr(
        self,
        user_bare_jid: str,
        exception: Exception | None = None,
        legacy_data: JSONSerializable | None = None,
    ) -> None:
        """
        This method is meant to be called to finalize QR code-based registration
        flows, once the legacy service confirms the QR flashing.

        Only used in when :attr:`BaseGateway.REGISTRATION_TYPE` is
        :attr:`.RegistrationType.QRCODE`.

        :param user_bare_jid: The bare JID of the almost-registered
            :class:`GatewayUser` instance
        :param exception: Optionally, an XMPPError to be raised to **not** confirm
            QR code flashing.
        :param legacy_data: dict which keys and values will be added to the persistent
            "legacy_module_data" for this user.
        """
        fut = self.qr_pending_registrations[user_bare_jid]
        if exception is None:
            fut.set_result(legacy_data)
        else:
            fut.set_exception(exception)

    async def unregister_user(
        self, user: GatewayUser, msg: str = "You unregistered from this gateway."
    ) -> None:
        self.send_presence(pshow="dnd", pstatus=msg, pto=user.jid)
        await self.xmpp.plugin["xep_0077"].api["user_remove"](None, None, user.jid)  # type:ignore[call-arg]
        await self.xmpp._session_cls.kill_by_jid(user.jid)

    async def unregister(self, session: SessionType) -> None:
        """
        Optionally override this if you need to clean additional
        stuff after a user has been removed from the persistent user store.

        By default, this just calls :meth:`BaseSession.logout`.

        :param session: The session of the user who just unregistered
        """
        with contextlib.suppress(NotImplementedError):
            await session.logout()

    async def input(
        self,
        jid: JID,
        text: str | None = None,
        mtype: MessageTypes = "chat",
        **input_kwargs: Any,  # noqa:ANN401
    ) -> str:
        """
        Request arbitrary user input using a simple chat message, and await the result.

        You shouldn't need to call this directly bust instead use
        :meth:`.BaseSession.input` to directly target a user.

        :param jid: The JID we want input from
        :param text: A prompt to display for the user
        :param mtype: Message type
        :return: The user's reply
        """
        return await self.__chat_commands_handler.input(
            jid, text, mtype=mtype, **input_kwargs
        )

    async def send_qr(
        self,
        text: str,
        **msg_kwargs: Any,  # noqa:ANN401
    ) -> None:
        """
        Sends a QR Code to a JID

        You shouldn't need to call directly bust instead use
        :meth:`.BaseSession.send_qr` to directly target a user.

        :param text: The text that will be converted to a QR Code
        :param msg_kwargs: Optional additional arguments to pass to
            :meth:`.BaseGateway.send_file`, such as the recipient of the QR,
            code
        """
        qr = qrcode.make(text)
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            qr.save(f.name)
            await self.send_file(Path(f.name), **msg_kwargs)

    def shutdown(self) -> list[asyncio.Task[None]]:
        # """
        # Called by the slidge entrypoint on normal exit.
        #
        # Sends offline presences from all contacts of all user sessions and from
        # the gateway component itself.
        # No need to call this manually, :func:`slidge.__main__.main` should take care of it.
        # """
        log.debug("Shutting down")
        tasks = []
        with self.store.session() as orm:
            for user in orm.query(GatewayUser).all():
                tasks.append(self._session_cls.from_jid(user.jid).shutdown())
                self.send_presence(ptype="unavailable", pto=user.jid)
        return tasks


SLIXMPP_PLUGINS = [
    "xep_0030",  # Service discovery
    "xep_0045",  # Multi-User Chat
    "xep_0050",  # Adhoc commands
    "xep_0054",  # VCard-temp (for MUC avatars)
    "xep_0055",  # Jabber search
    "xep_0059",  # Result Set Management
    "xep_0066",  # Out of Band Data
    "xep_0071",  # XHTML-IM (for stickers and custom emojis maybe later)
    "xep_0077",  # In-band registration
    "xep_0084",  # User Avatar
    "xep_0085",  # Chat state notifications
    "xep_0100",  # Gateway interaction
    "xep_0106",  # JID Escaping
    "xep_0115",  # Entity capabilities
    "xep_0122",  # Data Forms Validation
    "xep_0128",  # Service Discovery Extensions
    "xep_0153",  # vCard-Based Avatars (for MUC avatars)
    "xep_0172",  # User nickname
    "xep_0184",  # Message Delivery Receipts
    "xep_0199",  # XMPP Ping
    "xep_0221",  # Data Forms Media Element
    "xep_0231",  # Bits of Binary (for stickers and custom emojis maybe later)
    "xep_0249",  # Direct MUC Invitations
    "xep_0264",  # Jingle Content Thumbnails
    "xep_0280",  # Carbons
    "xep_0292_provider",  # VCard4
    "xep_0308",  # Last message correction
    "xep_0313",  # Message Archive Management
    "xep_0317",  # Hats
    "xep_0319",  # Last User Interaction in Presence
    "xep_0333",  # Chat markers
    "xep_0334",  # Message Processing Hints
    "xep_0356",  # Privileged Entity
    "xep_0363",  # HTTP file upload
    "xep_0385",  # Stateless in-line media sharing
    "xep_0402",  # PEP Native Bookmarks
    "xep_0421",  # Anonymous unique occupant identifiers for MUCs
    "xep_0424",  # Message retraction
    "xep_0425",  # Message moderation
    "xep_0444",  # Message reactions
    "xep_0447",  # Stateless File Sharing
    "xep_0449",  # Stickers
    "xep_0461",  # Message replies
    "xep_0462",  # Pubsub Type Filtering
    "xep_0463",  # MUC Affiliation Versioning
    "xep_0469",  # Bookmark Pinning
    "xep_0490",  # Message Displayed Synchronization
    "xep_0492",  # Chat Notification Settings
    # "xep_0503",  # Server-side spaces
    "xep_0511",  # Link Metadata
]

LOG_STRIP_ELEMENTS = ["data", "binval"]

log = logging.getLogger(__name__)
