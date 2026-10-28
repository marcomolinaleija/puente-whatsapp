from __future__ import annotations

from pathlib import Path

SOURCE = Path("/venv/lib/python3.13/site-packages/slidge_whatsapp/group.py")
source = SOURCE.read_text(encoding="utf-8")

assert "await muc.add_to_bookmarks(auto_join=False)" in source
assert "await muc.add_to_bookmarks()" not in source

print("no-auto-join bridge runtime smoke: ok")
