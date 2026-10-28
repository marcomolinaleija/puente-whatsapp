"""Keep the WhatsApp profile picture independent of XMPP avatar state."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def safe_avatar_preferences(fields: list[Any]) -> list[Any]:
    """Copy the base form so other gateway classes keep their own defaults."""
    fields = deepcopy(fields)
    for field in fields:
        if field.var == "sync_avatar":
            field.value = "false"
    return fields


def disable_avatar_sync(store: Any) -> int:
    """Persist the safe preference for existing accounts before any login."""
    from slidge.db import GatewayUser

    changed = 0
    with store.session(expire_on_commit=False) as orm:
        for user in orm.query(GatewayUser).all():
            preferences = dict(user.preferences or {})
            if preferences.get("sync_avatar") is False and user.avatar_hash is None:
                continue
            preferences["sync_avatar"] = False
            user.preferences = preferences
            user.avatar_hash = None
            changed += 1
        if changed:
            orm.commit()
    return changed
