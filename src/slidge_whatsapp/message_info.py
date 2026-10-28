"""Private, persistent group-message receipts; deployed inside slidge_whatsapp.

No message bodies or credentials are stored. SQLite work uses one dedicated worker.
All IQs are scoped to the requesting registered session and its existing room.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree as ET

NAMESPACE = "urn:can:whatsapp:message-info:0"
MAX_PARTICIPANTS = 2048
log = logging.getLogger(__name__)


def merge_rows(old: list[dict], new: list[dict]) -> list[dict]:
    rows = list(old)
    for incoming in new:
        row = dict(incoming)
        if not row.get("jid") and not row.get("lid"):
            continue
        matches = [
            i
            for i, prior in enumerate(rows)
            if (row.get("lid") and prior.get("lid") == row["lid"])
            or (row.get("jid") and prior.get("jid") == row["jid"])
        ]
        for i in reversed(matches):
            prior = rows.pop(i)
            for field in ("jid", "lid", "name"):
                row[field] = row.get(field) or prior.get(field, "")
            for field in ("delivered-at", "read-at"):
                a, b = int(row.get(field, 0)), int(prior.get(field, 0))
                row[field] = min(a, b) if a and b else a or b
        rows.append(row)
    if len(rows) > MAX_PARTICIPANTS:
        raise ValueError("Too many receipt participants")
    return sorted(rows, key=lambda p: p.get("lid") or p["jid"])


class InfoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="message-info")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with private permissions before writing any identities.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS info (
            account TEXT NOT NULL, room TEXT NOT NULL, message TEXT NOT NULL,
            data TEXT NOT NULL, PRIMARY KEY(account, room, message))""")
        return conn

    async def run(self, method, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, method, *args)

    def update(
        self,
        account: str,
        room: str,
        message: str,
        own: bool,
        complete: bool,
        participants: list[dict],
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "SELECT data FROM info WHERE account=? AND room=? AND message=?",
                (account, room, message),
            ).fetchone()
            data = (
                json.loads(result[0]) if result else {"own": False, "complete": False, "rows": []}
            )
            data["own"] = data["own"] or own
            data["complete"] = data["complete"] or complete
            data["rows"] = merge_rows(data["rows"], participants)
            conn.execute(
                "INSERT OR REPLACE INTO info VALUES (?, ?, ?, ?)",
                (account, room, message, json.dumps(data)),
            )
        return data

    def get(self, account: str, room: str, message: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM info WHERE account=? AND room=? AND message=?",
                (account, room, message),
            ).fetchone()
        return json.loads(row[0]) if row else None


def store(session):
    return session.xmpp._can_message_info_store


async def prepare_outgoing(recipient, message) -> tuple[bool, list[dict]]:
    """Capture membership before sending, not when Info is opened later."""
    if not message.Chat.IsGroup:
        return False, []
    rows = []
    try:
        async with asyncio.timeout(3):
            async for participant in recipient.get_participants():
                if participant.is_user:
                    continue
                contact = participant.contact
                jid = str(contact.legacy_id) if contact else ""
                lid = str(participant.occupant_id or "")
                if not lid.endswith("@lid"):
                    lid = ""
                if not jid and not lid:
                    return False, rows
                rows.append(
                    {
                        "jid": jid,
                        "lid": lid,
                        "name": participant.nickname,
                        "delivered-at": 0,
                        "read-at": 0,
                    }
                )
                if len(rows) >= MAX_PARTICIPANTS:
                    return False, rows
        complete = recipient.participants_filled and len(rows) == recipient.n_participants - 1
        return bool(complete), rows
    except Exception:
        log.warning("Unable to capture group recipients; sending remains unchanged")
        return False, []


async def outgoing_sent(recipient, message, snapshot: tuple[bool, list[dict]]) -> None:
    if not message.Chat.IsGroup:
        return
    try:
        await store(recipient.session).run(
            store(recipient.session).update,
            str(recipient.session.user_jid.bare),
            str(message.Chat.JID),
            str(message.ID),
            True,
            snapshot[0],
            snapshot[1],
        )
    except Exception:
        log.warning("Unable to save group recipients; message was already sent")


async def record_receipt(session, receipt, participant=None) -> None:
    from .generated import whatsapp

    if not receipt.Chat.IsGroup or receipt.Actor.IsMe:
        return
    field = {whatsapp.ReceiptDelivered: "delivered-at", whatsapp.ReceiptRead: "read-at"}.get(
        receipt.Kind
    )
    if field is None or not 0 < receipt.Timestamp <= 253402300799:
        return
    row = {
        "jid": str(receipt.Actor.JID),
        "lid": str(receipt.Actor.LID),
        "name": str(participant.nickname) if participant is not None else "",
        field: int(receipt.Timestamp),
    }
    try:
        for message_id in receipt.MessageIDs:
            await store(session).run(
                store(session).update,
                str(session.user_jid.bare),
                str(receipt.Chat.JID),
                str(message_id),
                False,
                False,
                [row],
            )
    except Exception:
        log.warning("Unable to save group receipt; normal marker processing continues")


async def ingest_message(session, message) -> None:
    from .generated import whatsapp

    if (
        not message.Chat.IsGroup
        or not message.Actor.IsMe
        or message.Kind
        not in (
            whatsapp.MessagePlain,
            whatsapp.MessageAttachment,
            whatsapp.MessagePoll,
        )
    ):
        return
    try:
        await store(session).run(
            store(session).update,
            str(session.user_jid.bare),
            str(message.Chat.JID),
            str(message.ID),
            True,
            False,
            [],
        )
        # Run before Slidge's duplicate-history early return: old receipts still enrich Info.
        for receipt in message.Receipts:
            await record_receipt(session, receipt)
    except Exception:
        log.warning("Unable to ingest message receipt metadata")


def info_xml(message_id: str, native_id: str, data: dict) -> ET.Element:
    node = ET.Element(
        f"{{{NAMESPACE}}}info",
        {
            "id": message_id,
            "legacy-id": native_id,
            "recipients-complete": str(data["complete"]).lower(),
        },
    )
    for row in data["rows"]:
        ET.SubElement(node, f"{{{NAMESPACE}}}participant", {k: str(v) for k, v in row.items()})
    return node


def resolve_owned_message(session, room, candidates: list[str]) -> str | None:
    """Use room-scoped origin/echo maps; never correlate by text or another account."""
    with session.xmpp.store.session() as orm:
        own_ids = {
            participant.occupant_id
            for participant in session.xmpp.store.participants.get_all(orm, room.stored.id)
            if participant.is_user
        }
    own_ids.update(("slidge-user", str(session.contacts.user_legacy_id)))
    for candidate in candidates:
        with session.xmpp.store.session() as orm:
            for origin in (True, False):
                native = session.xmpp.store.id_map.get_legacy(
                    orm,
                    room.stored.id,
                    candidate,
                    True,
                    origin=origin,
                )
                if native and session.message_is_carbon(room, native):
                    return str(native)
        if session.message_is_carbon(room, candidate):
            return candidate
        for archived in room.get_archived_messages(candidate):
            if archived.occupant_id in own_ids:
                # The archive message id is the native WhatsApp ID for group messages.
                return str(archived.stanza["id"])
    return None


async def handle_info(gateway, iq) -> None:
    from slixmpp.exceptions import XMPPError

    try:
        if iq["type"] != "get":
            raise XMPPError("bad-request")
        session = gateway.get_session_from_jid(iq["from"])
        if session is None:
            raise XMPPError("forbidden")
        if iq["to"].resource:
            raise XMPPError("bad-request")
        room = session.bookmarks.by_jid_only_if_exists(iq["to"])
        if room is None:
            raise XMPPError("item-not-found")
        node = iq.xml.find(f"{{{NAMESPACE}}}info")
        if node is None:
            raise XMPPError("bad-request")
        requested = node.get("id", "")
        reference = node.get("reference", "")
        if not requested or len(requested) > 512 or len(reference) > 512:
            raise XMPPError("bad-request")
        account, group = str(session.user_jid.bare), str(room.legacy_id)
        native = None
        data = None
        for candidate in dict.fromkeys([requested, reference]):
            if not candidate:
                continue
            data = await store(session).run(store(session).get, account, group, candidate)
            if data and data["own"]:
                native = candidate
                break
        if native is None:
            # Slidge ORM operations must run outside the global network callback.
            native = await store(session).run(
                resolve_owned_message, session, room, [x for x in (requested, reference) if x]
            )
            if native is None:
                raise XMPPError("forbidden")
            data = await store(session).run(
                store(session).update, account, group, native, True, False, []
            )
        reply = iq.reply()
        reply.append(info_xml(requested, native, data))
        reply.send()
    except XMPPError as exc:
        raise exc
    except Exception:
        log.exception("Group message info query failed")
        raise XMPPError("internal-server-error") from None


def install(gateway) -> None:
    from slidge import global_config
    from slixmpp.xmlstream.handler import CoroutineCallback
    from slixmpp.xmlstream.matcher import MatchXPath

    gateway._can_message_info_store = InfoStore(
        Path(global_config.HOME_DIR) / "message-info.sqlite3"
    )
    gateway["xep_0030"].add_feature(NAMESPACE)

    async def on_info(iq) -> None:
        await handle_info(gateway, iq)

    gateway.register_handler(
        CoroutineCallback(
            "CAN group message info",
            MatchXPath(f"{{{gateway.default_ns}}}iq/{{{NAMESPACE}}}info"),
            on_info,
        )
    )
