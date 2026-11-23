"""Parse RFC 822 messages fetched over IMAP."""

import asyncio
import email
import imaplib
import json
import re
from datetime import UTC
from email import policy
from email.utils import parsedate_to_datetime
from typing import Any

from db.accessors import mail_message as message_access
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import service, transport
from api.mail.headers import sender_address
from api.mail.sync_types import HTML_VERSION, Reread, SyncChanges, addresses, body_text, html_body, common


def _open_inbox(connection, email_address: str, password: str) -> None:
    try:
        connection.login(email_address, password)
    except imaplib.IMAP4.abort:
        raise
    except imaplib.IMAP4.error as error:
        raise service.CredentialError("IMAP login rejected") from error
    status, _ = connection.select('"INBOX"', readonly=True)
    if status != "OK":
        raise RuntimeError("IMAP select failed")


def _logout(connection) -> None:
    try:
        connection.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


def _inbox_flags(connection, uid_set: str) -> dict[str, list[bytes]]:
    status, data = connection.uid("fetch", uid_set, "(FLAGS)")
    if status != "OK":
        raise RuntimeError("IMAP flags fetch failed")
    result = {}
    for entry in data or []:
        raw = entry[0] if isinstance(entry, tuple) else entry
        if not isinstance(raw, bytes):
            continue
        uid = re.search(rb"\bUID\s+(\d+)", raw, re.I)
        if uid:
            match = re.search(rb"\bFLAGS\s*\(([^)]*)\)", raw, re.I)
            if not match:
                raise RuntimeError("IMAP flags missing")
            flags = match[1].lower().split()
            result[uid[1].decode()] = flags
    return result


def _fetch(host: str, port: int, email_address: str, password: str, cursor: str, known: dict[str, str], hidden: dict[str, str], limit: int, max_body_chars: int) -> SyncChanges:
    connection = transport.open_imap(host, port)
    try:
        _open_inbox(connection, email_address, password)
        _, values = connection.response("UIDVALIDITY")
        validity = values[0].decode() if values and values[0] else ""
        if not validity:
            raise RuntimeError("IMAP UIDVALIDITY missing")
        previous_validity, separator, position = cursor.partition(":")
        previous_uid, _, pending_json = position.partition(":")
        rebuild = not separator or previous_validity != validity or not previous_uid.isdigit()
        # Read suffixes from earlier draft workers once. New cursors stay readable by the base API.
        pending = {}
        if pending_json and not rebuild:
            try:
                pending = json.loads(pending_json)
                if not isinstance(pending, dict) or not all(uid.isascii() and uid.isdigit() and isinstance(local_id, str)
                                                           for uid, local_id in pending.items()):
                    raise ValueError("Invalid deferred IMAP UIDs")
            except ValueError:
                rebuild, pending = True, {}
        prefix = f"{validity}:"
        current_hidden = {key[len(prefix):]: local_id for key, local_id in hidden.items() if key.startswith(prefix)}
        pending.update(current_hidden)
        if rebuild:
            known = {}
        last = 0 if rebuild else int(previous_uid)
        status, data = connection.uid("search", None, "ALL" if rebuild else "UID", *(() if rebuild else (f"{last + 1}:*",)))
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        uids = sorted(int(uid) for uid in (data[0].split() if data and data[0] else []) if int(uid) > last)
        selected = uids[-limit:] if rebuild else uids[:limit]
        changes = SyncChanges(full_rebuild=rebuild, cursor=f"{validity}:{selected[-1] if selected else last}")
        changes.imap_retired = {key: local_id for key, local_id in hidden.items() if not key.startswith(prefix)}
        numeric = [int(uid) for uid in known.keys() | pending.keys() if uid.isdigit()]
        recheck = []
        if numeric:
            flags_by_uid = _inbox_flags(connection, f"{min(numeric)}:{max(numeric)}")
            present = {uid for uid, flags in flags_by_uid.items() if b"\\deleted" not in flags}
            for uid in known.keys() & present:
                changes.read_updates[uid] = b"\\seen" in flags_by_uid[uid]
                changes.flag_updates[uid] = b"\\flagged" in flags_by_uid[uid]
            changes.moved_out = [uid for uid in known if uid.isdigit() and uid not in present]
            for uid in current_hidden.keys() - flags_by_uid.keys():
                changes.imap_retired[f"{validity}:{uid}"] = current_hidden[uid]
            for uid in known.keys() & (flags_by_uid.keys() - present):
                pending[uid] = known[uid]
            changes.imap_hidden = {uid: pending[uid] for uid in pending.keys() & (flags_by_uid.keys() - present)
                                   if pending[uid] and uid not in current_hidden}
            recheck = sorted(int(uid) for uid in pending.keys() & flags_by_uid.keys()
                             if uid in present or not pending[uid])[:limit]
        for uid in dict.fromkeys([*selected, *recheck]):
            status, data = connection.uid("fetch", str(uid), "(UID FLAGS RFC822.SIZE BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("IMAP message fetch failed")
            part = next((entry for entry in data or [] if isinstance(entry, tuple) and entry[1]), None)
            if part is None:
                raise RuntimeError("IMAP message missing from fetch")
            flags_match = re.search(rb"\bFLAGS\s*\(([^)]*)\)", part[0], re.I)
            flags = flags_match.group(1).lower().split() if flags_match else []
            parsed = parse_rfc822(part[1], max_body_chars=max_body_chars)
            parsed.update(folder="INBOX", provider_uid=str(uid), imap_uidvalidity=validity,
                          is_read=b"\\seen" in flags, is_flagged=b"\\flagged" in flags)
            if b"\\deleted" in flags:
                changes.moved_out.append(str(uid))
                local_id = pending.get(str(uid)) or known.get(str(uid))
                if local_id:
                    changes.imap_hidden[str(uid)] = local_id
                else:
                    parsed.update(folder="IMAP_DELETED", provider_uid=f"{validity}:{uid}")
                    changes.imap_new_hidden.append(parsed)
                continue
            # The full message read is later than the range FLAGS response.
            changes.imap_hidden.pop(str(uid), None)
            changes.imap_retired.pop(f"{validity}:{uid}", None)
            if str(uid) in changes.read_updates:
                changes.read_updates[str(uid)] = parsed["is_read"]
                changes.flag_updates[str(uid)] = parsed["is_flagged"]
            changes.upserts.append(parsed)
            if local_id := pending.pop(str(uid), None):
                changes.imap_undeleted[str(uid)] = local_id
        returned = {row["provider_uid"] for row in changes.upserts}
        changes.moved_out = [uid for uid in changes.moved_out if uid not in returned]
        return changes
    finally:
        _logout(connection)


def _fetch_bodies(host: str, port: int, email_address: str, password: str, uids: list[str], max_body_chars: int) -> Reread:
    connection = transport.open_imap(host, port)
    try:
        _open_inbox(connection, email_address, password)
        found: Reread = {}
        for uid in uids:
            if not uid.isdigit():
                found[uid] = None  # not an IMAP UID: never readable
                continue
            try:
                status, data = connection.uid("fetch", uid, "(UID BODY.PEEK[])")
            except (imaplib.IMAP4.error, OSError):
                break  # connection lost: the rest waits for the next run
            if status != "OK":
                continue  # this mail failed now; asked again next run
            part = next((entry for entry in data or [] if isinstance(entry, tuple) and entry[1]), None)
            # No data: the server no longer has this UID.
            found[uid] = parse_rfc822(part[1], max_body_chars=max_body_chars) if part is not None else None
        return found
    finally:
        _logout(connection)


async def fetch_changes(account: MailAccount, *, cursor: str, folders: list[str], limit: int, max_body_chars: int) -> SyncChanges:
    credentials = await service.load_credentials(account)
    host = credentials["imap_host"]
    await transport.check_mail_host(host)
    password = await service.imap_password(credentials)
    known = await message_access.known_uids(account.id, "INBOX") if cursor else {}
    hidden = await message_access.known_uids(account.id, "IMAP_DELETED")
    return await asyncio.to_thread(_fetch, host, int(credentials.get("imap_port", 993)), account.email_address, password,
                                   cursor, known, hidden, limit, max_body_chars)


async def confirm_inbox(account: MailAccount, uids, cursor: str) -> set[str]:
    """Resolve replays without treating a UID reused in another UIDVALIDITY as the old mail."""
    credentials = await service.load_credentials(account)
    host = credentials["imap_host"]
    await transport.check_mail_host(host)
    password = await service.imap_password(credentials)

    def read():
        connection = transport.open_imap(host, int(credentials.get("imap_port", 993)))
        try:
            _open_inbox(connection, account.email_address, password)
            _, values = connection.response("UIDVALIDITY")
            if not values or values[0] != cursor.partition(":")[0].encode():
                raise RuntimeError("IMAP UIDVALIDITY changed during poll")
            flags = _inbox_flags(connection, ",".join(uids))
            return {uid for uid in uids if uid in flags and b"\\deleted" not in flags[uid]}
        finally:
            _logout(connection)
    return await asyncio.to_thread(read)


async def fetch_bodies(account: MailAccount, messages: list[MailMessage], *, max_body_chars: int) -> Reread:
    """Re-read INBOX messages by UID without marking them read (BODY.PEEK); see `Reread` for the result."""
    uids = [message.provider_uid for message in messages]
    credentials = await service.load_credentials(account)
    host = credentials["imap_host"]
    await transport.check_mail_host(host)
    password = await service.imap_password(credentials)
    return await asyncio.to_thread(_fetch_bodies, host, int(credentials.get("imap_port", 993)), account.email_address, password, uids, max_body_chars)


def _part_text(part) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else ""
    except LookupError:
        # An unknown charset must not cost the text; undecodable bytes become replacement characters.
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def parse_rfc822(raw: bytes, *, max_body_chars: int) -> dict[str, Any]:
    msg = email.message_from_bytes(raw, policy=policy.default)
    texts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    files = []
    for part in msg.walk():
        try:
            filename = part.get_filename()
            if filename or part.get_content_disposition() == "attachment":
                payload = part.get_payload(decode=True) or b""
                files.append({"name": filename or "", "size": len(payload), "content_type": part.get_content_type()})
            elif part.get_content_type() in texts:
                texts[part.get_content_type()].append(_part_text(part))
        except Exception:
            pass
    body = body_text(texts["text/plain"], texts["text/html"])[:max_body_chars]
    html, html_status = html_body(texts["text/html"])
    def header(key: str) -> str:
        try:
            return str(msg.get(key, ""))
        except Exception:
            return ""

    def mailbox_addresses(key: str) -> list[str] | None:
        try:
            values = msg.get_all(key, [])
            if not all(hasattr(value, "addresses") for value in values):
                return None
            return [address.addr_spec for value in values for address in value.addresses if address.username and address.domain]
        except Exception:
            return None

    try:
        date = parsedate_to_datetime(header("date"))
        if date and date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        date = None
    hdr = {key.lower(): header(key) for key, _ in msg.raw_items()}
    raw_from = next((value for key, value in msg.raw_items() if key.lower() == "from"), "")
    sender = header("from")
    sender_fields = common(raw_from, hdr)
    sender_fields["sender_address"] = sender_address(raw_from)
    to_addresses = mailbox_addresses("to")
    cc_addresses = mailbox_addresses("cc")
    reply_to_addresses = mailbox_addresses("reply-to")
    return {**sender_fields, "thread_id": hdr.get("message-id", ""),
            "subject": header("subject"), "sender": sender,
            "recipients_json": json.dumps(to_addresses) if to_addresses is not None else addresses(header("to")),
            "cc_json": json.dumps(cc_addresses) if cc_addresses is not None else addresses(header("cc")),
            "reply_to_json": json.dumps(reply_to_addresses) if reply_to_addresses is not None else addresses(header("reply-to")),
            'snippet': ' '.join(body.split())[:500], 'body_text': body, 'body_html': html, 'html_status': html_status, 'html_version': HTML_VERSION,
            'internal_date': date, 'has_attachments': bool(files),
            'attachments_json': json.dumps(files), 'is_read': False,
            'is_flagged': False, 'labels_json': '[]'}
