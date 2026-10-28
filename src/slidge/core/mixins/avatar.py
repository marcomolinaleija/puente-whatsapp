import hashlib
from asyncio import Task
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PIL import UnidentifiedImageError
from slixmpp import JID
from sqlalchemy.orm.exc import DetachedInstanceError

from ...db.avatar import CachedAvatar, avatar_cache
from ...db.models import Contact, Room
from ...util.types import AnySession, Avatar
from .db import UpdateInfoMixin

if TYPE_CHECKING:
    from ..pubsub import PepAvatar


class AvatarMixin(UpdateInfoMixin):
    """
    Mixin for XMPP entities that have avatars that represent them.

    Both :py:class:`slidge.LegacyContact` and :py:class:`slidge.LegacyMUC` use
    :py:class:`.AvatarMixin`.
    """

    jid: JID = NotImplemented
    session: AnySession = NotImplemented
    stored: Contact | Room
    log: Logger

    def __init__(self) -> None:
        super().__init__()
        self._set_avatar_task: Task[None] | None = None

    @property
    def avatar(self) -> Avatar | None:
        """
        This property can be used to set or unset the avatar.

        Unlike the awaitable :func:`.set_avatar`, it schedules the update for
        later execution and is not blocking
        """
        try:
            if self.stored.avatar is None:
                return None
        except DetachedInstanceError:
            self.merge()
            if self.stored.avatar is None:
                return None
        if self.stored.avatar.legacy_id is None:
            unique_id = None
        else:
            unique_id = self.stored.avatar.legacy_id
        return Avatar(
            unique_id=unique_id,
            url=self.stored.avatar.url,
        )

    @avatar.setter
    def avatar(self, avatar: Avatar | Path | str | None) -> None:
        avatar = convert_avatar(avatar)
        if self._set_avatar_task:
            self._set_avatar_task.cancel()
        self.log.debug("Setting avatar with property")
        self._set_avatar_task = self.session.create_task(self.set_avatar(avatar))

    async def __has_changed(self, avatar: Avatar | None) -> bool:
        if self.avatar is None:
            return avatar is not None
        if avatar is None:
            return self.avatar is not None

        if self.avatar.unique_id is not None and avatar.unique_id is not None:
            return self.avatar.unique_id != avatar.unique_id

        if (
            self.avatar.url is not None
            and avatar.url is not None
            and self.avatar.url == avatar.url
        ):
            return await avatar_cache.url_modified(avatar.url)

        if avatar.path is not None:
            cached = self.get_cached_avatar()
            if cached is not None:
                return cached.path.read_bytes() != avatar.path.read_bytes()

        return True

    async def set_avatar(
        self, avatar: Avatar | Path | str | None = None, delete: bool = False
    ) -> None:
        """
        Set an avatar for this entity

        :param avatar: The avatar. Should ideally come with a legacy network-wide unique
            ID
        :param delete: If the avatar is provided as a Path, whether to delete
            it once used or not.
        """
        avatar = convert_avatar(avatar)

        if avatar is not None and avatar.unique_id is None and avatar.data is not None:
            self.log.debug("Hashing bytes to generate a unique ID")
            avatar = Avatar(
                data=avatar.data, unique_id=hashlib.sha512(avatar.data).hexdigest()
            )

        try:
            if not await self.__has_changed(avatar):
                return
        except Exception:
            self.log.exception("Could not determine if avatar has changed, giving up")
            return

        if avatar is None:
            cached_avatar = None
        else:
            try:
                cached_avatar = await avatar_cache.convert_or_get(avatar)
            except UnidentifiedImageError:
                self.log.warning("%s is not a valid image", avatar)
                cached_avatar = None
            except Exception:
                self.log.exception("Failed to set avatar '%s'", avatar)
                cached_avatar = None

        if delete:
            if avatar is None or avatar.path is None:
                self.log.warning("Requested avatar path delete, but no path provided")
            else:
                avatar.path.unlink()

        stored_avatar = None if cached_avatar is None else cached_avatar.stored
        if not self._updating_info:
            with self.xmpp.store.session() as orm, orm.no_autoflush:
                self.stored = orm.merge(self.stored)
                orm.refresh(self.stored)

        self.stored.avatar = stored_avatar
        self.commit()

        self._post_avatar_update(cached_avatar)

    def get_cached_avatar(self) -> Optional["CachedAvatar"]:
        try:
            if self.stored.avatar is None:
                return None
        except DetachedInstanceError:
            self.merge()
            if self.stored.avatar is None:
                return None
        return avatar_cache.get(self.stored.avatar)

    def get_avatar(self) -> Optional["PepAvatar"]:
        cached_avatar = self.get_cached_avatar()
        if cached_avatar is None:
            return None
        from ..pubsub import PepAvatar

        item = PepAvatar()
        item.set_avatar_from_cache(cached_avatar)
        return item

    def _post_avatar_update(self, cached_avatar: Optional["CachedAvatar"]) -> None:
        raise NotImplementedError


def convert_avatar(
    avatar: Avatar | Path | str | None, unique_id: str | None = None
) -> Avatar | None:
    if isinstance(avatar, Path):
        return Avatar(path=avatar, unique_id=unique_id)
    if isinstance(avatar, str):
        return Avatar(url=avatar)
    if avatar is None or all(x is None for x in avatar):
        return None
    return avatar
