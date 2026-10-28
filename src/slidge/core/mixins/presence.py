import contextlib
import re
from asyncio import Task, sleep
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from slixmpp import Presence
from slixmpp.types import PresenceShows, PresenceTypes
from sqlalchemy.orm.exc import DetachedInstanceError

from ...db.models import Contact, Participant
from ...util.types import AnySession, CachedPresence
from .base import BaseSender
from .db import DBMixin

if TYPE_CHECKING:
    pass


class _NoChange(Exception):
    pass


_FRIEND_REQUEST_PRESENCES = {"subscribe", "unsubscribe", "subscribed", "unsubscribed"}
_UPDATE_LAST_SEEN_FALLBACK_TASKS = dict[int, Task[None]]()
_ONE_WEEK_SECONDS = 3600 * 24 * 7


async def _update_last_seen_fallback(session: AnySession, contact_pk: int) -> None:
    await sleep(_ONE_WEEK_SECONDS)
    with session.xmpp.store.session() as orm:
        stored = orm.get(Contact, contact_pk)
        if stored is None:
            return
        contact = session.contacts.from_store(stored)
    contact.send_last_presence(force=True, no_cache_online=False)


def _clear_last_seen_task(contact_pk: int, _task: Task[None]) -> None:
    with contextlib.suppress(KeyError):
        del _UPDATE_LAST_SEEN_FALLBACK_TASKS[contact_pk]


class PresenceMixin(BaseSender, DBMixin):
    _ONLY_SEND_PRESENCE_CHANGES = False

    # this attribute actually only exists for contacts and not participants
    _updating_info: bool
    stored: Contact | Participant

    def __init__(self, *a: object, **k: object) -> None:
        super().__init__(*a, **k)
        # this is only used when a presence is set during Contact.update_info(),
        # when the contact does not have a DB primary key yet, and is written
        # to DB at the end of update_info()
        self.cached_presence: CachedPresence | None = None

    def __is_contact(self) -> bool:
        return isinstance(self.stored, Contact)

    def __stored(self) -> Contact | None:
        if self.__is_contact():
            assert isinstance(self.stored, Contact)
            return self.stored
        else:
            assert isinstance(self.stored, Participant)
            try:
                return self.stored.contact
            except DetachedInstanceError:
                with self.xmpp.store.session() as orm:
                    orm.add(self.stored)
                    if self.stored.contact is None:
                        return None
                    orm.refresh(self.stored.contact)
                    orm.merge(self.stored)
                    return self.stored.contact

    @property
    def __contact_pk(self) -> int | None:
        stored = self.__stored()
        return None if stored is None else stored.id

    def _get_last_presence(self) -> CachedPresence | None:
        stored = self.__stored()
        if stored is None or not stored.cached_presence:
            return None
        return CachedPresence(
            None if stored.last_seen is None else stored.last_seen.replace(tzinfo=UTC),
            stored.ptype,  # type:ignore
            stored.pstatus,
            stored.pshow,  # type:ignore
        )

    def _store_last_presence(self, new: CachedPresence) -> None:
        if self.__is_contact():
            contact = self
        elif (contact := getattr(self, "contact", None)) is None:  # type:ignore[assignment]
            return
        contact.update_stored_attribute(  # type:ignore[attr-defined]
            cached_presence=True,
            **new._asdict(),
        )

    def _make_presence(
        self,
        *,
        last_seen: datetime | None = None,
        force: bool = False,
        bare: bool = False,
        ptype: PresenceTypes | None = None,
        pstatus: str | None = None,
        pshow: PresenceShows | None = None,
    ) -> Presence:
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.astimezone(UTC)

        old = self._get_last_presence()

        if ptype not in _FRIEND_REQUEST_PRESENCES:
            new = CachedPresence(
                last_seen=last_seen, ptype=ptype, pstatus=pstatus, pshow=pshow
            )
            if old != new:
                if hasattr(self, "muc") and ptype == "unavailable":
                    stored = self.__stored()
                    if stored is not None:
                        stored.cached_presence = False
                        self.commit()
                else:
                    self._store_last_presence(new)
            if old and not force and self._ONLY_SEND_PRESENCE_CHANGES:
                if old == new:
                    self.session.log.debug("Presence is the same as cached")
                    raise _NoChange
                self.session.log.debug(
                    "Presence is not the same as cached: %s vs %s", old, new
                )

        p = self.xmpp.make_presence(
            pfrom=self.jid.bare if bare else self.jid,
            ptype=ptype,
            pshow=pshow,
            pstatus=pstatus,
        )
        if last_seen:
            # it's ugly to check for the presence of this string, but a better fix is more work
            if not re.match(
                ".*Last seen .*", p["status"]
            ) and self.session.user.preferences.get("last_seen_fallback", True):
                last_seen_fallback, recent = get_last_seen_fallback(last_seen)
                if p["status"]:
                    p["status"] = p["status"] + " -- " + last_seen_fallback
                else:
                    p["status"] = last_seen_fallback
                pk = self.__contact_pk
                if recent and pk is not None:
                    # if less than a week, we use sth like 'Last seen: Monday, 8:05",
                    # but if lasts more than a week, this is not very informative, so
                    # we need to force resend an updated presence status
                    task = _UPDATE_LAST_SEEN_FALLBACK_TASKS.get(pk)
                    if task is not None:
                        task.cancel()
                    task = self.session.create_task(
                        _update_last_seen_fallback(self.session, pk)
                    )
                    _UPDATE_LAST_SEEN_FALLBACK_TASKS[pk] = task
                    task.add_done_callback(partial(_clear_last_seen_task, pk))
            p["idle"]["since"] = last_seen
        return p

    def send_last_presence(
        self, force: bool = False, no_cache_online: bool = False
    ) -> None:
        if (cache := self._get_last_presence()) is None:
            if force:
                if no_cache_online:
                    self.online()
                else:
                    self.offline()
            return
        self._send(
            self._make_presence(
                last_seen=cache.last_seen,
                force=True,
                ptype=cache.ptype,
                pshow=cache.pshow,
                pstatus=cache.pstatus,
            )
        )

    def online(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        """
        Send an "online" presence from this contact to the user.

        :param status: Arbitrary text, details of the status, eg: "Listening to Britney Spears"
        :param last_seen: For :xep:`0319`
        """
        with contextlib.suppress(_NoChange):
            self._send(self._make_presence(pstatus=status, last_seen=last_seen))

    def away(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        """
        Send an "away" presence from this contact to the user.

        This is a global status, as opposed to :meth:`.LegacyContact.inactive`
        which concerns a specific conversation, ie a specific "chat window"

        :param status: Arbitrary text, details of the status, eg: "Gone to fight capitalism"
        :param last_seen: For :xep:`0319`
        """
        with contextlib.suppress(_NoChange):
            self._send(
                self._make_presence(pstatus=status, pshow="away", last_seen=last_seen)
            )

    def extended_away(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        """
        Send an "extended away" presence from this contact to the user.

        This is a global status, as opposed to :meth:`.LegacyContact.inactive`
        which concerns a specific conversation, ie a specific "chat window"

        :param status: Arbitrary text, details of the status, eg: "Gone to fight capitalism"
        :param last_seen: For :xep:`0319`
        """
        with contextlib.suppress(_NoChange):
            self._send(
                self._make_presence(pstatus=status, pshow="xa", last_seen=last_seen)
            )

    def busy(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        """
        Send a "busy" (ie, "dnd") presence from this contact to the user,

        :param status: eg: "Trying to make sense of XEP-0100"
        :param last_seen: For :xep:`0319`
        """
        with contextlib.suppress(_NoChange):
            self._send(
                self._make_presence(pstatus=status, pshow="dnd", last_seen=last_seen)
            )

    def offline(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        """
        Send an "offline" presence from this contact to the user.

        :param status: eg: "Trying to make sense of XEP-0100"
        :param last_seen: For :xep:`0319`
        """
        with contextlib.suppress(_NoChange):
            self._send(
                self._make_presence(
                    pstatus=status, ptype="unavailable", last_seen=last_seen
                )
            )


def get_last_seen_fallback(last_seen: datetime) -> tuple[str, bool]:
    now = datetime.now(tz=UTC)
    if now - last_seen < timedelta(days=7):
        return f"Last seen {last_seen:%A %H:%M %p GMT}", True
    else:
        return f"Last seen {last_seen:%b %-d %Y}", False
