"""Send via SMTP, Gmail, Graph or connectors after explicit confirmation.

Proposal confirmation and newsletter unsubscribe call here; the agent never sends itself.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timezone
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from db.models.mail_account import MailAccount
from db.models.mail_message import MailMessage

from api.mail import connectors, providers, service, transport
from api.mail.headers import plain_address
from api.mail.seed_ids import is_seed_account


class SeedAccountError(RuntimeError):
    """Sample accounts cannot send mail."""


def reply_recipients(original: MailMessage, account: MailAccount, *, reply_all: bool) -> tuple[list[str], list[str]]:
    """Return (to, cc) for a reply; the confirmation card shows exactly these addresses.

    ``to`` is the Reply-To address list without the account's own address when that leaves a valid
    address, else the sender. A reply to all moves the sender (when not in Reply-To) and the
    original To/Cc into ``cc``, without the account's own address and without anyone already in
    ``to``.
    """
    own = account.email_address.casefold()
    to: list[str] = []
    for address in json.loads(original.reply_to_json or "[]"):
        valid = plain_address(address) if isinstance(address, str) else ""
        if valid and valid != own and valid not in to:
            to.append(valid)
    if not to and original.sender_address:
        to.append(original.sender_address)
    cc: list[str] = []
    seen = {own, *(address.casefold() for address in to)}
    if reply_all:
        for address in [original.sender_address, *json.loads(original.recipients_json or "[]"), *json.loads(original.cc_json or "[]")]:
            if address and address.casefold() not in seen:
                cc.append(address)
                seen.add(address.casefold())
    return to, cc


def quote_original(message: MailMessage, *, tz: str = "Europe/Berlin") -> str:
    if message.internal_date is None:
        first = f"{message.sender} schrieb:"
    else:
        date = message.internal_date
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        d = date.astimezone(ZoneInfo(tz))
        first = f"Am {d.day}.{d.month}. um {d:%H:%M} schrieb {message.sender}:"
    return "\n".join([first, *("> " + line if line else ">" for line in message.body_text.splitlines())])


def _message(account: MailAccount, *, to: list[str], cc: Sequence[str], subject: str, body: str, reply_to: MailMessage | None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = account.email_address
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    if reply_to and reply_to.rfc_message_id:
        reference = reply_to.rfc_message_id.strip()
        if not reference.startswith("<"):
            reference = f"<{reference}>"
        message["In-Reply-To"] = reference
        message["References"] = f"{reply_to.references} {reference}".strip()
    message.set_content(body)
    return message


def _send_smtp(account: MailAccount, credentials: dict[str, Any], *, to: list[str], cc: Sequence[str], subject: str, body: str, reply_to: MailMessage | None) -> dict[str, Any]:
    """Blocking SMTP send; runs in a worker thread."""
    message = _message(account, to=to, cc=cc, subject=subject, body=body, reply_to=reply_to)
    server = transport.open_smtp(credentials["smtp_host"], int(credentials.get("smtp_port", 465)), starttls=bool(credentials.get("smtp_starttls")))
    try:
        server.login(account.email_address, credentials["password"])
        server.send_message(message, to_addrs=[*to, *cc])
    finally:
        server.quit()
    return {"status": "sent", "via": "smtp"}


async def _send_gmail(account: MailAccount, token: str, *, to: list[str], cc: Sequence[str], subject: str, body: str, reply_to: MailMessage | None) -> dict[str, Any]:
    message = _message(account, to=to, cc=cc, subject=subject, body=body, reply_to=reply_to)
    payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")}
    if reply_to and reply_to.thread_id:
        payload["threadId"] = reply_to.thread_id
    async with httpx.AsyncClient(timeout=30, base_url=providers.GMAIL_API) as client:
        response = await client.post("/messages/send", json=payload, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return {"status": "sent", "via": "gmail", "id": response.json().get("id")}


async def _send_graph(account: MailAccount, token: str, *, to: list[str], cc: Sequence[str], subject: str, body: str, reply_to: MailMessage | None, forward_of: MailMessage | None, reply_all: bool) -> dict[str, Any]:
    payload = {
        "subject": subject, "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": address}} for address in to],
        "ccRecipients": [{"emailAddress": {"address": address}} for address in cc],
        # Graph reply drafts copy the original's recipients; only the confirmed ones may remain.
        "bccRecipients": [],
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30, base_url=providers.GRAPH_API) as client:
        original = reply_to or forward_of
        if original:
            operation = ("createReplyAll" if reply_all else "createReply") if reply_to else "createForward"
            response = await client.post(f"/me/messages/{quote(original.provider_uid, safe='')}/{operation}", json={}, headers=headers)
            response.raise_for_status()
            draft_id = quote(response.json()["id"], safe="")
            response = await client.patch(f"/me/messages/{draft_id}", json=payload, headers=headers)
            response.raise_for_status()
            response = await client.post(f"/me/messages/{draft_id}/send", headers=headers)
        else:
            response = await client.post("/me/sendMail", json={"message": payload}, headers=headers)
        response.raise_for_status()
        return {"status": "sent", "via": "graph"}


async def send_message(
    account: MailAccount, *, to: list[str], cc: Sequence[str] = (), subject: str,
    body: str, reply_to: MailMessage | None = None, forward_of: MailMessage | None = None,
    reply_all: bool = False, credentials: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one confirmed message; provider failures are mapped by the caller."""
    if is_seed_account(account.id):
        raise SeedAccountError()
    if reply_to and not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    elif forward_of and not subject.lower().startswith(("wg:", "fw:", "fwd:")):
        subject = "Wg: " + subject
    if account.auth_type == connectors.CONNECTOR:
        payload = {"to": to, "subject": subject, "body": body}
        if cc:
            payload["cc"] = list(cc)
        if reply_to:
            payload["replyToMessageId"] = reply_to.provider_uid
        await connectors.call_write_tool(account, "send_email", payload)
        return {"status": "sent", "via": f"connector:{account.provider}"}
    credentials = credentials if credentials is not None else await service.load_credentials(account)
    if account.provider == providers.IMAP:
        await transport.check_mail_host(credentials["smtp_host"])
        password = await service.imap_password(credentials)
        return await asyncio.to_thread(_send_smtp, account, {**credentials, "password": password}, to=to, cc=cc, subject=subject, body=body, reply_to=reply_to)
    if account.provider == providers.GOOGLE:
        token = await service.valid_access_token(account, credentials)
        return await _send_gmail(account, token, to=to, cc=cc, subject=subject, body=body, reply_to=reply_to)
    if account.provider == providers.MICROSOFT:
        token = await service.valid_access_token(account, credentials)
        return await _send_graph(account, token, to=to, cc=cc, subject=subject, body=body, reply_to=reply_to, forward_of=forward_of, reply_all=reply_all)
    raise ValueError("unknown provider")
