import logging
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from slixmpp import Message
from slixmpp.exceptions import IqError, IqTimeout
from slixmpp.plugins.xep_0084.stanza import Info

from slidge.util.types import AnySession

from .caps import CapsMixin
from .disco import DiscoMixin
from .message import MessageMixin
from .muc import MucMixin
from .presence import PresenceHandlerMixin
from .pubsub import PubSubMixin
from .registration import RegistrationMixin
from .search import SearchMixin
from .util import exceptions_to_xmpp_errors
from .vcard import VCardMixin

if TYPE_CHECKING:
    from slidge.core.gateway import BaseGateway

GatewayType = TypeVar("GatewayType", bound="BaseGateway[Any]")


class SessionDispatcher(
    CapsMixin,
    DiscoMixin,
    RegistrationMixin,
    MessageMixin,
    MucMixin,
    PresenceHandlerMixin,
    PubSubMixin,
    SearchMixin,
    VCardMixin,
    Generic[GatewayType],
):
    __slots__: list[str] = ["_MamMixin__mam_cleanup_task", "xmpp"]
    xmpp: GatewayType

    def __init__(self, xmpp: GatewayType) -> None:
        super().__init__(xmpp)
        xmpp.add_event_handler(
            "avatar_metadata_publish", self.on_avatar_metadata_publish
        )

    @exceptions_to_xmpp_errors
    async def on_avatar_metadata_publish(self, m: Message) -> None:
        session = await self._get_session(m, timeout=None)
        if not session.user.preferences.get("sync_avatar", False):
            session.log.debug("User does not want to sync their avatar")
            return
        info = m["pubsub_event"]["items"]["item"]["avatar_metadata"]["info"]

        await self.on_avatar_metadata_info(session, info)

    async def on_avatar_metadata_info(self, session: AnySession, info: Info) -> None:
        hash_ = info["id"]

        if session.user.avatar_hash == hash_:
            session.log.debug("We already know this avatar hash")
            return

        if hash_:
            try:
                iq = await self.xmpp.plugin["xep_0084"].retrieve_avatar(
                    session.user_jid, hash_, ifrom=self.xmpp.boundjid.bare
                )
            except (IqError, IqTimeout) as e:
                session.log.warning("Could not fetch the user's avatar: %s", e)
                return
            bytes_ = iq["pubsub"]["items"]["item"]["avatar_data"]["value"]
            type_ = info["type"]
            height = info["height"]
            width = info["width"]
        else:
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                session.user.avatar_hash = None
                orm.add(session.user)
                orm.commit()
            bytes_ = type_ = height = width = hash_ = None
        try:
            await session.on_avatar(bytes_, hash_, type_, width, height)
        except NotImplementedError:
            pass
        except Exception as e:
            # If something goes wrong here, replying an error stanza will to the
            # avatar update will likely not show in most clients, so let's send
            # a normal message from the component to the user.
            session.send_gateway_message(
                f"Something went wrong trying to set your avatar: {e!r}"
            )
        else:
            session.user.avatar_hash = hash_
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                orm.add(session.user)
                orm.commit()
            for room in session.bookmarks:
                participant = await room.get_user_participant()
                participant.send_last_presence(force=True, no_cache_online=True)


log = logging.getLogger(__name__)
