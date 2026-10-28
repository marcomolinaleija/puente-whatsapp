import asyncio
import hashlib
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path

import aiohttp
from multidict import CIMultiDictProxy
from PIL.Image import Image
from PIL.Image import open as open_image
from sqlalchemy import select

from ..core import config
from ..util.lock import NamedLockMixin
from ..util.types import Avatar as AvatarType
from .models import Avatar
from .store import AvatarStore


class CachedAvatar:
    def __init__(self, stored: Avatar, root_dir: Path) -> None:
        self.stored = stored
        self._root = root_dir

    @property
    def pk(self) -> int | None:
        return self.stored.id

    @property
    def hash(self) -> str:
        return self.stored.hash

    @property
    def height(self) -> int:
        return self.stored.height

    @property
    def width(self) -> int:
        return self.stored.width

    @property
    def etag(self) -> str | None:
        return self.stored.etag

    @property
    def last_modified(self) -> str | None:
        return self.stored.last_modified

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()

    @property
    def path(self) -> Path:
        return (self._root / self.hash).with_suffix(".png")


class NotModified(Exception):
    pass


class AvatarCache(NamedLockMixin):
    dir: Path
    http: aiohttp.ClientSession
    store: AvatarStore

    def __init__(self) -> None:
        self._thread_pool = ThreadPoolExecutor(config.AVATAR_RESAMPLING_THREADS)
        super().__init__()

    def get(self, stored: Avatar) -> CachedAvatar:
        return CachedAvatar(stored, self.dir)

    def set_dir(self, path: Path) -> None:
        self.dir = path
        self.dir.mkdir(exist_ok=True)
        log.debug("Checking avatar files")
        with self.store.session(expire_on_commit=False) as orm:
            for stored in orm.query(Avatar).all():
                avatar = CachedAvatar(stored, path)
                if avatar.path.exists():
                    continue
                log.warning(
                    "Removing avatar %s from store because %s does not exist",
                    avatar.hash,
                    avatar.path,
                )
                orm.delete(stored)
            orm.commit()

    def close(self) -> None:
        self._thread_pool.shutdown(cancel_futures=True)

    def __get_http_headers(
        self, cached: CachedAvatar | Avatar | None = None
    ) -> dict[str, str]:
        headers = {}
        if cached and (self.dir / cached.hash).with_suffix(".png").exists():
            if last_modified := cached.last_modified:
                headers["If-Modified-Since"] = last_modified
            if etag := cached.etag:
                headers["If-None-Match"] = etag
        return headers

    async def __download(
        self,
        url: str,
        headers: dict[str, str],
    ) -> tuple[Image, CIMultiDictProxy[str]]:
        async with self.http.get(url, headers=headers) as response:
            if response.status == HTTPStatus.NOT_MODIFIED:
                log.debug("Using avatar cache for %s", url)
                raise NotModified
            response.raise_for_status()
            return (
                open_image(io.BytesIO(await response.read())),
                response.headers,
            )

    async def __is_modified(self, url: str, headers: dict[str, str]) -> bool:
        async with self.http.head(url, headers=headers) as response:
            return response.status != HTTPStatus.NOT_MODIFIED

    async def url_modified(self, url: str) -> bool:
        with self.store.session() as orm:
            cached = orm.query(Avatar).filter_by(url=url).one_or_none()
        if cached is None:
            return True
        headers = self.__get_http_headers(cached)
        return await self.__is_modified(url, headers)

    @staticmethod
    async def _get_image(avatar: AvatarType) -> Image:
        if avatar.data is not None:
            return open_image(io.BytesIO(avatar.data))
        elif avatar.path is not None:
            return open_image(avatar.path)
        raise TypeError("Avatar must be bytes or a Path", avatar)

    async def convert_or_get(self, avatar: AvatarType) -> CachedAvatar:
        if avatar.unique_id is not None:
            with self.store.session() as orm:
                stored = (
                    orm.query(Avatar)
                    .filter_by(legacy_id=str(avatar.unique_id))
                    .one_or_none()
                )
                if stored is not None:
                    return self.get(stored)

        if avatar.url is not None:
            return await self.__convert_url(avatar)

        return await self.convert(avatar, await self._get_image(avatar))

    async def __convert_url(self, avatar: AvatarType) -> CachedAvatar:
        assert avatar.url is not None
        async with self.lock(avatar.unique_id or avatar.url):
            with self.store.session() as orm:
                if avatar.unique_id is None:
                    stored = orm.query(Avatar).filter_by(url=avatar.url).one_or_none()
                else:
                    stored = (
                        orm.query(Avatar)
                        .filter_by(legacy_id=str(avatar.unique_id))
                        .one_or_none()
                    )
                    if stored is not None:
                        return self.get(stored)

            try:
                img, response_headers = await self.__download(
                    avatar.url, self.__get_http_headers(stored)
                )
            except NotModified:
                assert stored is not None
                return self.get(stored)

            return await self.convert(avatar, img, response_headers)

    async def convert(
        self,
        avatar: AvatarType,
        img: Image,
        response_headers: CIMultiDictProxy[str] | None = None,
    ) -> CachedAvatar:
        resize = (size := config.AVATAR_SIZE) and any(x > size for x in img.size)
        if resize:
            await asyncio.get_event_loop().run_in_executor(
                self._thread_pool, img.thumbnail, (size, size)
            )
            log.debug("Resampled image to %s", img.size)

        if (
            not resize
            and img.format == "PNG"
            and avatar.path is not None
            and avatar.path.exists()
        ):
            img_bytes = avatar.path.read_bytes()
        else:
            with io.BytesIO() as f:
                img.save(f, format="PNG")
                img_bytes = f.getvalue()

        hash_ = hashlib.sha1(img_bytes).hexdigest()
        file_path = (self.dir / hash_).with_suffix(".png")
        if file_path.exists():
            log.warning("Overwriting %s", file_path)
        with file_path.open("wb") as file:
            file.write(img_bytes)

        with self.store.session(expire_on_commit=False) as orm:
            stored = orm.execute(select(Avatar).where(Avatar.hash == hash_)).scalar()

            if stored is not None:
                if (
                    avatar.unique_id is not None
                    and str(avatar.unique_id) != stored.legacy_id
                ):
                    log.warning(
                        "Updating the 'unique' ID of an avatar, was '%s', is now '%s'",
                        stored.legacy_id,
                        avatar.unique_id,
                    )
                    stored.legacy_id = str(avatar.unique_id)
                    orm.add(stored)
                    orm.commit()

                return self.get(stored)

        stored = Avatar(
            hash=hash_,
            height=img.height,
            width=img.width,
            url=avatar.url,
            legacy_id=avatar.unique_id,
        )
        if response_headers:
            stored.etag = response_headers.get("etag")
            stored.last_modified = response_headers.get("last-modified")

        with self.store.session(expire_on_commit=False) as orm:
            if avatar.url is not None:
                existing = orm.execute(
                    select(Avatar).filter_by(url=avatar.url)
                ).scalar_one_or_none()
                if existing is not None:
                    orm.delete(existing)
                    orm.commit()
            orm.add(stored)
            orm.commit()
            return self.get(stored)


avatar_cache = AvatarCache()
log = logging.getLogger(__name__)
_download_lock = asyncio.Lock()

__all__ = (
    "CachedAvatar",
    "avatar_cache",
)
