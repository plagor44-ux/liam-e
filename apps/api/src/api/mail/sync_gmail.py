"""Parse Gmail API full-format messages."""

import base64
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import providers, service
from api.mail.sync_types import HTML_VERSION, Reread, SyncChanges, addresses, body_text, html_body, common, inbox_labels, reread_http


async def fetch_changes(account: MailAccount, *, cursor: str, folders: list[str], limit: int, max_body_chars: int) -> SyncChanges:
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    headers = {"Authorization": f"Bearer {token}"}
    changes = SyncChanges(cursor=cursor)
    async with httpx.AsyncClient(timeout=30, base_url=providers.GMAIL_API) as client:
        ids: set[str] = set()
        rebuild = not cursor
        if cursor:
            page_token = ""
            while True:
                params: dict[str, Any] = {"startHistoryId": cursor, "maxResults": max(1, min(limit, 500)), "historyTypes": ["messageAdded", "labelAdded", "labelRemoved", "messageDeleted"]}
                if page_token:
                    params["pageToken"] = page_token
                response = await client.get("/history", params=params, headers=headers)
                if response.status_code == 404:
                    rebuild = True
                    break
                response.raise_for_status()
                page = response.json()
                changes.cursor = page.get("historyId", changes.cursor)
                for event in page.get("history", []):
                    for kind in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
                        for entry in event.get(kind, []):
                            if entry.get("message", {}).get("id"):
                                ids.add(entry["message"]["id"])
                page_token = page.get("nextPageToken", "")
                if not page_token:
                    break
                history = page.get("history", [])
                if len(ids) >= limit and history and history[-1].get("id"):
                    changes.cursor = str(history[-1]["id"])
                    break
        if rebuild:
            ids.clear()
            changes = SyncChanges(full_rebuild=True)
            profile = await client.get("/profile", headers=headers)
            profile.raise_for_status()
            changes.cursor = profile.json().get("historyId", "")
            page_token = ""
            while len(ids) < limit:
                params = {"q": "in:inbox", "maxResults": min(500, limit - len(ids))}
                if page_token:
                    params["pageToken"] = page_token
                response = await client.get("/messages", params=params, headers=headers)
                response.raise_for_status()
                page = response.json()
                ids.update(ref["id"] for ref in page.get("messages", []))
                page_token = page.get("nextPageToken", "")
                if not page_token:
                    break
        for uid in ids:
            response = await client.get(f"/messages/{uid}", params={"format": "full"}, headers=headers)
            if response.status_code == 404:
                changes.moved_out.append(uid)
                continue
            response.raise_for_status()
            message = response.json()
            await _hydrate_html_parts(client, message, headers)
            labels = message.get("labelIds") or []
            if not inbox_labels(labels):
                changes.moved_from_inbox(uid, labels)
                continue
            changes.upserts.append(parse_message(message, max_body_chars=max_body_chars))
    return changes


async def current_labels(account: MailAccount, candidates: set[str]) -> dict[str, list[str]]:
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    labels = {}
    async with httpx.AsyncClient(timeout=30, base_url=providers.GMAIL_API, headers={"Authorization": f"Bearer {token}"}) as client:
        for uid in candidates:
            response = await client.get(f"/messages/{quote(uid, safe='')}", params={"format": "minimal"})
            if response.status_code == 404:
                continue
            response.raise_for_status()
            item = response.json()
            if item.get("id") != uid or not isinstance(item.get("labelIds"), list):
                raise ValueError("Missing Gmail message labels")
            labels[uid] = item["labelIds"]
    return labels


async def fetch_bodies(account: MailAccount, messages: list[MailMessage], *, max_body_chars: int) -> Reread:
    """Re-read messages by id (format=full does not mark them read); see `Reread` for the result."""
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    async with httpx.AsyncClient(timeout=30, base_url=providers.GMAIL_API) as client:
        return await reread_http(
            [message.provider_uid for message in messages],
            lambda uid: client.get(f"/messages/{quote(uid, safe='')}", params={"format": "full"}, headers={"Authorization": f"Bearer {token}"}),
            lambda data: parse_message(data, max_body_chars=max_body_chars),
            prepare=lambda data: _hydrate_html_parts(client, data, {"Authorization": f"Bearer {token}"}),
        )


async def _hydrate_html_parts(client: httpx.AsyncClient, message: dict[str, Any], headers: dict[str, str]) -> None:
    """Fetch only HTML body parts referenced by Gmail, with bounded bytes and part count."""
    pending = [message.get('payload') or {}]
    checked = 0
    while pending:
        part = pending.pop()
        if not isinstance(part, dict):
            continue
        pending.extend(part.get('parts') or [])
        body = part.get('body') or {}
        attachment = body.get('attachmentId')
        if part.get('mimeType') != 'text/html' or part.get('filename') or not attachment or body.get('data'):
            continue
        checked += 1
        if checked > 20:
            return  # unresolved parts remain pending for a later attempt
        if isinstance(body.get('size'), int) and body['size'] > 800_000:
            body['_html_too_large'] = True
            continue
        try:
            uid, aid = quote(str(message['id']), safe=''), quote(str(attachment), safe='')
            async with client.stream('GET', f'/messages/{uid}/attachments/{aid}', headers=headers) as response:
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > 1_100_000:
                        body['_html_too_large'] = True
                        break
                    payload.extend(chunk)
                else:
                    data = json.loads(payload).get('data')
                    if isinstance(data, str): body['data'] = data
        except (httpx.HTTPError, ValueError, KeyError):
            continue  # do not silently mark a failed HTML fetch as absent


def parse_message(message: dict[str, Any], *, max_body_chars: int) -> dict[str, Any]:
    payload = message.get('payload') or {}
    headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
    files: list[dict[str, Any]] = []
    texts: dict[str, list[str]] = {'text/plain': [], 'text/html': []}
    missing_html = False
    oversized_html = False

    def walk(part: dict[str, Any]) -> None:
        nonlocal missing_html, oversized_html
        filename = part.get('filename')
        body = part.get('body') or {}
        if filename:
            files.append({'name': filename, 'size': body.get('size', 0), 'content_type': part.get('mimeType', '')})
        elif part.get('mimeType') == 'text/html' and body.get('attachmentId') and not body.get('data'):
            if body.get('_html_too_large'): oversized_html = True
            else: missing_html = True
        elif part.get('mimeType') in texts and body.get('data'):
            data = body['data']
            texts[part['mimeType']].append(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode(errors='replace'))
        for child in part.get('parts') or []:
            walk(child)

    walk(payload)
    body = body_text(texts['text/plain'], texts['text/html'])[:max_body_chars]
    html, html_status = html_body(texts['text/html'])
    labels = message.get('labelIds') or []
    sender = headers.get('from', '')
    return {**common(sender, headers), 'folder': 'INBOX', 'provider_uid': message['id'],
            'thread_id': message.get('threadId', ''), 'subject': headers.get('subject', ''),
            'sender': sender, 'recipients_json': addresses(headers.get('to', '')),
            'cc_json': addresses(headers.get('cc', '')), 'reply_to_json': addresses(headers.get('reply-to', '')),
            'snippet': ' '.join((message.get('snippet') or body).split())[:500],
            'body_text': body, 'body_html': None if oversized_html else html, 'html_status': 'pending' if missing_html else 'too_large' if oversized_html else html_status, 'html_version': None if missing_html else HTML_VERSION, 'internal_date': datetime.fromtimestamp(int(message['internalDate']) / 1000, tz=UTC) if message.get('internalDate') else None,
            'has_attachments': bool(files), 'attachments_json': json.dumps(files),
            'is_read': 'UNREAD' not in labels, 'is_flagged': 'STARRED' in labels, 'labels_json': json.dumps(labels)}
