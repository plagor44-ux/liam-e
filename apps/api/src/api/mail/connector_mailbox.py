"""Keep connector reads and writes on the same provider mailbox as its local index."""
import base64
import json
from contextlib import asynccontextmanager
from email.utils import parseaddr
from urllib.parse import quote

import httpx
from db.accessors import chat_settings, mail_sync_state
from db.models.mail_account import inbox_only
from db.models.mail_sync_state import CONNECTOR_CURSOR_PREFIX
from api.mail import connectors


class MailboxUnproven(ValueError):
    """An initial connection has no usable identity yet. Its ordinary inbox can still sync."""


class MailboxUnavailable(ValueError):
    """No positive mailbox proof. Return a reconnect status without provider content."""


def encode(binding: tuple[str, str, str], cursor: str) -> str:
    token = base64.urlsafe_b64encode(json.dumps(binding).encode()).decode()
    return f"{CONNECTOR_CURSOR_PREFIX}{token}|{cursor}"


def decode(cursor: str) -> tuple[tuple[str, str, str], str]:
    try:
        token, progress = cursor.removeprefix(CONNECTOR_CURSOR_PREFIX).split('|', 1)
        parts = json.loads(base64.b64decode(token, altchars=b'-_', validate=True))
        if not isinstance(parts, list) or len(parts) != 3 or not all(isinstance(p, str) and p for p in parts):
            raise ValueError()
        return (parts[0], parts[1], parts[2]), progress
    except (ValueError, UnicodeError):
        raise MailboxUnavailable() from None


async def _credential(provider: str) -> str:
    for scope in ('workspace', 'user', 'organization'):
        path = f"/v1/connectors/{quote(provider, safe='')}/{scope}/credentials"
        try:
            result = await connectors._get(path, {"auth_type": "oauth2"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (403, 404):
                continue
            raise
        choices = [row for row in (result.get('credentials') or []) if row.get('name') and row.get('authentication_type') == 'oauth2']
        if choices:
            selected = [row for row in choices if row.get('is_default')] or choices
            if len(selected) == 1:
                return selected[0]['name']
            break
    raise MailboxUnproven()


def _sent_by(message: dict, address: str) -> bool:
    return ('SENT' in (message.get('labelIds') or []) and
            parseaddr(message.get('sender') or '')[1].casefold() == address.casefold())


async def _anchor(account) -> tuple[str, str]:
    data = await connectors.call_read_tool_json(account.provider, 'search_threads', {'query': 'in:sent from:me', 'pageSize': 1})
    if not isinstance(data.get('threads'), list):
        raise ValueError('Incomplete connector search response')
    if not data['threads']:
        raise MailboxUnproven()
    for thread in data['threads']:
        for message in thread.get('messages', []):
            if thread.get('id') and message.get('id') and _sent_by(message, account.email_address):
                return thread['id'], message['id']
    raise MailboxUnavailable()


async def _verify(account, thread_id: str, message_id: str) -> None:
    thread = await connectors.call_read_tool_json(account.provider, 'get_thread', {'threadId': thread_id, 'messageFormat': 'MINIMAL'})
    if not isinstance(thread, dict) or not thread.get('id') or not isinstance(thread.get('messages'), list):
        raise ValueError('Incomplete connector identity response')
    if thread['id'] != thread_id or not any(
        message.get('id') == message_id and _sent_by(message, account.email_address) for message in thread.get('messages', [])
    ):
        raise MailboxUnavailable()


@asynccontextmanager
async def _check_identity(account_id: str, generation: int | None, observed):
    try:
        yield
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 404:
            raise
        await chat_settings.invalidate_connector(account_id, generation, observed=observed)
        raise MailboxUnavailable() from None
    except MailboxUnavailable:
        await chat_settings.invalidate_connector(account_id, generation, observed=observed)
        raise


async def verify_bound_mailbox(account, binding: tuple[str, str, str], *, generation: int | None, observed) -> None:
    """Verify the current bound credential and invalidate an identity mismatch."""
    async with _check_identity(account.id, generation, observed):
        await _verify(account, binding[1], binding[2])


@asynccontextmanager
async def bound_mailbox(account, *, cursor: str | None = None, bootstrap: bool = False, check_after: bool = False, generation: int | None = None, observed=None):
    """Check the provider identity on the pinned credential. Transient failures stop this call."""
    if inbox_only(account):
        raise MailboxUnavailable()
    if cursor is None:
        state = await mail_sync_state.get_state(account.id)
        if not state or not state.connector_verified:
            raise MailboxUnavailable()
        cursor = state.connector_cursor
        generation = state.connector_generation
        observed = state
    if cursor.startswith(CONNECTOR_CURSOR_PREFIX):
        async with _check_identity(account.id, generation, observed):
            (credential, thread_id, message_id), progress = decode(cursor)
    elif bootstrap:
        credential = await _credential(account.provider)
        thread_id = message_id = progress = ''
    else:
        raise MailboxUnavailable()
    token = connectors.mailbox_credential.set(credential)
    try:
        if not thread_id:
            thread_id, message_id = await _anchor(account)
        binding = (credential, thread_id, message_id)
        await verify_bound_mailbox(account, binding, generation=generation, observed=observed)
        yield binding, progress
        if check_after:
            await verify_bound_mailbox(account, binding, generation=generation, observed=observed)
    finally:
        connectors.mailbox_credential.reset(token)


@asynccontextmanager
async def bound_inbox_message(account, message):
    """A direct label action on an incoming-only mailbox needs proof of this provider message."""
    if message.account_id != account.id or not message.provider_uid or not message.thread_id:
        raise MailboxUnavailable()
    try:
        credential = await _credential(account.provider)
    except MailboxUnproven:
        raise MailboxUnavailable() from None
    token = connectors.mailbox_credential.set(credential)
    try:
        thread = await connectors.call_read_tool_json(account.provider, 'get_thread',
            {'threadId': message.thread_id, 'messageFormat': 'MINIMAL'})
        if thread.get('id') != message.thread_id or not any(
                row.get('id') == message.provider_uid and row.get('sender', '') == message.sender
                and row.get('subject', '') == message.subject for row in thread.get('messages', [])):
            raise MailboxUnavailable()
        yield
    finally:
        connectors.mailbox_credential.reset(token)
