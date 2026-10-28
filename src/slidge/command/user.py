# Commands available to users
import contextlib
from copy import deepcopy
from typing import Any, cast

from slixmpp import JID
from slixmpp.exceptions import XMPPError

from slidge.db.meta import JSONSerializable
from slidge.db.models import Space

from ..util.types import (
    AnyMUC,
    AnySession,
    MucType,
    UserPreferences,
)
from .base import (
    Command,
    CommandAccess,
    Confirmation,
    ConfirmationSession,
    Form,
    FormField,
    FormSession,
    FormValues,
    SearchResult,
    TableResult,
)
from .categories import CONTACTS, GROUPS, SPACES


class Search(Command[AnySession]):
    NAME = "🔎 Search for contacts"
    HELP = "Search for contacts via this gateway"
    CHAT_COMMAND = "find"
    NODE = CONTACTS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = CONTACTS

    async def run(
        self, session: "AnySession | None", _ifrom: JID, *args: str
    ) -> FormSession[AnySession] | SearchResult | None:
        assert session is not None
        await session.ready
        if args:
            return await session.on_search(
                {self.xmpp.SEARCH_FIELDS[0].var: " ".join(args)}
            )
        return FormSession[AnySession](
            title=self.xmpp.SEARCH_TITLE,
            instructions=self.xmpp.SEARCH_INSTRUCTIONS,
            fields=self.xmpp.SEARCH_FIELDS,
            handler=self.search,
        )

    @staticmethod
    async def search(
        form_values: FormValues, session: "AnySession | None", _ifrom: JID
    ) -> SearchResult:
        assert session is not None
        results = await session.on_search(form_values)  # type: ignore
        if results is None:
            raise XMPPError("item-not-found", "No contact was found")

        return results


class SyncContacts(Command[AnySession]):
    NAME = "🔄 Sync XMPP roster"
    HELP = (
        "Synchronize your XMPP roster with your legacy contacts. "
        "Slidge will only add/remove/modify contacts in its dedicated roster group"
    )
    CHAT_COMMAND = "sync-contacts"
    NODE = CONTACTS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = CONTACTS

    async def run(
        self,
        session: "AnySession | None",
        _ifrom: JID,
        *args: str,
    ) -> ConfirmationSession[AnySession]:
        assert session is not None
        await session.ready
        return ConfirmationSession[AnySession](
            prompt="Are you sure you want to sync your roster?",
            success=None,
            handler=self.sync,
        )

    async def sync(self, session: "AnySession | None", _ifrom: JID) -> str:
        if session is None:
            raise RuntimeError
        roster_iq = await self.xmpp["xep_0356"].get_roster(session.user_jid.bare)

        contacts = session.contacts.known_contacts()

        added = 0
        removed = 0
        updated = 0
        keep_non_friends = session.user.preferences.get(
            "roster_add_non_friends", True
        )
        for item in roster_iq["roster"]:
            groups = set(item["groups"])
            if self.xmpp.ROSTER_GROUP not in groups:
                continue

            contact = contacts.pop(item["jid"], None)
            keep_contact = contact is not None and (
                contact.is_friend or keep_non_friends
            )
            if not keep_contact:
                if len(groups) == 1:
                    await self.xmpp["xep_0356"].set_roster(
                        session.user_jid, {item["jid"]: {"subscription": "remove"}}
                    )
                    if contact is not None:
                        contact.added_to_roster = False
                    removed += 1
                else:
                    groups.remove(self.xmpp.ROSTER_GROUP)
                    await self.xmpp["xep_0356"].set_roster(
                        session.user_jid,
                        {
                            item["jid"]: {
                                "subscription": item["subscription"],
                                "name": item["name"],
                                "groups": groups,
                            }
                        },
                    )
                    if contact is not None:
                        contact.added_to_roster = False
                    updated += 1
            elif contact.name != item["name"]:
                await contact.add_to_roster(force=True)
                updated += 1

        # we popped before so this only acts on slidge contacts not in the xmpp roster
        for contact in contacts.values():
            if not contact.is_friend and not keep_non_friends:
                continue
            added += 1
            await contact.add_to_roster()

        return f"{added} added, {removed} removed, {updated} updated"


class ListContacts(Command[AnySession]):
    NAME = HELP = "👤 List your legacy contacts"
    CHAT_COMMAND = "contacts"
    NODE = CONTACTS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = CONTACTS

    async def run(
        self,
        session: "AnySession | None",
        _ifrom: JID,
        *_: str,
    ) -> TableResult:
        assert session is not None
        await session.ready
        contacts = sorted(
            session.contacts, key=lambda c: c.name.casefold() if c.name else ""
        )
        return TableResult(
            description="Your buddies",
            fields=[FormField("name"), FormField("jid", type="jid-single")],
            items=[{"name": c.name, "jid": c.jid.bare} for c in contacts],
        )


class ListGroups(Command[AnySession]):
    NAME = HELP = "👥 List your legacy groups"
    CHAT_COMMAND = "groups"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = GROUPS

    async def run(
        self, session: "AnySession | None", _ifrom: JID, *_: str
    ) -> TableResult:
        assert session is not None
        await session.ready
        groups: list[AnyMUC] = sorted(
            session.bookmarks, key=lambda g: (g.name or g.jid.node).casefold()
        )
        return TableResult(
            description="Your groups",
            fields=[FormField("name"), FormField("jid", type="jid-single")],
            items=[
                {"name": g.name or str(g.legacy_id), "jid": g.jid.bare} for g in groups
            ],
            jids_are_mucs=True,
        )


class ListSpaces(Command[AnySession]):
    NAME = "🌐 List my spaces"
    HELP = "List the spaces you are part of. Spaces are collections of rooms."
    CHAT_COMMAND = "spaces"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = SPACES

    related_to_spaces = True

    async def run(
        self, session: "AnySession | None", _ifrom: JID, *_: str
    ) -> TableResult:
        assert session is not None
        spaces = await _get_updated_spaces(session)
        return TableResult(
            description="Your spaces. If your client does not support spaces, use the 'space-rooms' command.",
            fields=[FormField("name"), FormField("iri")],
            items=[
                {
                    "name": s.name or str(s.legacy_id),
                    "iri": f"xmpp:{self.xmpp.boundjid.bare}?;node={await session.bookmarks.space_legacy_id_to_node(s.legacy_id)}",
                }
                for s in spaces
            ],
            jids_are_mucs=True,
        )


class ListRoomsInSpace(Command[AnySession]):
    NAME = "🌐 List the rooms in a space"
    HELP = "List the rooms of a space you are part of. Spaces are collections of rooms."
    CHAT_COMMAND = "space-rooms"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = SPACES

    async def run(
        self, session: "AnySession | None", _ifrom: JID, *_: str
    ) -> "FormSession[AnySession]":
        assert session is not None
        spaces = await _get_updated_spaces(session)
        return FormSession(
            title="Your spaces",
            instructions="Select a space to view its rooms",
            fields=[
                FormField(
                    var="space_legacy_id",
                    label="Space",
                    required=True,
                    type="list-single",
                    options=[
                        {
                            "label": s.name or str(s.legacy_id),
                            "value": str(s.legacy_id),
                        }
                        for s in spaces
                    ],
                )
            ],
            handler=self.list_rooms,
            handler_args=({str(s.legacy_id): s.name for s in spaces},),
        )

    async def list_rooms(
        self,
        form_values: FormValues,
        session: "AnySession | None",
        _ifrom: JID,
        space_names: dict[str, str],
    ) -> TableResult:
        assert session is not None
        space_legacy_id = form_values.get("space_legacy_id")
        if space_legacy_id is None:
            raise XMPPError("bad-request", "You need to specify a space")
        assert isinstance(space_legacy_id, str)
        await session.ready
        with self.xmpp.store.session() as orm:
            rooms = sorted(
                list(
                    self.xmpp.store.spaces.get_rooms(
                        orm,
                        session.user_pk,
                        await session.bookmarks.space_legacy_id_to_node(
                            space_legacy_id
                        ),
                    )
                ),
                key=lambda r: (r.name or str(r.jid.node)).casefold(),
            )
        name = space_names.get(space_legacy_id)
        return TableResult(
            fields=[
                FormField("name", "Name"),
                FormField("jid", "JID", type="jid-single"),
            ],
            description=f"Rooms of '{name or space_legacy_id}'",
            items=[{"name": r.name or str(r.legacy_id), "jid": r.jid} for r in rooms],
            jids_are_mucs=True,
        )


class Login(Command[AnySession]):
    NAME = "🔐 Re-login to the legacy network"
    HELP = "Login to the legacy service"
    CHAT_COMMAND = "re-login"
    NODE = "https://slidge.im/command/core/" + CHAT_COMMAND

    ACCESS = CommandAccess.USER_NON_LOGGED

    async def run(
        self,
        session: "AnySession | None",
        _ifrom: JID,
        *_: str,
    ) -> str:
        assert session is not None
        if session.is_logging_in:
            raise XMPPError("bad-request", "You are already logging in.")
        session.is_logging_in = True
        try:
            msg = await self.xmpp.login_wrap(session)
        except Exception as e:
            session.send_gateway_status(f"Re-login failed: {e}", show="dnd")
            raise XMPPError(
                "internal-server-error", etype="wait", text=f"Could not login: {e}"
            )
        finally:
            session.is_logging_in = False
        session.logged = True
        session.send_gateway_status(msg or "Re-connected", show="chat")
        session.send_gateway_message(msg or "Re-connected")
        return msg


class CreateGroup(Command[AnySession]):
    NAME = "🆕 New legacy group"
    HELP = "Create a group on the legacy service"
    CHAT_COMMAND = "create-group"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    CATEGORY = GROUPS

    ACCESS = CommandAccess.USER_LOGGED

    async def run(
        self,
        session: "AnySession | None",
        _ifrom: JID,
        *_: str,
    ) -> FormSession[AnySession]:
        assert session is not None
        await session.ready
        contacts = session.contacts.known_contacts(only_friends=True)
        return FormSession(
            title="Create a new group",
            instructions="Pick contacts that should be part of this new group",
            fields=[
                FormField(var="group_name", label="Name of the group", required=True),
                FormField(
                    var="contacts",
                    label="Contacts to add to the new group",
                    type="list-multi",
                    options=[
                        {"value": str(contact.jid), "label": contact.name}
                        for contact in sorted(contacts.values(), key=lambda c: c.name)
                    ],
                    required=False,
                ),
            ],
            handler=self.finish,
        )

    @staticmethod
    async def finish(
        form_values: FormValues,
        session: "AnySession | None",
        *_: Any,  # noqa:ANN401
    ) -> TableResult:
        assert session is not None
        legacy_id: str = await session.on_create_group(
            cast(str, form_values["group_name"]),
            [
                await session.contacts.by_jid(JID(j))
                for j in form_values.get("contacts", [])  # type:ignore
            ],
        )
        muc = await session.bookmarks.by_legacy_id(legacy_id)
        return TableResult(
            description=f"Your new group: xmpp:{muc.jid}?join",
            fields=[FormField("name"), FormField("jid", type="jid-single")],
            items=[{"name": muc.name, "jid": muc.jid}],
            jids_are_mucs=True,
        )


class Preferences(Command[AnySession]):
    NAME = "⚙️ Preferences"
    HELP = "Customize the gateway behaviour to your liking"
    CHAT_COMMAND = "preferences"
    NODE = "https://slidge.im/command/core/preferences"
    ACCESS = CommandAccess.USER

    async def run(
        self,
        session: "AnySession | None",
        _ifrom: JID,
        *_: str,
    ) -> FormSession[AnySession]:
        fields = deepcopy(self.xmpp.PREFERENCES)
        assert session is not None
        current = session.user.preferences
        for field in fields:
            field.value = current.get(field.var, field.value)  # type:ignore
        return Form(
            title="Preferences",
            instructions=self.HELP,
            fields=fields,
            handler=self.finish,  # type:ignore
            handler_kwargs={"previous": current},
        )

    async def finish(
        self,
        form_values: UserPreferences,
        session: "AnySession | None",
        *_: Any,  # noqa:ANN401
        previous: JSONSerializable,
    ) -> str:
        assert session is not None
        if previous == form_values:
            return "No preference was changed"

        previous = previous.copy()
        user = session.user
        user.preferences.update(form_values)  # type:ignore
        self.xmpp.store.users.update(user)

        with contextlib.suppress(NotImplementedError):
            await session.on_preferences(previous, form_values)  # type:ignore[arg-type]

        if not previous["sync_avatar"] and form_values["sync_avatar"]:
            await self.xmpp.fetch_user_avatar(session)
        else:
            user.avatar_hash = None

        return "Your preferences have been updated."


class Unregister(Command[AnySession]):
    NAME = "❌ Unregister from the gateway"
    HELP = "Unregister from the gateway"
    CHAT_COMMAND = "unregister"
    NODE = "https://slidge.im/command/core/unregister"
    ACCESS = CommandAccess.USER

    async def run(
        self, session: "AnySession | None", _ifrom: JID, *_: str
    ) -> ConfirmationSession[AnySession]:
        return ConfirmationSession[AnySession](
            prompt=f"Are you sure you want to unregister from '{self.xmpp.boundjid}'?",
            success=f"You are not registered to '{self.xmpp.boundjid}' anymore.",
            handler=self.unregister,
        )

    async def unregister(self, session: "AnySession | None", _ifrom: JID) -> str:
        assert session is not None
        await self.xmpp.unregister_user(session.user)
        return "You are not registered anymore. Bye!"


class LeaveGroup(Command[AnySession]):
    NAME = HELP = "❌ Leave a legacy group"
    CHAT_COMMAND = "leave-group"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = GROUPS

    async def run(
        self,
        session: "AnySession | None",
        ifrom: JID,
        *_: str,
    ) -> FormSession[AnySession]:
        assert session is not None
        await session.ready
        groups = sorted(session.bookmarks, key=lambda g: g.DISCO_NAME.casefold())
        return FormSession(
            title="Leave a group",
            instructions="Select the group you want to leave",
            fields=[
                FormField(
                    "group",
                    "Group name",
                    type="list-single",
                    options=[
                        {"label": g.name or str(g.legacy_id), "value": str(i)}
                        for i, g in enumerate(groups)
                    ],
                )
            ],
            handler=self.confirm,
            handler_args=(groups,),
        )

    async def confirm(
        self,
        form_values: FormValues,
        _session: "AnySession | None",
        _ifrom: JID,
        groups: list[AnyMUC],
    ) -> Confirmation:
        group = groups[int(form_values["group"])]  # type:ignore
        return Confirmation(
            prompt=f"Are you sure you want to leave the group '{group.name}'?",
            handler=self.finish,
            handler_args=(group,),
        )

    @staticmethod
    async def finish(session: AnySession, _ifrom: JID, group: AnyMUC) -> None:
        await group.on_leave()
        await session.bookmarks.remove(group, reason="You left this group via slidge.")


class InviteInGroups(Command[AnySession]):
    NAME = "💌 Re-invite me in my groups"
    HELP = "Ask the gateway to send invitations for all your private groups"
    CHAT_COMMAND = "re-invite"
    NODE = GROUPS.node + "/" + CHAT_COMMAND
    ACCESS = CommandAccess.USER_LOGGED
    CATEGORY = GROUPS

    async def run(self, session: "AnySession | None", _ifrom: JID, *_: str) -> None:
        assert session is not None
        await session.ready
        for muc in session.bookmarks:
            if muc.type == MucType.GROUP:
                session.send_gateway_invite(
                    muc, reason="You asked to be re-invited in all groups."
                )


async def _get_updated_spaces(session: AnySession) -> list[Space]:
    await session.ready
    with session.xmpp.store.session() as orm:
        spaces = list(session.xmpp.store.spaces.get_all(orm, session.user_pk))
    updated_spaces: list[Space] = []
    for space in spaces:
        try:
            updated_spaces.append(await session.bookmarks.update_space_if_needed(space))
        except Exception:
            session.log.exception(
                "Something went wrong trying to update space '%r'", space
            )

    return sorted(updated_spaces, key=lambda s: s.name or str(s.legacy_id))
