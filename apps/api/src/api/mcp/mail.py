"""Read-only mail services for both the facade and a future native transport.

Only the existing inbox index is read. Message fields are untrusted external content, never
instructions. The services accept a verified grant, not caller-supplied authorization.
"""

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from db.accessors import mail_account, mail_message, mail_sync_state
from fastapi import HTTPException

from api.mcp.access import ReadGrant, ensure_agent_access, restrict_accounts
from api.mcp.schemas import (
    AccountCount, AccountItem, AccountsResult, IndexStatus, MessageItem, ReadResult,
    SearchResult, SummaryResult,
)

MAX_RESPONSE_BYTES = 128 * 1024
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _size(model) -> int:
    # ASCII escaping is a conservative upper bound on the UTF-8 JSON used by FastAPI.
    return len(json.dumps(model.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":")).encode())


def _scope(grant: ReadGrant, requested: list[str] | None) -> list[str]:
    return sorted(restrict_accounts(grant, requested))


async def _envelope(scope: list[str]) -> dict:
    states = {s.account_id: s for s in await mail_sync_state.list_for_accounts(scope)}
    index = []
    for account_id in scope:
        state = states.get(account_id)
        # A successful sync can still be a bounded provider page; there is no durable
        # completeness watermark in this index, so even an "ok" status is not proof.
        warnings = ["provider_freshness_unknown", "index_completeness_unknown"]
        if state is None or state.last_sync_at is None or state.status != "ok":
            warnings.append("index_incomplete")
        index.append(IndexStatus(account_id=account_id, last_sync_at=state.last_sync_at if state else None,
                                 incomplete=True, warnings=warnings))
    return {"scope": scope, "index": index}


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    return value[:limit], len(value) > limit


def _link(message_id: str) -> str:
    try:
        if str(UUID(message_id)) != message_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=500, detail="invalid_index_identifier") from None
    return f"/mail/{message_id}"


def _item(row) -> MessageItem:
    subject, subject_cut = _bounded(row.subject, 200)
    sender, sender_cut = _bounded(row.sender, 200)
    snippet, snippet_cut = _bounded(row.snippet, 500)
    return MessageItem(id=row.id, account_id=row.account_id, folder=row.folder[:64], subject=subject, sender=sender,
                       date=row.internal_date, snippet=snippet, is_read=row.is_read,
                       app_link=_link(row.id), truncated=subject_cut or sender_cut or snippet_cut or len(row.folder) > 64)


def _binding(query: str, scope: list[str]) -> str:
    raw = json.dumps([query, scope], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _encode_cursor(row, query: str, scope: list[str]) -> str:
    data = [(row.internal_date or EPOCH).isoformat(), row.id, _binding(query, scope)]
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, query: str, scope: list[str]) -> tuple[datetime, str]:
    try:
        if len(cursor) > 1024:
            raise ValueError
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        date_text, message_id, binding = data
        date = datetime.fromisoformat(date_text)
        if date.tzinfo is None or date.utcoffset() is None or binding != _binding(query, scope):
            raise ValueError
        if str(UUID(message_id)) != message_id:
            raise ValueError
        return date, message_id
    except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError):
        raise HTTPException(status_code=422, detail="cursor_invalid") from None


async def liam_mail_accounts(grant: ReadGrant) -> AccountsResult:
    await ensure_agent_access()
    scope = _scope(grant, None)
    accounts = []
    for row in await mail_account.list_for_ids(scope):
        name, cut = _bounded(row.display_name, 200)
        accounts.append(AccountItem(id=row.id, display_name=name, truncated=cut))
    result = AccountsResult(**await _envelope(scope), accounts=accounts, truncated=any(a.truncated for a in accounts))
    if _size(result) > MAX_RESPONSE_BYTES:
        # Preserve every authorized account and mark each shortened display name.
        for account in result.accounts:
            if len(account.display_name) > 40:
                account.display_name = account.display_name[:40]
                account.truncated = True
                result.truncated = True
    if _size(result) > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=500, detail="response_limit")
    return result


async def liam_mail_search(grant: ReadGrant, *, query: str = "", account_ids: list[str] | None = None,
                           cursor: str = "", limit: int = 50) -> SearchResult:
    await ensure_agent_access()
    if not 1 <= limit <= 50 or len(query) > 1000:
        raise HTTPException(status_code=422, detail="invalid_search")
    scope = _scope(grant, account_ids)
    before = _decode_cursor(cursor, query, scope) if cursor else None
    rows = await mail_message.list_page(folder="INBOX", account_ids=scope, query=query, before=before, limit=limit + 1)
    selected = rows[:limit]
    result = SearchResult(**await _envelope(scope), untrusted_messages=[_item(row) for row in selected])
    while True:
        more = len(rows) > len(selected)
        result.next_cursor = _encode_cursor(selected[-1], query, scope) if more and selected else None
        result.truncated = more or any(item.truncated for item in result.untrusted_messages)
        if _size(result) <= MAX_RESPONSE_BYTES:
            break
        if not selected:
            raise HTTPException(status_code=500, detail="response_limit")
        selected.pop()
        result.untrusted_messages.pop()
    return result


async def liam_mail_read(grant: ReadGrant, message_id: str) -> ReadResult:
    await ensure_agent_access()
    scope = _scope(grant, None)
    # Invalid, absent and foreign IDs all have the same response.
    try:
        if str(UUID(message_id)) != message_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=404, detail="message_not_found") from None
    # The read space is the inbox index, like search: a mail moved elsewhere answers like a
    # missing one instead of widening access to other folders by ID.
    row = await mail_message.get_scoped_message(message_id, scope, folder="INBOX")
    if row is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    body, cut = _bounded(row.body_text, 20_000)
    result = ReadResult(**await _envelope(scope), untrusted_message=_item(row), body_text=body,
                        body_available=bool(row.body_text), truncated=cut)
    result.truncated = result.truncated or result.untrusted_message.truncated
    while _size(result) > MAX_RESPONSE_BYTES and result.body_text:
        result.body_text = result.body_text[:len(result.body_text) // 2]
        result.truncated = True
    if _size(result) > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=500, detail="response_limit")
    return result


async def liam_mail_summary(grant: ReadGrant, *, account_ids: list[str] | None = None) -> SummaryResult:
    await ensure_agent_access()
    scope = _scope(grant, account_ids)
    values = await mail_message.inbox_counts(scope)
    counts = [AccountCount(account_id=id, total=values.get(id, (0, 0))[0], unread=values.get(id, (0, 0))[1]) for id in scope]
    result = SummaryResult(**await _envelope(scope), counts=counts,
                           total=sum(c.total for c in counts), unread=sum(c.unread for c in counts))
    if _size(result) > MAX_RESPONSE_BYTES:
        raise HTTPException(status_code=500, detail="response_limit")
    return result
