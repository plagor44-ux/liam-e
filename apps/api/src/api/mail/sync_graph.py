"""Parse Microsoft Graph messages."""

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import service
from api.mail.providers import GRAPH_API
from api.mail.sync_types import HTML_VERSION, Reread, SyncChanges, addresses, attachments, body_text, html_body, common, reread_http


def _safe_delta_url(url: str) -> bool:
    parsed = httpx.URL(url)
    base = httpx.URL(GRAPH_API)
    return (parsed.scheme == "https" and parsed.host == base.host
            and (parsed.port or 443) == (base.port or 443)
            and "@" not in urlsplit(url).netloc)


async def fetch_changes(account: MailAccount, *, cursor: str, folders: list[str], limit: int, max_body_chars: int) -> SyncChanges:
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    headers = {"Authorization": f"Bearer {token}", "Prefer": f"odata.maxpagesize={max(1, limit)}"}
    changes = SyncChanges(cursor=cursor, full_rebuild=not cursor)
    url = cursor or "/me/mailFolders/inbox/messages/delta"
    params = None if cursor else {"$top": str(limit), "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,replyTo,receivedDateTime,body,hasAttachments,isRead,internetMessageId,internetMessageHeaders,flag"}
    latest: dict[str, dict[str, Any] | None] = {}
    async with httpx.AsyncClient(timeout=30, base_url=GRAPH_API) as client:
        while url:
            if url != "/me/mailFolders/inbox/messages/delta" and not _safe_delta_url(url):
                raise ValueError("Invalid Graph delta URL")
            response = await client.get(url, params=params, headers=headers, follow_redirects=False)
            response.raise_for_status()
            page = response.json()
            for item in page.get("value", []):
                if "@removed" in item:
                    latest[item["id"]] = None
                else:
                    if item.get("hasAttachments"):
                        item["attachments"] = await fetch_attachments(client, item["id"], headers)
                    latest[item["id"]] = parse_message(item, folder="INBOX", max_body_chars=max_body_chars)
            url = page.get("@odata.nextLink", "")
            params = None
            if url and len(latest) >= limit:
                if not _safe_delta_url(url):
                    raise ValueError("Invalid Graph next link")
                changes.cursor = url
                break
            if not url:
                changes.cursor = page.get("@odata.deltaLink", "")
                if not changes.cursor or not _safe_delta_url(changes.cursor):
                    raise ValueError("Missing or invalid Graph delta link")
    changes.upserts = [item for item in latest.values() if item is not None]
    changes.moved_out = [uid for uid, item in latest.items() if item is None]
    return changes


async def confirm_inbox(account: MailAccount, uids) -> set[str]:
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    confirmed = set()
    async with httpx.AsyncClient(timeout=30, base_url=GRAPH_API, headers={"Authorization": f"Bearer {token}"}) as client:
        for uid in uids:
            response = await client.get(f"/me/mailFolders/inbox/messages/{quote(uid, safe='')}", params={"$select": "id"})
            if response.status_code == 404:
                continue
            response.raise_for_status()
            if response.json().get("id") == uid:
                confirmed.add(uid)
    return confirmed


async def fetch_attachments(
    client: httpx.AsyncClient, message_id: str, headers: dict[str, str]
) -> list[dict[str, Any]] | None:
    url = f"/me/messages/{message_id}/attachments"
    params: dict[str, str] | None = {"$select": "name,size,contentType"}
    items: list[dict[str, Any]] = []
    try:
        for _ in range(5):
            response = await client.get(url, params=params, headers=headers, follow_redirects=False)
            response.raise_for_status()
            page = response.json()
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
            if not url:
                break
            next_url = httpx.URL(url)
            base_url = httpx.URL(GRAPH_API)
            next_port = next_url.port if next_url.port is not None else 443
            base_port = base_url.port if base_url.port is not None else 443
            if (next_url.scheme != "https" or next_url.host != base_url.host
                    or next_port != base_port or "@" in urlsplit(url).netloc):
                return None
            params = None
    except Exception:
        return None
    return items


async def fetch_bodies(account: MailAccount, messages: list[MailMessage], *, max_body_chars: int) -> Reread:
    """Re-read message bodies by id; see `Reread` for the result."""
    credentials = await service.load_credentials(account)
    token = await service.valid_access_token(account, credentials)
    async with httpx.AsyncClient(timeout=30, base_url=GRAPH_API) as client:
        return await reread_http(
            [message.provider_uid for message in messages],
            lambda uid: client.get(f"/me/messages/{quote(uid, safe='')}", params={"$select": "id,body"}, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False),
            lambda data: parse_message(data, folder="INBOX", max_body_chars=max_body_chars),
        )


def parse_message(message: dict[str, Any], *, folder: str, max_body_chars: int) -> dict[str, Any]:
    body = message.get('body') or {}
    content = body.get('content') or ''
    html = body.get('contentType', '').lower() == 'html'
    text = body_text([] if html else [content], [content] if html else [])[:max_body_chars]
    html_content, html_status = html_body([content] if html else [])
    sender = (message.get('from') or {}).get('emailAddress', {}).get('address', '')
    headers = {h['name'].lower(): h['value'] for h in message.get('internetMessageHeaders') or []}
    headers['message-id'] = message.get('internetMessageId') or headers.get('message-id', '')
    def recipients(key: str) -> str:
        return json.dumps([r.get('emailAddress', {}).get('address', '') for r in message.get(key) or [] if r.get('emailAddress', {}).get('address')])
    received = message.get('receivedDateTime')
    # A delta cursor from before replyTo joined $select keeps its old projection; its headers still carry Reply-To.
    reply_to = recipients('replyTo') if 'replyTo' in message else addresses(headers.get('reply-to', ''))
    return {**common(sender, headers), 'folder': folder, 'provider_uid': message['id'],
            'thread_id': message.get('conversationId', ''), 'subject': message.get('subject') or '',
            'sender': sender, 'recipients_json': recipients('toRecipients'), 'cc_json': recipients('ccRecipients'), 'reply_to_json': reply_to,
            'snippet': ' '.join(text.split())[:500], 'body_text': text, 'body_html': html_content, 'html_status': html_status if 'body' in message else 'pending', 'html_version': HTML_VERSION if 'body' in message else None,
            'internal_date': datetime.fromisoformat(received) if received else None,
            'has_attachments': bool(message.get('hasAttachments')), 'attachments_json': json.dumps(attachments(message.get('attachments') or [])),
            'is_read': bool(message.get('isRead')), 'is_flagged': (message.get('flag') or {}).get('flagStatus', '').lower() == 'flagged', 'labels_json': '[]'}
