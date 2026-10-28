import asyncio
import logging
import uuid
import warnings
from collections.abc import Collection, Iterator
from copy import copy
from datetime import UTC, datetime

from slixmpp import Iq, Message

from ..db.models import ArchivedMessage, ArchivedMessageSource, Room
from ..db.store import SlidgeStore
from ..util.archive_msg import HistoryMessage
from ..util.types import AnyParticipant, HoleBound


class MessageArchive:
    def __init__(self, room: Room, store: SlidgeStore) -> None:
        self.room = room
        self.__rooms_store = store.rooms
        self.__store = store.mam

    def _prepare_add(
        self,
        msg: Message,
        participant: "AnyParticipant | None" = None,
        archive_only: bool = False,
        legacy_msg_id: str | None = None,
    ) -> tuple[HistoryMessage, str | None] | None:
        if not archivable(msg):
            return
        new_msg = copy(msg)
        if participant and not participant.muc.is_anonymous:
            new_msg["muc"]["role"] = participant.role or "participant"
            new_msg["muc"]["affiliation"] = participant.affiliation or "member"
            if participant.contact:
                new_msg["muc"]["jid"] = participant.contact.jid.bare
            elif participant.is_user:
                new_msg["muc"]["jid"] = participant.user_jid.bare
            elif participant.is_system:
                new_msg["muc"]["jid"] = participant.muc.jid
            else:
                warnings.warn(
                    f"No real JID for participant '{participant.nickname}' in '{self.room.name}'"
                )
                new_msg["muc"]["jid"] = (
                    f"{uuid.uuid4()}@{participant.xmpp.boundjid.bare}"
                )

        if "replace" in msg:
            legacy_msg_id = None

        return HistoryMessage(new_msg), legacy_msg_id

    def add(
        self,
        msg: Message,
        participant: "AnyParticipant | None" = None,
        archive_only: bool = False,
        legacy_msg_id: str | None = None,
    ) -> None:
        prepared = self._prepare_add(
            msg, participant, archive_only, legacy_msg_id,
        )
        if prepared is None:
            return
        history, legacy_msg_id = prepared
        with self.__store.session(expire_on_commit=False) as orm:
            if self.room.id is None:
                self.room = self.__rooms_store.get(
                    orm, self.room.user_account_id, self.room.legacy_id
                )
            self.__store.add_message(
                orm,
                self.room.id,
                history,
                archive_only,
                None if legacy_msg_id is None else str(legacy_msg_id),
            )
            orm.commit()

    # CAN nonblocking group echo archive v1
    async def add_async(
        self, msg: Message, participant: "AnyParticipant | None" = None,
        archive_only: bool = False, legacy_msg_id: str | None = None,
    ) -> None:
        # Prepare mutable stanza/participant data only on the owner loop.
        prepared = self._prepare_add(msg, participant, archive_only, legacy_msg_id)
        if prepared is None:
            return
        history, legacy_msg_id = prepared
        xml, when, stanza_id = str(history.stanza), history.when, history.id
        room_pk = self.room.id
        account_pk, room_legacy_id = self.room.user_account_id, self.room.legacy_id
        store, rooms = self.__store, self.__rooms_store

        def commit():
            # Only immutable values and stateless stores enter the worker.
            # Each transaction creates/closes its own session, including on failure.
            with store.session(expire_on_commit=False) as orm:
                pk = room_pk
                if pk is None:
                    pk = rooms.get(orm, account_pk, room_legacy_id).id
                message = HistoryMessage(xml, when=when)
                message.id = stanza_id
                store.add_message(orm, pk, message, archive_only,
                                  None if legacy_msg_id is None else str(legacy_msg_id))
                orm.commit()

        task = asyncio.create_task(asyncio.to_thread(commit))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not release the caller's room lock while SQL is still running.
            await asyncio.gather(task, return_exceptions=True)
            raise

    def __iter__(self) -> Iterator[HistoryMessage]:
        return iter(self.get_all())

    @staticmethod
    def __to_bound(stored: ArchivedMessage) -> HoleBound:
        return HoleBound(
            stored.legacy_id,  # type:ignore
            stored.timestamp.replace(tzinfo=UTC),
        )

    def get_hole_bounds(self) -> tuple[HoleBound | None, HoleBound | None]:
        with self.__store.session() as orm:
            most_recent = self.__store.get_most_recent_with_legacy_id(orm, self.room.id)
            if most_recent is None:
                return None, None
            if most_recent.source == ArchivedMessageSource.BACKFILL:
                # most recent = only backfill, fetch everything since last backfill
                return self.__to_bound(most_recent), None

            most_recent_back_filled = self.__store.get_most_recent_with_legacy_id(
                orm, self.room.id, ArchivedMessageSource.BACKFILL
            )
            if most_recent_back_filled is None:
                # group was never back-filled, fetch everything before first live
                least_recent_live = self.__store.get_first(orm, self.room.id, True)
                assert least_recent_live is not None
                return None, self.__to_bound(least_recent_live)

            assert most_recent_back_filled.legacy_id is not None
            least_recent_live = self.__store.get_least_recent_with_legacy_id_after(
                orm, self.room.id, most_recent_back_filled.legacy_id
            )
            assert least_recent_live is not None
            # this is a hole caused by slidge downtime
            return self.__to_bound(most_recent_back_filled), self.__to_bound(
                least_recent_live
            )

    def get_all(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        before_id: str | None = None,
        after_id: str | None = None,
        ids: Collection[str] = (),
        last_page_n: int | None = None,
        sender: str | None = None,
        flip: bool = False,
    ) -> Iterator[HistoryMessage]:
        with self.__store.session() as orm:
            yield from self.__store.get_messages(
                orm,
                self.room.id,
                before_id=before_id,
                after_id=after_id,
                ids=ids,
                last_page_n=last_page_n,
                sender=sender,
                start_date=start_date,
                end_date=end_date,
                flip=flip,
            )

    async def send_metadata(self, iq: Iq) -> None:
        """
        Send archive extent, as per the spec

        :param iq:
        :return:
        """
        reply = iq.reply()
        with self.__store.session() as orm:
            messages = self.__store.get_first_and_last(orm, self.room.id)
        if messages:
            for x, m in [("start", messages[0]), ("end", messages[-1])]:
                reply["mam_metadata"][x]["id"] = m.id
                reply["mam_metadata"][x]["timestamp"] = m.sent_on.replace(tzinfo=UTC)
        else:
            reply.enable("mam_metadata")
        reply.send()


def archivable(msg: Message) -> bool:
    """
    Determine if a message stanza is worth archiving, ie, convey meaningful
    info

    :param msg:
    :return:
    """

    if "no-store" in msg:
        return False

    if "no-permanent-store" in msg:
        return False

    if "store" in msg:
        return True

    if msg["body"]:
        return True

    if "retract" in msg:
        return True

    if "reactions" in msg:
        return True

    if "displayed" in msg:
        return True

    return bool(msg["thread"] and msg["subject"])


log = logging.getLogger(__name__)
