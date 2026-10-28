from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from mimetypes import guess_extension
from typing import Any, ClassVar

import sqlalchemy as sa
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0231.stanza import BitsOfBinary
from sqlalchemy import ColumnElement, Engine, delete, event, select, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session, attributes, joinedload, load_only, sessionmaker

from ..core import config
from ..util.archive_msg import HistoryMessage
from ..util.types import MamMetadata, Sticker
from .meta import Base
from .models import (
    ArchivedMessage,
    ArchivedMessageSource,
    Avatar,
    Bob,
    Contact,
    ContactSent,
    DirectMessages,
    DirectThreads,
    GatewayUser,
    GroupMessages,
    GroupMessagesOrigin,
    GroupThreads,
    Participant,
    Room,
    Space,
)


class UpdatedMixin:
    model: type[Base] = NotImplemented

    def __init__(self, session: Session) -> None:
        self.reset_updated(session)

    def get_by_pk(self, session: Session, pk: int) -> type[Base]:
        stmt = select(self.model).where(self.model.id == pk)  # type:ignore[attr-defined]
        return session.scalar(stmt)  # type:ignore[no-any-return]

    def reset_updated(self, session: Session) -> None:
        session.execute(update(self.model).values(updated=False))


class SlidgeStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.session = sessionmaker[Any](engine)

        self.users = UserStore(self.session)
        self.avatars = AvatarStore(self.session)
        self.id_map = IdMapStore()
        self.bob = BobStore()
        with self.session() as session:
            self.contacts = ContactStore(session)
            self.mam = MAMStore(session, self.session)
            self.rooms = RoomStore(session)
            self.participants = ParticipantStore(session)
            self.spaces = SpaceStore(session)
            session.commit()


class UserStore:
    def __init__(self, session_maker: sessionmaker[Any]) -> None:
        self.session = session_maker

    def update(self, user: GatewayUser) -> None:
        with self.session(expire_on_commit=False) as session:
            # https://github.com/sqlalchemy/sqlalchemy/discussions/6473
            try:
                attributes.flag_modified(user, "legacy_module_data")
                attributes.flag_modified(user, "preferences")
            except InvalidRequestError:
                pass
            session.add(user)
            session.commit()


class AvatarStore:
    def __init__(self, session_maker: sessionmaker[Any]) -> None:
        self.session = session_maker


LegacyToXmppType = (
    type[DirectMessages]
    | type[DirectThreads]
    | type[GroupMessages]
    | type[GroupThreads]
    | type[GroupMessagesOrigin]
)


class IdMapStore:
    @staticmethod
    def _set(
        session: Session,
        foreign_key: int,
        legacy_id: str,
        xmpp_ids: list[str],
        type_: LegacyToXmppType,
    ) -> None:
        kwargs = dict(foreign_key=foreign_key, legacy_id=legacy_id)
        ids = list(
            session.scalars(
                select(type_.id).filter(
                    type_.foreign_key == foreign_key, type_.legacy_id == legacy_id
                )
            )
        )
        if ids:
            log.debug("Resetting legacy ID %s", legacy_id)
            session.execute(delete(type_).where(type_.id.in_(ids)))
        for xmpp_id in xmpp_ids:
            msg = type_(xmpp_id=xmpp_id, **kwargs)
            session.add(msg)

    def set_thread(
        self,
        session: Session,
        foreign_key: int,
        legacy_id: str,
        xmpp_id: str,
        group: bool,
    ) -> None:
        self._set(
            session,
            foreign_key,
            legacy_id,
            [xmpp_id],
            GroupThreads if group else DirectThreads,
        )

    def set_msg(
        self,
        session: Session,
        foreign_key: int,
        legacy_id: str,
        xmpp_ids: list[str],
        group: bool,
    ) -> None:
        self._set(
            session,
            foreign_key,
            legacy_id,
            xmpp_ids,
            GroupMessages if group else DirectMessages,
        )

    def set_origin(
        self, session: Session, foreign_key: int, legacy_id: str, xmpp_id: str
    ) -> None:
        self._set(
            session,
            foreign_key,
            legacy_id,
            [xmpp_id],
            GroupMessagesOrigin,
        )

    def get_origin(
        self, session: Session, foreign_key: int, legacy_id: str
    ) -> list[str]:
        return self._get(
            session,
            foreign_key,
            legacy_id,
            GroupMessagesOrigin,
        )

    @staticmethod
    def _get(
        session: Session, foreign_key: int, legacy_id: str, type_: LegacyToXmppType
    ) -> list[str]:
        return list(
            session.scalars(
                select(type_.xmpp_id).filter_by(
                    foreign_key=foreign_key, legacy_id=str(legacy_id)
                )
            )
        )

    def get_xmpp(
        self, session: Session, foreign_key: int, legacy_id: str, group: bool
    ) -> list[str]:
        return self._get(
            session,
            foreign_key,
            legacy_id,
            GroupMessages if group else DirectMessages,
        )

    @staticmethod
    def _get_legacy(
        session: Session, foreign_key: int, xmpp_id: str, type_: LegacyToXmppType
    ) -> str | None:
        return session.scalar(
            select(type_.legacy_id).filter_by(foreign_key=foreign_key, xmpp_id=xmpp_id)
        )

    def get_legacy(
        self,
        session: Session,
        foreign_key: int,
        xmpp_id: str,
        group: bool,
        origin: bool = False,
    ) -> str | None:
        if origin and group:
            return self._get_legacy(
                session,
                foreign_key,
                xmpp_id,
                GroupMessagesOrigin,
            )
        return self._get_legacy(
            session,
            foreign_key,
            xmpp_id,
            GroupMessages if group else DirectMessages,
        )

    def get_thread(
        self, session: Session, foreign_key: int, xmpp_id: str, group: bool
    ) -> str | None:
        return self._get_legacy(
            session,
            foreign_key,
            xmpp_id,
            GroupThreads if group else DirectThreads,
        )

    @staticmethod
    def was_sent_by_user(
        session: Session, foreign_key: int, legacy_id: str, group: bool
    ) -> bool:
        type_ = GroupMessages if group else DirectMessages
        return (
            session.scalar(
                select(type_.id).filter_by(foreign_key=foreign_key, legacy_id=legacy_id)
            )
            is not None
        )


class ContactStore(UpdatedMixin):
    model = Contact

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        session.execute(update(Contact).values(cached_presence=False))
        session.execute(update(Contact).values(caps_ver=None))

    @staticmethod
    def add_to_sent(session: Session, contact_pk: int, msg_id: str) -> None:
        if (
            session.query(ContactSent.id)
            .where(ContactSent.contact_id == contact_pk)
            .where(ContactSent.msg_id == msg_id)
            .first()
        ) is not None:
            log.warning("Contact %s has already sent message %s", contact_pk, msg_id)
            return
        new = ContactSent(contact_id=contact_pk, msg_id=msg_id)
        session.add(new)

    @staticmethod
    def pop_sent_up_to(session: Session, contact_pk: int, msg_id: str) -> list[str]:
        result = []
        to_del = []
        for row in session.execute(
            select(ContactSent)
            .where(ContactSent.contact_id == contact_pk)
            .order_by(ContactSent.id)
        ).scalars():
            to_del.append(row.id)
            result.append(row.msg_id)
            if row.msg_id == msg_id:
                break
        session.execute(delete(ContactSent).where(ContactSent.id.in_(to_del)))
        return result


class MAMStore:
    def __init__(self, session: Session, session_maker: sessionmaker[Any]) -> None:
        self.session = session_maker
        self.reset_source(session)

    @staticmethod
    def reset_source(session: Session) -> None:
        session.execute(
            update(ArchivedMessage).values(source=ArchivedMessageSource.BACKFILL)
        )

    @staticmethod
    def nuke_older_than(session: Session, days: int) -> None:
        session.execute(
            delete(ArchivedMessage).where(
                ArchivedMessage.timestamp < datetime.now() - timedelta(days=days)
            )
        )

    @staticmethod
    def add_message(
        session: Session,
        room_pk: int,
        message: HistoryMessage,
        archive_only: bool,
        legacy_msg_id: str | None,
    ) -> None:
        source = (
            ArchivedMessageSource.BACKFILL
            if archive_only
            else ArchivedMessageSource.LIVE
        )
        existing = session.execute(
            select(ArchivedMessage)
            .where(ArchivedMessage.room_id == room_pk)
            .where(ArchivedMessage.stanza_id == message.id)
        ).scalar()
        if existing is None and legacy_msg_id is not None:
            existing = session.execute(
                select(ArchivedMessage)
                .where(ArchivedMessage.room_id == room_pk)
                .where(ArchivedMessage.legacy_id == str(legacy_msg_id))
            ).scalar()
        if existing is not None:
            log.debug("Updating message %s in room %s", message.id, room_pk)
            existing.timestamp = message.when
            existing.stanza = str(message.stanza)
            existing.author_jid = message.stanza.get_from()
            existing.source = source
            existing.legacy_id = legacy_msg_id
            session.add(existing)
            return
        mam_msg = ArchivedMessage(
            stanza_id=message.id,
            timestamp=message.when,
            stanza=str(message.stanza),
            author_jid=message.stanza.get_from(),
            room_id=room_pk,
            source=source,
            legacy_id=legacy_msg_id,
        )
        session.add(mam_msg)

    @staticmethod
    def get_messages(
        session: Session,
        room_pk: int,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        before_id: str | None = None,
        after_id: str | None = None,
        ids: Collection[str] = (),
        last_page_n: int | None = None,
        sender: str | None = None,
        flip: bool = False,
    ) -> Iterator[HistoryMessage]:
        q = select(ArchivedMessage).where(ArchivedMessage.room_id == room_pk)
        if start_date is not None:
            q = q.where(ArchivedMessage.timestamp >= start_date)
        if end_date is not None:
            q = q.where(ArchivedMessage.timestamp <= end_date)
        if before_id is not None:
            stamp = session.execute(
                select(ArchivedMessage.timestamp).where(
                    ArchivedMessage.stanza_id == before_id,
                    ArchivedMessage.room_id == room_pk,
                )
            ).scalar_one_or_none()
            if stamp is None:
                raise XMPPError(
                    "item-not-found",
                    f"Message {before_id} not found",
                )
            q = q.where(ArchivedMessage.timestamp < stamp)
        if after_id is not None:
            stamp = session.execute(
                select(ArchivedMessage.timestamp).where(
                    ArchivedMessage.stanza_id == after_id,
                    ArchivedMessage.room_id == room_pk,
                )
            ).scalar_one_or_none()
            if stamp is None:
                raise XMPPError(
                    "item-not-found",
                    f"Message {after_id} not found",
                )
            q = q.where(ArchivedMessage.timestamp > stamp)
        if ids:
            q = q.filter(ArchivedMessage.stanza_id.in_(ids))
        if sender is not None:
            q = q.where(ArchivedMessage.author_jid == sender)
        if flip:
            q = q.order_by(ArchivedMessage.timestamp.desc())
        else:
            q = q.order_by(ArchivedMessage.timestamp.asc())
        msgs = list(session.execute(q).scalars())
        if ids and len(msgs) != len(ids):
            raise XMPPError(
                "item-not-found",
                "One of the requested messages IDs could not be found "
                "with the given constraints.",
            )
        if last_page_n is not None:
            msgs = msgs[:last_page_n] if flip else msgs[-last_page_n:]
        for h in msgs:
            yield HistoryMessage(
                stanza=str(h.stanza), when=h.timestamp.replace(tzinfo=UTC)
            )

    @staticmethod
    def get_first(
        session: Session, room_pk: int, with_legacy_id: bool = False
    ) -> ArchivedMessage | None:
        q = (
            select(ArchivedMessage)
            .where(ArchivedMessage.room_id == room_pk)
            .order_by(ArchivedMessage.timestamp.asc())
        )
        if with_legacy_id:
            q = q.filter(ArchivedMessage.legacy_id.isnot(None))
        return session.execute(q).scalar()

    @staticmethod
    def get_last(
        session: Session, room_pk: int, source: ArchivedMessageSource | None = None
    ) -> ArchivedMessage | None:
        q = select(ArchivedMessage).where(ArchivedMessage.room_id == room_pk)

        if source is not None:
            q = q.where(ArchivedMessage.source == source)

        return session.execute(q.order_by(ArchivedMessage.timestamp.desc())).scalar()

    def get_first_and_last(self, session: Session, room_pk: int) -> list[MamMetadata]:
        r = []
        first = self.get_first(session, room_pk)
        if first is not None:
            r.append(MamMetadata(first.stanza_id, first.timestamp))
        last = self.get_last(session, room_pk)
        if last is not None:
            r.append(MamMetadata(last.stanza_id, last.timestamp))
        return r

    @staticmethod
    def get_most_recent_with_legacy_id(
        session: Session, room_pk: int, source: ArchivedMessageSource | None = None
    ) -> ArchivedMessage | None:
        q = (
            select(ArchivedMessage)
            .where(ArchivedMessage.room_id == room_pk)
            .where(ArchivedMessage.legacy_id.isnot(None))
        )
        if source is not None:
            q = q.where(ArchivedMessage.source == source)
        return session.execute(q.order_by(ArchivedMessage.timestamp.desc())).scalar()

    @staticmethod
    def get_least_recent_with_legacy_id_after(
        session: Session,
        room_pk: int,
        after_id: str,
        source: ArchivedMessageSource = ArchivedMessageSource.LIVE,
    ) -> ArchivedMessage | None:
        after_timestamp = (
            session.query(ArchivedMessage.timestamp)
            .filter(ArchivedMessage.room_id == room_pk)
            .filter(ArchivedMessage.legacy_id == after_id)
            .scalar()
        )
        q = (
            select(ArchivedMessage)
            .where(ArchivedMessage.room_id == room_pk)
            .where(ArchivedMessage.legacy_id.isnot(None))
            .where(ArchivedMessage.source == source)
            .where(ArchivedMessage.timestamp > after_timestamp)
        )
        return session.execute(q.order_by(ArchivedMessage.timestamp.asc())).scalar()

    @staticmethod
    def get_by_legacy_id(
        session: Session, room_pk: int, legacy_id: str
    ) -> ArchivedMessage | None:
        return (
            session.query(ArchivedMessage)
            .filter(ArchivedMessage.room_id == room_pk)
            .filter(ArchivedMessage.legacy_id == legacy_id)
            .first()
        )

    @staticmethod
    def pop_unread_up_to(session: Session, room_pk: int, stanza_id: str) -> list[str]:
        q = (
            select(ArchivedMessage.id, ArchivedMessage.stanza_id)
            .where(ArchivedMessage.room_id == room_pk)
            .where(~ArchivedMessage.displayed_by_user)
            .where(ArchivedMessage.legacy_id.is_not(None))
            .order_by(ArchivedMessage.timestamp.asc())
        )

        ref = session.scalar(
            select(ArchivedMessage)
            .where(ArchivedMessage.room_id == room_pk)
            .where(ArchivedMessage.stanza_id == stanza_id)
        )

        if ref is None:
            log.debug(
                "(pop unread in muc): message not found, returning all MAM messages."
            )
            rows = session.execute(q)
        else:
            rows = session.execute(q.where(ArchivedMessage.timestamp <= ref.timestamp))

        pks: list[int] = []
        stanza_ids: list[str] = []

        for id_, stanza_id in rows:
            pks.append(id_)
            stanza_ids.append(stanza_id)

        session.execute(
            update(ArchivedMessage)
            .where(ArchivedMessage.id.in_(pks))
            .values(displayed_by_user=True)
        )
        return stanza_ids

    @staticmethod
    def is_displayed_by_user(
        session: Session, room_jid: str, legacy_msg_id: str
    ) -> bool:
        return any(
            session.execute(
                select(ArchivedMessage.displayed_by_user)
                .join(Room)
                .where(Room.jid == room_jid)
                .where(ArchivedMessage.legacy_id == legacy_msg_id)
            ).scalars()
        )


class RoomStore(UpdatedMixin):
    model = Room

    def reset_updated(self, session: Session) -> None:
        super().reset_updated(session)
        session.execute(
            update(Room).values(
                subject_setter=None,
                user_resources=None,
                history_filled=False,
                participants_filled=False,
            )
        )

    @staticmethod
    def get_all(session: Session, user_pk: int) -> Iterator[Room]:
        yield from session.scalars(select(Room).where(Room.user_account_id == user_pk))

    @staticmethod
    def get(session: Session, user_pk: int, legacy_id: str) -> Room:
        return session.execute(
            select(Room)
            .where(Room.user_account_id == user_pk)
            .where(Room.legacy_id == legacy_id)
        ).scalar_one()

    @staticmethod
    def nick_available(session: Session, room_pk: int, nickname: str) -> bool:
        return (
            session.execute(
                select(Participant.id).filter_by(room_id=room_pk, nickname=nickname)
            )
        ).one_or_none() is None


class ParticipantStore:
    def __init__(self, session: Session) -> None:
        session.execute(delete(Participant))

    @staticmethod
    def get_all(
        session: Session, room_pk: int, user_included: bool = True
    ) -> Iterator[Participant]:
        query = select(Participant).where(Participant.room_id == room_pk)
        if not user_included:
            query = query.where(~Participant.is_user)
        yield from session.scalars(query).unique()

    @staticmethod
    def delete(session: Session, pk: int) -> None:
        session.execute(delete(Participant).where(Participant.id == pk))


class BobStore:
    _ATTR_MAP: ClassVar[dict[str, str]] = {
        "sha-1": "sha_1",
        "sha1": "sha_1",
        "sha-256": "sha_256",
        "sha256": "sha_256",
        "sha-512": "sha_512",
        "sha512": "sha_512",
    }

    _ALG_MAP: ClassVar[dict[str, Callable[[bytes], hashlib._Hash]]] = {
        "sha_1": hashlib.sha1,
        "sha_256": hashlib.sha256,
        "sha_512": hashlib.sha512,
    }

    def __init__(self) -> None:
        if (config.HOME_DIR / "slidge_stickers").exists():
            shutil.move(
                config.HOME_DIR / "slidge_stickers", config.HOME_DIR / "bob_store"
            )
        self.root_dir = config.HOME_DIR / "bob_store"
        self.root_dir.mkdir(exist_ok=True)

    @staticmethod
    def __split_cid(cid: str) -> list[str]:
        return cid.removesuffix("@bob.xmpp.org").split("+")

    def __get_condition(self, cid: str) -> ColumnElement[bool]:
        alg_name, digest = self.__split_cid(cid)
        attr = self._ATTR_MAP.get(alg_name)
        if attr is None:
            log.warning("Unknown hash algorithm: %s", alg_name)
            raise ValueError
        return getattr(Bob, attr) == digest  # type:ignore[no-any-return]

    def get(self, session: Session, cid: str) -> Bob | None:
        try:
            return session.query(Bob).filter(self.__get_condition(cid)).scalar()  # type:ignore[no-any-return]
        except ValueError:
            log.warning("Cannot get Bob with CID: %s", cid)
            return None

    def get_sticker(self, session: Session, cid: str) -> Sticker | None:
        bob = self.get(session, cid)
        if bob is None:
            return None
        return self.__sticker_from_bob(bob)

    def __sticker_from_bob(self, bob: Bob) -> Sticker:
        return Sticker(
            self.root_dir / bob.file_name,
            bob.content_type,
            {h: getattr(bob, h) for h in self._ALG_MAP},
        )

    def get_bob(
        self, session: Session, _jid: object, _node: object, _ifrom: object, cid: str
    ) -> BitsOfBinary | None:
        stored = self.get(session, cid)
        if stored is None:
            return None
        bob = BitsOfBinary()
        bob["data"] = (self.root_dir / stored.file_name).read_bytes()
        if stored.content_type is not None:
            bob["type"] = stored.content_type
        bob["cid"] = cid
        return bob

    def del_bob(
        self, session: Session, _jid: object, _node: object, _ifrom: object, cid: str
    ) -> None:
        try:
            file_name = session.scalar(
                delete(Bob).where(self.__get_condition(cid)).returning(Bob.file_name)
            )
        except ValueError:
            log.warning("Cannot delete Bob with CID: %s", cid)
            return None
        if file_name is None:
            log.warning("No BoB with CID: %s", cid)
            return None
        (self.root_dir / file_name).unlink()

    def set_bob(
        self,
        session: Session,
        _jid: object,
        _node: object,
        _ifrom: object,
        bob: BitsOfBinary,
    ) -> Sticker | None:
        return self.set_sticker(session, bob["cid"], bob["data"], bob["type"])

    def set_sticker(
        self,
        session: Session,
        cid: str,
        bytes_: bytes,
        content_type: str | None,
    ) -> Sticker | None:
        try:
            alg_name, digest = self.__split_cid(cid)
        except ValueError:
            log.warning("Invalid CID provided: %s", cid)
            return None
        attr = self._ATTR_MAP.get(alg_name)
        if attr is None:
            log.warning("Cannot set Bob: Unknown algorithm type: %s", alg_name)
            return None
        existing = self.get(session, cid)
        if existing:
            log.debug("Bob already exists")
            return None
        path = self.root_dir / uuid.uuid4().hex
        if content_type is None:
            try:
                import magic
            except ImportError:
                content_type = "application/octet-stream"
            else:
                content_type = magic.from_buffer(bytes_, mime=True)
        path = path.with_suffix(guess_extension(content_type) or "")
        path.write_bytes(bytes_)
        hashes = {k: v(bytes_).hexdigest() for k, v in self._ALG_MAP.items()}
        if hashes[attr] != digest:
            path.unlink(missing_ok=True)
            raise ValueError("Provided CID does not match calculated hash")
        row = Bob(file_name=path.name, content_type=content_type, **hashes)
        session.add(row)
        return self.__sticker_from_bob(row)


class SpaceStore(UpdatedMixin):
    model = Space

    def __init__(self, session: Session) -> None:
        session.execute(delete(Space))

    @staticmethod
    def add_or_get(session: Session, user_pk: int, legacy_id: str) -> Space:
        space = session.execute(
            select(Space)
            .where(Space.user_account_id == user_pk)
            .where(Space.legacy_id == legacy_id)
        ).scalar_one_or_none()
        if space is None:
            space = Space(user_account_id=user_pk, legacy_id=legacy_id)
            session.add(space)
            session.commit()
        return space

    @staticmethod
    def get_all(session: Session, user_pk: int) -> Iterable[Space]:
        return session.execute(
            select(Space).where(Space.user_account_id == user_pk)
        ).scalars()

    @staticmethod
    def get_by_legacy_id(
        session: Session, user_pk: int, legacy_id: str, full: bool = False
    ) -> Space | None:
        stmt = (
            select(Space)
            .where(Space.user_account_id == user_pk)
            .where(Space.legacy_id == legacy_id)
        )
        if full:
            stmt = stmt.options(
                joinedload(Space.owners),
                joinedload(Space.creator),
            )
        return session.execute(stmt).unique().scalar_one_or_none()

    @staticmethod
    def get_unupdated(session: Session, user_pk: int) -> list[Space]:
        return list(
            session.execute(
                select(Space)
                .where(Space.user_account_id == user_pk)
                .where(Space.updated.is_(False))
            ).scalars()
        )

    @staticmethod
    def get_rooms(
        session: Session,
        user_pk: int,
        legacy_id: str,
        room_legacy_ids: Iterable[str] = (),
    ) -> list[Room]:
        q = (
            select(Room)
            .join(Room.space)
            .where(Room.user_account_id == user_pk)
            .where(Space.legacy_id == legacy_id)
            .options(load_only(Room.jid, Room.name))
        )
        if room_legacy_ids:
            q = q.where(Room.legacy_id.in_(room_legacy_ids))
        return list(session.execute(q).scalars())

    @staticmethod
    def exists(session: Session, user_pk: int, legacy_id: str) -> bool:
        return session.execute(
            select(
                sa.exists()
                .where(Space.user_account_id == user_pk)
                .where(Space.legacy_id == legacy_id)
            )
        ).scalar_one()


@event.listens_for(sa.orm.Session, "after_flush")
def _check_avatar_orphans(session: Session, flush_context: sa.ExecutionContext) -> None:
    if not session.deleted:
        return

    potentially_orphaned = set()
    for obj in session.deleted:
        if isinstance(obj, (Contact, Room)) and obj.avatar_id:
            potentially_orphaned.add(obj.avatar_id)
    if not potentially_orphaned:
        return

    result = session.execute(
        sa.delete(Avatar).where(
            sa.and_(
                Avatar.id.in_(potentially_orphaned),
                sa.not_(sa.exists().where(Contact.avatar_id == Avatar.id)),
                sa.not_(sa.exists().where(Room.avatar_id == Avatar.id)),
            )
        )
    )
    deleted_count = result.rowcount  # type:ignore[attr-defined]
    log.debug("Auto-deleted %s orphaned avatars", deleted_count)


log = logging.getLogger(__name__)
