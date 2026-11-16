"""Backport upstream bot-message fixes to pinned Whatsmeow (applied after the status patch).

MPL-2.0 upstream code, Tulir Asokan, whatsmeow ecc39170bc14 (2026-11-13):
- "full" bot edits carry edit_target_id like "inner"/"last".
- When decrypting with the edit target fails, retry with the message's own ID.
Every anchor must match exactly once; an unknown source fails the build.
"""
from __future__ import annotations

import sys
from pathlib import Path

EDIT_TYPES_OLD = "\tEditTypeLast  BotEditType = \"last\"\n"
EDIT_TYPES_NEW = EDIT_TYPES_OLD + "\tEditTypeFull  BotEditType = \"full\"\n"

PARSE_OLD = "\tif botInfo.EditType == types.EditTypeInner || botInfo.EditType == types.EditTypeLast {\n"
PARSE_NEW = (
    "\tif botInfo.EditType == types.EditTypeInner || botInfo.EditType == types.EditTypeLast"
    " || botInfo.EditType == types.EditTypeFull {\n"
)

DECRYPT_OLD = (
    "\t\t\t\tdecrypted, err = cli.decryptBotMessage(ctx, messageSecret, &msMsg, decryptMessageID, targetSenderJID, info)\n"
    "\t\t\t}\n"
)
DECRYPT_NEW = (
    "\t\t\t\tdecrypted, err = cli.decryptBotMessage(ctx, messageSecret, &msMsg, decryptMessageID, targetSenderJID, info)\n"
    "\t\t\t\tif err != nil && decryptMessageID != info.ID {\n"
    "\t\t\t\t\tdecrypted, err = cli.decryptBotMessage(ctx, messageSecret, &msMsg, info.ID, targetSenderJID, info)\n"
    "\t\t\t\t}\n"
    "\t\t\t}\n"
)

TARGET_OLD = "\t\t\tif info.MsgBotInfo.EditType == types.EditTypeInner || info.MsgBotInfo.EditType == types.EditTypeLast {\n"
TARGET_NEW = (
    "\t\t\tif (info.MsgBotInfo.EditType == types.EditTypeInner || info.MsgBotInfo.EditType == types.EditTypeLast"
    " || info.MsgBotInfo.EditType == types.EditTypeFull) && info.MsgBotInfo.EditTargetID != \"\" {\n"
)


def replace_once(source: str, old: str, new: str, name: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"Upstream bot patch anchor changed: {name}")
    return source.replace(old, new, 1)


def apply(root: Path) -> None:
    types_path = root / "types" / "message.go"
    message_path = root / "message.go"
    types_src = replace_once(types_path.read_text(), EDIT_TYPES_OLD, EDIT_TYPES_NEW, "edit types")
    message_src = message_path.read_text()
    message_src = replace_once(message_src, PARSE_OLD, PARSE_NEW, "parse bot info")
    message_src = replace_once(message_src, TARGET_OLD, TARGET_NEW, "decrypt target")
    message_src = replace_once(message_src, DECRYPT_OLD, DECRYPT_NEW, "decrypt fallback")
    types_path.write_text(types_src)
    message_path.write_text(message_src)
    print("Applied pinned Whatsmeow bot-message backport ecc39170bc14")


if __name__ == "__main__":
    apply(Path(sys.argv[1]))
