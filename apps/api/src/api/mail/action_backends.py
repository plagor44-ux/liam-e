"""Provider mutations; callers retain the pre-action index snapshot for undo."""

import asyncio
import imaplib
import json
import re
from typing import Protocol
from urllib.parse import quote

import httpx
from db.accessors import mail_message as message_access
from db.models.mail_account import MailAccount, gmail_studio
from db.models.mail_message import IMAP_RETIRED, MailMessage

from api.mail import connectors, providers, service, transport
from api.mail.actions import ActionKind, ActionUnsupportedError, write_connector_labels
from api.mail.seed_ids import is_seed_account


class ProviderBackend(Protocol):
    async def apply(self, message: MailMessage, kind: ActionKind) -> dict: ...
    async def revert(self, message: MailMessage, previous: dict) -> dict: ...


def backend_for(account: MailAccount) -> ProviderBackend:
    if is_seed_account(account.id):
        return IndexBackend()
    if account.auth_type == connectors.CONNECTOR:
        return LabelBackend(account, connector=True)
    if account.provider == providers.IMAP:
        return ImapBackend(account)
    if account.provider == providers.GOOGLE:
        return LabelBackend(account)
    if account.provider == providers.MICROSOFT:
        return GraphBackend(account)
    raise ActionUnsupportedError()


class IndexBackend:
    async def apply(self, message: MailMessage, kind: ActionKind) -> dict:
        return {}

    async def revert(self, message: MailMessage, previous: dict) -> dict:
        return {}


async def _headers(account: MailAccount) -> dict[str, str]:
    credentials = await service.load_credentials(account)
    return {"Authorization": f"Bearer {await service.valid_access_token(account, credentials)}"}


class LabelBackend:
    def __init__(self, account: MailAccount, *, connector: bool = False):
        self.account = account
        self.connector = connector

    async def apply(self, message: MailMessage, kind: ActionKind) -> dict:
        # Folder state remains authoritative even if a connector omitted labels.
        current = set(json.loads(message.labels_json))
        if message.folder in ("INBOX", "TRASH", "SPAM"):
            current.add(message.folder)
        if kind == "archive":
            desired = sorted(current - {"INBOX"})
        elif kind == "trash":
            desired = sorted((current - {"INBOX"}) | {"TRASH"})
        elif kind == "spam":
            desired = sorted((current - {"INBOX"}) | {"SPAM"})
        elif kind == "restore":
            desired = sorted((current - {"TRASH"}) | {"INBOX"} | (set() if message.is_read else {"UNREAD"}))
        elif kind == "mark_read":
            current.add("UNREAD")
            desired = sorted(current - {"UNREAD"})
        else:
            current.discard("UNREAD")
            desired = sorted(current | {"UNREAD"})
        return await self._change(message, current, set(desired), kind=kind)

    async def revert(self, message: MailMessage, previous: dict, *, kind: ActionKind | None = None) -> dict:
        current = set(json.loads(message.labels_json))
        desired = set(json.loads(previous["labels_json"]))
        if message.folder in ("INBOX", "TRASH", "SPAM"):
            current.add(message.folder)
        desired.difference_update({"INBOX", "TRASH", "SPAM", "UNREAD"})
        if previous["folder"] in ("INBOX", "TRASH", "SPAM"):
            desired.add(previous["folder"])
        if not previous["is_read"]:
            desired.add("UNREAD")
        if gmail_studio(self.account):
            # Undo restores the labels controlled by this action and leaves other provider changes alone.
            managed = {"UNREAD"} if kind in {"mark_read", "mark_unread"} else {"INBOX", "TRASH"}
            destination = previous["folder"] if managed != {"UNREAD"} else None
            return await self._change(message, current, (current - managed) | (desired & managed), destination=destination)
        await self._change(message, current, desired)
        return {"labels_json": previous["labels_json"]}

    async def _change(
        self, message: MailMessage, current: set[str], desired: set[str], *,
        kind: ActionKind | None = None, destination: str | None = None,
    ) -> dict:
        trash_transition = ("TRASH" in desired) != (message.folder == "TRASH")
        if kind == "trash" or (kind in (None, "restore") and trash_transition):
            # Reassert both folder labels. Legacy copies can contain contradictory labels.
            folder_labels = {"TRASH", "INBOX"}
            current = (current - folder_labels) | (folder_labels - desired)
        added, removed = sorted(desired - current), sorted(current - desired)
        if gmail_studio(self.account):
            action = kind
            if action is None:
                if "TRASH" in added:
                    action = "trash"
                elif "INBOX" in added or "TRASH" in removed:
                    action = "restore"
                elif "INBOX" in removed:
                    action = "archive"
                else:
                    action = "mark_unread" if "UNREAD" in added else "mark_read"
            if kind is not None:
                destination = {"archive": "ARCHIVE", "trash": "TRASH", "restore": "INBOX"}.get(kind)
            before, labels = await connectors.change_gmail_labels(
                self.account, message, added, removed, action=action, destination=destination)
            updates = {"folder": connectors.observed_folder(labels), "is_read": "UNREAD" not in labels,
                       "is_flagged": "STARRED" in labels, "labels_json": json.dumps(sorted(labels))}
            if kind is not None:
                updates["connector_previous"] = {
                    "folder": connectors.observed_folder(before), "is_read": "UNREAD" not in before,
                    "labels_json": json.dumps(sorted(before)),
                }
            return updates
        if self.connector:
            await write_connector_labels(self.account, message, added=added, removed=removed)
        else:
            headers = await _headers(self.account)
            path = f"/messages/{quote(message.provider_uid, safe='')}"
            async with httpx.AsyncClient(timeout=30, base_url=providers.GMAIL_API, headers=headers) as client:
                if kind == "trash" or ("TRASH" in added):
                    response = await client.post(path + "/trash")
                    response.raise_for_status()
                    return {"labels_json": json.dumps(sorted(desired))}
                elif "TRASH" in removed or (kind == "restore" and message.folder == "TRASH"):
                    response = await client.post(path + "/untrash")
                    response.raise_for_status()
                # TRASH is managed by its dedicated endpoints, not modify.
                added = [label for label in added if label != "TRASH"]
                removed = [label for label in removed if label != "TRASH"]
                if added or removed:
                    response = await client.post(path + "/modify", json={"addLabelIds": added, "removeLabelIds": removed})
                    response.raise_for_status()
        return {"labels_json": json.dumps(sorted(desired))}


class GraphBackend:
    def __init__(self, account: MailAccount):
        self.account = account

    async def _change(self, message: MailMessage, *, folder: str | None = None, is_read: bool | None = None) -> dict:
        headers = await _headers(self.account)
        uid = message.provider_uid
        updates = {}
        async with httpx.AsyncClient(timeout=30, base_url=providers.GRAPH_API, headers=headers) as client:
            if is_read is not None:
                response = await client.patch(f"/me/messages/{quote(uid, safe='')}", json={"isRead": is_read})
                response.raise_for_status()
            if folder is not None and folder != message.folder:
                destination = {"INBOX": "inbox", "ARCHIVE": "archive", "TRASH": "deleteditems", "SPAM": "junkemail"}.get(folder)
                if destination is None:
                    raise ActionUnsupportedError()
                response = await client.post(f"/me/messages/{quote(uid, safe='')}/move", json={"destinationId": destination})
                response.raise_for_status()
                uid = response.json()["id"]
                if not isinstance(uid, str) or not uid:
                    raise RuntimeError("Missing Graph message id")
                updates["provider_uid"] = uid
        return updates

    async def apply(self, message: MailMessage, kind: ActionKind) -> dict:
        if kind in ("mark_read", "mark_unread"):
            return await self._change(message, is_read=kind == "mark_read")
        return await self._change(message, folder={"archive": "ARCHIVE", "trash": "TRASH", "spam": "SPAM", "restore": "INBOX"}[kind])

    async def revert(self, message: MailMessage, previous: dict) -> dict:
        updates = await self._change(message, folder=previous["folder"], is_read=previous["is_read"])
        # An earlier move's snapshot ID is no longer valid, even for read-only undo.
        return {"provider_uid": message.provider_uid} | updates


def _quoted(value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ActionUnsupportedError()
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _ok(result):
    status, data = result
    if status != "OK":
        raise RuntimeError("IMAP command failed")
    return data


def _folder(folder: str, listing: list) -> str:
    if folder == "INBOX":
        return "INBOX"
    special = {"ARCHIVE": "\\archive", "TRASH": "\\trash", "SPAM": "\\junk"}.get(folder)
    if special is None:
        raise ActionUnsupportedError()
    parsed = []
    for line in listing:
        if not isinstance(line, bytes):
            continue
        match = re.match(r'\(([^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(.+)$', line.decode())
        if not match:
            continue
        flags, name = match.groups()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r'\\(.)', r'\1', name[1:-1])
        if "\\noselect" not in flags.lower().split():
            parsed.append((flags.lower().split(), name))
    for flags, name in parsed:
        if special in flags:
            return name
    for fallback in {"ARCHIVE": ("Archive", "Archiv"), "TRASH": ("Trash", "Papierkorb", "Deleted Items"), "SPAM": ("Junk", "Spam", "Junk E-Mail", "Junk-E-Mail", "Bulk", "Bulk Mail")}[folder]:
        if any(name == fallback for _, name in parsed):
            return fallback
    raise ActionUnsupportedError()


def _search(connection, message: MailMessage, uid: str | None = None) -> str:
    if not message.rfc_message_id:
        raise ActionUnsupportedError()
    within = ("UID", uid) if uid else ()
    data = _ok(connection.uid("SEARCH", None, *within, "HEADER", "Message-ID", _quoted(message.rfc_message_id)))
    uids = data[0].split() if data and data[0] else []
    if len(uids) != 1 or not uids[0].isdigit() or (uid and uids[0].decode() != uid):
        raise RuntimeError("IMAP message not uniquely found")
    return uids[0].decode()


def imap_locatable(message: MailMessage) -> bool:
    # Sync observed an INBOX disappearance. Neither marker is a physical Archive UID.
    return message.folder not in {"IMAP_DELETED", IMAP_RETIRED} and not (
        message.folder == "ARCHIVE" and message.provider_uid.startswith(("inbox:", "retired:")))


def _message_uid(connection, message: MailMessage, *, expected: str | None = None) -> str:
    if not imap_locatable(message):
        raise ActionUnsupportedError()
    if message.folder == "INBOX":
        generation = expected or getattr(message, "imap_uidvalidity", None)
        if generation and _uidvalidity(connection) != generation:
            raise RuntimeError("Indexed IMAP UIDVALIDITY changed")
        return message.provider_uid
    if message.folder == "TRASH" and ":" in message.provider_uid:
        validity, uid = message.provider_uid.split(":", 1)
        if not uid.isdigit() or _uidvalidity(connection) != validity:
            raise RuntimeError("Indexed IMAP trash UIDVALIDITY changed")
        return _search(connection, message, uid)
    return _search(connection, message)


def _uidvalidity(connection) -> str:
    _, values = connection.response("UIDVALIDITY")
    if not values or not isinstance(values[0], bytes) or not values[0].isdigit():
        raise RuntimeError("Missing IMAP UIDVALIDITY")
    return values[0].decode()


class ImapBackend:
    def __init__(self, account: MailAccount):
        self.account = account

    async def apply(self, message: MailMessage, kind: ActionKind) -> dict:
        folder = {"archive": "ARCHIVE", "trash": "TRASH", "spam": "SPAM", "restore": "INBOX"}.get(kind, message.folder)
        is_read = kind == "mark_read" if kind in ("mark_read", "mark_unread") else None
        return await self._run(message, folder, is_read)

    async def revert(self, message: MailMessage, previous: dict) -> dict:
        return await self._run(message, previous["folder"], previous["is_read"])

    async def _run(self, message: MailMessage, folder: str, is_read: bool | None) -> dict:
        if not imap_locatable(message):
            raise ActionUnsupportedError()
        expected = message.imap_uidvalidity
        if message.folder == "INBOX" and folder == "TRASH" and expected is None:
            expected = await message_access.legacy_imap_uidvalidity(message)
        credentials = await service.load_credentials(self.account)
        host = credentials["imap_host"]
        await transport.check_mail_host(host)
        password = await service.imap_password(credentials)
        return await asyncio.to_thread(self._change, host, int(credentials.get("imap_port", 993)), password, message, folder, is_read, expected)

    def _change(self, host: str, port: int, password: str, message: MailMessage, folder: str, is_read: bool | None, expected: str | None) -> dict:
        connection = transport.open_imap(host, port)
        try:
            _ok(connection.login(self.account.email_address, password))
            listing = _ok(connection.list('""', '"*"')) if message.folder != "INBOX" or folder != "INBOX" else []
            source = _folder(message.folder, listing)
            target = _folder(folder, listing)
            capabilities = {value.decode().upper() if isinstance(value, bytes) else value.upper() for value in connection.capabilities}
            moving = source != target
            if moving and "MOVE" not in capabilities and "UIDPLUS" not in capabilities:
                # Never expunge unrelated deleted messages on servers without UID EXPUNGE.
                raise ActionUnsupportedError()
            if moving and not message.rfc_message_id:
                # Undo must be able to locate the message after its folder UID changes.
                raise ActionUnsupportedError()
            _ok(connection.select(_quoted(source)))
            uid = _message_uid(connection, message, expected=expected)
            if not uid.isdigit():
                raise ActionUnsupportedError()
            updates = {}
            if moving and folder == "TRASH" and message.folder == "INBOX":
                # An IMAP UID command can return OK even if it matched no message.
                if not expected:
                    raise RuntimeError("Missing indexed IMAP UIDVALIDITY")
                _search(connection, message, uid)
                updates["imap_source_uidvalidity"] = expected
                updates["imap_uidvalidity"] = None
            if is_read is not None:
                _ok(connection.uid("STORE", uid, "+FLAGS" if is_read else "-FLAGS", "(\\Seen)"))
            stored_uid = message.provider_uid
            if moving:
                _ok(connection.uid("MOVE" if "MOVE" in capabilities else "COPY", uid, _quoted(target)))
                _, copy_data = connection.response("COPYUID")
                copied_uid = None
                if copy_data and isinstance(copy_data[0], bytes):
                    match = re.fullmatch(rb"(\d+) (\d+) (\d+)", copy_data[0])
                    if match and match[2].decode() == uid:
                        validity, copied_uid = match[1].decode(), match[3].decode()
                if folder == "TRASH":
                    if copied_uid is None:
                        _ok(connection.select(_quoted(target)))
                        copied_uid = _search(connection, message)
                        validity = _uidvalidity(connection)
                        if "MOVE" not in capabilities:
                            _ok(connection.select(_quoted(source)))
                            if message.folder == "INBOX" and _uidvalidity(connection) != expected:
                                raise RuntimeError("Indexed IMAP UIDVALIDITY changed")
                            _search(connection, message, uid)
                    stored_uid = f"{validity}:{copied_uid}"
                if "MOVE" not in capabilities:
                    _ok(connection.uid("STORE", uid, "+FLAGS", "(\\Deleted)"))
                    _ok(connection.uid("EXPUNGE", uid))
                if folder == "INBOX":
                    if copied_uid is not None:
                        stored_uid = copied_uid
                    else:
                        _ok(connection.select(_quoted(target)))
                        stored_uid = _search(connection, message)
                        validity = _uidvalidity(connection)
                    updates["imap_uidvalidity"] = validity
            return {"provider_uid": stored_uid, **updates}
        finally:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
