"""Bounded group read horizons, acknowledged only after successful native sends."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC

READ_STATE_RETRY_SECONDS = 60
log = logging.getLogger(__name__)


def _horizon_query(orm, room_id, stanza_id):
    from slidge.db.models import ArchivedMessage
    from sqlalchemy import select

    return orm.scalar(select(ArchivedMessage).where(
        ArchivedMessage.room_id == room_id, ArchivedMessage.stanza_id == stanza_id,
    ))


def pending_ids(recipient, stanza_id):
    from slidge.db.models import ArchivedMessage, GroupMessages
    from sqlalchemy import and_, or_, select

    with recipient.xmpp.store.session() as orm:
        ref = _horizon_query(orm, recipient.stored.id, stanza_id)
        if ref is None:
            return []  # Never interpret an unknown horizon as "read everything".
        return list(orm.scalars(select(ArchivedMessage.stanza_id).where(
            ArchivedMessage.room_id == recipient.stored.id,
            ~ArchivedMessage.displayed_by_user,
            or_(ArchivedMessage.legacy_id.is_not(None),
                select(GroupMessages.id).where(
                    GroupMessages.foreign_key == recipient.stored.id,
                    GroupMessages.xmpp_id == ArchivedMessage.stanza_id,
                    GroupMessages.legacy_id.is_not(None),
                ).exists()),
            or_(ArchivedMessage.timestamp < ref.timestamp, and_(
                ArchivedMessage.timestamp == ref.timestamp, ArchivedMessage.id <= ref.id,
            )),
        ).order_by(ArchivedMessage.timestamp, ArchivedMessage.id)))


def acknowledge_id(recipient, stanza_id):
    from slidge.db.models import ArchivedMessage
    from sqlalchemy import update

    with recipient.xmpp.store.session() as orm:
        orm.execute(update(ArchivedMessage).where(
            ArchivedMessage.room_id == recipient.stored.id,
            ArchivedMessage.stanza_id == stanza_id,
        ).values(displayed_by_user=True))
        orm.commit()


def resolve_horizon(recipient, marker_id):
    from slidge.db.models import ArchivedMessage, GroupMessages
    from sqlalchemy import or_, select

    with recipient.xmpp.store.session() as orm:
        rows = list(orm.scalars(select(ArchivedMessage).where(
            ArchivedMessage.room_id == recipient.stored.id,
            or_(ArchivedMessage.stanza_id == marker_id, ArchivedMessage.legacy_id == marker_id,
                select(GroupMessages.id).where(
                    GroupMessages.foreign_key == recipient.stored.id,
                    GroupMessages.xmpp_id == ArchivedMessage.stanza_id,
                    GroupMessages.legacy_id == marker_id,
                ).exists()),
        ).order_by(ArchivedMessage.timestamp.desc(), ArchivedMessage.id.desc())))
        horizons = []
        for row in rows:
            ids = set(orm.scalars(select(GroupMessages.legacy_id).where(
                GroupMessages.foreign_key == recipient.stored.id,
                GroupMessages.xmpp_id == row.stanza_id,
                GroupMessages.legacy_id.is_not(None),
            )))
            if row.legacy_id:
                ids.add(row.legacy_id)
            if len(ids) != 1:
                return None  # Missing or conflicting identity is never "read all".
            horizons.append((row.timestamp, ids.pop()))
        if not horizons or len({native for _stamp, native in horizons}) != 1:
            return None
        stamp, native_id = horizons[0]
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return int(stamp.timestamp()), native_id


def original_timestamp(recipient, native_id):
    horizon = resolve_horizon(recipient, native_id)
    return horizon[0] if horizon else None


def native_error_kind(exc):
    # errors.Join puts the failed snapshot error last. Classify it without
    # logging raw errors (which may contain IDs, XML, keys or private paths).
    text = str(exc).split("\n")[-1].casefold()
    for pattern, kind in (
        ("mismatching lthash", "snapshot_hash"),
        ("app state key", "missing_key"),
        ("context deadline exceeded", "timeout"),
        ("mismatching", "integrity"),
        ("failed to fetch", "snapshot_fetch"),
        ("connection", "connection"),
    ):
        if pattern in text:
            return kind
    return "native_error"


async def _db(recipient, method, *args):
    executor = recipient.xmpp._can_message_info_store
    return await executor.run(method, recipient, *args)


async def unread_up_to(recipient, stanza_id):
    return await _db(recipient, pending_ids, stanza_id)


async def acknowledge(recipient, stanza_id):
    await _db(recipient, acknowledge_id, stanza_id)


async def native_with_retry(method, payload):
    # Both operations are idempotent. Keep retries bounded and surface final failure.
    for attempt in range(3):
        try:
            return await asyncio.to_thread(method, payload)
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep((0.2, 0.5)[attempt])


async def send_receipt(recipient, receipt):
    await native_with_retry(recipient.wa.SendReceipt, receipt)


async def sync_group_read(recipient, native_id):
    if not recipient.get_wa_chat().IsGroup:
        return
    # App-state versions are shared across chats of one WhatsApp session.
    # Serialize only these mutations, never the network loop or other accounts.
    lock = getattr(recipient.session, "_can_read_state_lock", None)
    if lock is None:
        lock = recipient.session._can_read_state_lock = asyncio.Lock()
    async with lock:
        if asyncio.get_running_loop().time() < getattr(
            recipient.session, "_can_read_state_retry_after", 0
        ):
            from slixmpp.exceptions import XMPPError

            raise XMPPError(
                "internal-server-error",
                "La sincronización de lectura con WhatsApp sigue pendiente; "
                "podrás reintentar en un minuto.",
                clear=False,
            )
        await _sync_group_read(recipient, native_id)


async def _sync_group_read(recipient, native_id):
    from slidge_whatsapp.generated import go, whatsapp
    from slixmpp.exceptions import XMPPError

    horizon = await _db(recipient, resolve_horizon, native_id)
    if not horizon or horizon[0] <= 0:
        raise XMPPError(
            "item-not-found", "No se pudo resolver el mensaje que delimita la lectura del grupo.",
            clear=False,
        )
    stamp, native_id = horizon
    previous = getattr(recipient, "_can_read_state_horizon", None)
    if previous and (stamp < previous[0] or (stamp, native_id) == previous):
        return
    actor = await recipient.get_wa_actor(native_id)
    if actor.IsMe or not actor.LID:
        raise XMPPError("bad-request", "Group read horizon must identify an incoming sender.",
                        clear=False)
    receipt = whatsapp.Receipt(
        Chat=recipient.get_wa_chat(), OriginActor=actor,
        MessageIDs=go.Slice_string([native_id]), Timestamp=stamp,
    )
    # Timestamp is the original send time, not the read time: do not clear later messages.
    try:
        # The native operation already recovers the collection and retries once.
        # Repeating it here would reset the same broken collection up to 3 times.
        await asyncio.to_thread(recipient.wa.MarkChatRead, receipt)
    except RuntimeError as exc:
        recipient.session._can_read_state_retry_after = (
            asyncio.get_running_loop().time() + READ_STATE_RETRY_SECONDS
        )
        log.warning("CAN_READ_STATE stage=deferred retry_seconds=%s error_kind=%s",
                    READ_STATE_RETRY_SECONDS, native_error_kind(exc))
        raise XMPPError(
            "internal-server-error",
            "No se pudo sincronizar la lectura del grupo con WhatsApp; "
            "podrás reintentar en un minuto.",
            clear=False,
        ) from exc
    recipient._can_read_state_horizon = (stamp, native_id)
    recipient.session._can_read_state_retry_after = 0
