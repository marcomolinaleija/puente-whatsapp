from slixmpp.jid import unescape_node


class EscapeMixin:
    async def legacy_id_to_jid_username(self, legacy_id: str) -> str:
        """
        Convert a legacy ID to a valid 'user' part of a JID.

        The default implementation uses :xep:`0106`.

        Should be overridden for cases where the str conversion of
        the legacy_id is not enough, e.g., if it is case-sensitive or contains
        forbidden characters not covered by :xep:`0106`.

        :param legacy_id: The identifier to convert.
        :return: A valid username part (or "local part") of a JID.
        """
        return str(legacy_id).translate(ESCAPE_TABLE)

    async def jid_username_to_legacy_id(self, jid_username: str) -> str:
        """
        Convert a JID user part to a legacy ID.

        The default implementation uses :xep:`0106`.

        Should be overridden in case legacy IDs are not strings, or more
        generally for any case where the username part of a JID
        is not enough to identify a contact on the legacy network.

        :param jid_username: User part of a JID, ie "user" in "user@example.com"
        :return: The string representation of an identifier on the legacy network.
        """
        return unescape_node(jid_username)  # type:ignore[no-any-return]


JID_ESCAPE_SEQUENCES = {
    "\\20",
    "\\22",
    "\\26",
    "\\27",
    "\\2f",
    "\\3a",
    "\\3c",
    "\\3e",
    "\\40",
    "\\5c",
}


JID_UNESCAPE_TRANSFORMATIONS = {
    "\\20": " ",
    "\\22": '"',
    "\\26": "&",
    "\\27": "'",
    "\\2f": "/",
    "\\3a": ":",
    "\\3c": "<",
    "\\3e": ">",
    "\\40": "@",
    "\\5c": "\\",
}


ESCAPE_TABLE = "".maketrans({v: k for k, v in JID_UNESCAPE_TRANSFORMATIONS.items()})

__all__ = "ESCAPE_TABLE", "unescape_node"
