"""Backport upstream a238105488 to pinned Whatsmeow, preserving voice dependencies."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# MPL-2.0 upstream change, Tulir Asokan, 2026-07-20:
# https://github.com/tulir/whatsmeow/commit/a2381054887e16d22428430fdc4d2b027a7f29a7
PINNED_HASHES = {
    "client.go": "ed30c643215c712c77199bff49942350f21bdcd6eca69a7c4420a2d948a47963",
    "message.go": "70248732c0b1f7db381fcee69725c1387643815cce73584329bd7419a22a48ca",
    "types/message.go": "58fac2e88f841172169c5c58a7ff07a7de2586fd7945c3e196b47a025d2e9349",
}
HANDLER = '''func (cli *Client) handleUnencryptedMessage(ctx context.Context, node *waBinary.Node) {
	info, err := cli.parseMessageInfo(node)
	if err != nil {
		cli.Log.Warnf("Failed to parse message: %v", err)
		cli.sendAck(ctx, node, NackParsingError)
		return
	}
	if info.Sender.Server != types.NewsletterServer {
		cli.sendAck(ctx, node, 0)
		return
	}
	info.IsNewsletterStatus = true
	var cancelled bool
	defer cli.maybeDeferredAck(ctx, node)(&cancelled)
	cancelled = cli.handlePlaintextMessage(ctx, info, node)
}

'''


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise ValueError("Upstream patch anchor changed")
    return source.replace(old, new, 1)


def apply(root: Path) -> None:
    sources = {}
    # Check every input before writing. Never patch an unknown dependency version.
    for name, expected in PINNED_HASHES.items():
        raw = (root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"Pinned Whatsmeow source mismatch: {name}")
        sources[name] = raw.decode()
    sources["client.go"] = replace_once(
        sources["client.go"], '\t\t"message":      cli.handleEncryptedMessage,\n',
        '\t\t"message":      cli.handleEncryptedMessage,\n'
        '\t\t"status":       cli.handleUnencryptedMessage,\n',
    )
    sources["message.go"] = replace_once(
        sources["message.go"], "func (cli *Client) parseMessageSource(",
        HANDLER + "func (cli *Client) parseMessageSource(",
    )
    sources["message.go"] = replace_once(
        sources["message.go"], '\tif !ag.OK() {\n\t\treturn nil, ag.Error()\n\t}\n',
        '\tif err = ag.Error(); err != nil {\n\t\treturn nil, err\n\t}\n',
    )
    sources["types/message.go"] = replace_once(
        sources["types/message.go"], '\tMsgMetaInfo MsgMetaInfo\n',
        '\tMsgMetaInfo MsgMetaInfo\n\n\tIsNewsletterStatus bool\n',
    )
    for name, source in sources.items():
        (root / name).write_bytes(source.encode())
    print("Applied pinned Whatsmeow status-node ACK backport a238105488")


if __name__ == "__main__":
    apply(Path(sys.argv[1]))
