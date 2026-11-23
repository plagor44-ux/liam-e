"""The synced message index: page through messages and read one in full."""

import asyncio
import base64
import binascii
import json
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from db.accessors import mail_account as account_access
from db.accessors import mail_insight as insight_access
from db.accessors import mail_message as message_access
from db.accessors import mail_reference_snapshot
from db.models.mail_insight import MailInsight
from db.models.mail_message import MailMessage
from fastapi import APIRouter, HTTPException, Query, Response

from api.errors import refuse
from api.locales import Locale
from api.mail import actions, insights, original
from api.mail.headers import sender_name
from api.mail.state import color_indexes
from api.routers.api.v1.mail.schemas import BulkMessageActionIn, BulkMessageActionOut, BulkMessageActionResult, InsightOut, MessageActionIn, MessageActionOut, MessageDetailOut, MessageHtmlOut, MessageListItemOut, MessageOriginalOut, MessagePageOut

logger = logging.getLogger(__name__)
router = APIRouter()
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def encode_cursor(message: MailMessage) -> str:
    key = f"{(message.internal_date or EPOCH).isoformat()}|{message.id}"
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode()
        date_text, message_id = raw.split("|")
        dt = datetime.fromisoformat(date_text)
        if not message_id or dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("Invalid cursor key")
        return dt, message_id
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise refuse(422, "cursor_invalid", "Diese Seite konnte nicht geladen werden. Öffne den Posteingang noch einmal.") from error


def list_item(message: MailMessage, colors: dict[str, int], insight: MailInsight | None) -> MessageListItemOut:
    return MessageListItemOut(
        id=message.id, account_id=message.account_id,
        color_index=colors.get(message.account_id, 0), subject=message.subject,
        sender_name=sender_name(message.sender), sender_address=message.sender_address,
        snippet=message.snippet, internal_date=message.internal_date,
        is_read=message.is_read, is_flagged=message.is_flagged,
        has_attachments=message.has_attachments,
        deadline=insight.deadline if insight else None,
        needs_reply=insight.needs_reply if insight else False,
    )


@router.get("", operation_id="mail_messages_list", summary="List the synced inbox")
async def list_messages(
    cursor: str = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    account_id: Annotated[list[str], Query()] = [],
    unread: bool = False,
    important: bool = False,
    attachments: bool = False,
    q: str = "",
    folder: Literal["INBOX", "ARCHIVE", "TRASH"] = "INBOX",
) -> MessagePageOut:
    """List messages with filters and an opaque cursor for the next page."""
    before = decode_cursor(cursor) if cursor else None
    messages = await message_access.list_page(
        folder=folder, account_ids=account_id or None, unread_only=unread,
        important_only=important, with_attachments=attachments, query=q,
        before=before, limit=limit,
    )
    insights = await insight_access.get_for_messages([message.id for message in messages])
    colors = color_indexes(await account_access.list_accounts())
    return MessagePageOut(
        items=[list_item(message, colors, insights.get(message.id)) for message in messages],
        next_cursor=encode_cursor(messages[-1]) if len(messages) == limit else None,
    )


@router.post("/actions", operation_id="mail_messages_bulk_action", summary="Apply an undoable action to messages")
async def bulk_message_action(body: BulkMessageActionIn) -> BulkMessageActionOut:
    """Apply actions independently, with bounded provider concurrency."""
    semaphore = asyncio.Semaphore(4)
    accounts: asyncio.Future | None = None

    async def colors() -> dict:
        # One account read per request, shared by all rows; a failure only drops the display rows.
        nonlocal accounts
        if accounts is None:
            accounts = asyncio.ensure_future(account_access.list_accounts())
        return color_indexes(await accounts)

    async def apply(message_id: str) -> BulkMessageActionResult:
        async with semaphore:
            try:
                action = await actions.apply_action(message_id, body.kind)
            except actions.ActionUnsupportedError:
                return BulkMessageActionResult(message_id=message_id, status="unsupported")
            except HTTPException as error:
                if error.status_code == 404:
                    return BulkMessageActionResult(message_id=message_id, status="not_found")
                return BulkMessageActionResult(message_id=message_id, status="failed")
            except Exception as error:
                logger.warning("Bulk action failed: %s", type(error).__name__)
                return BulkMessageActionResult(message_id=message_id, status="failed")
            result = BulkMessageActionResult(message_id=message_id, status="done", action_id=action.id)
            try:
                message = await message_access.get_message(message_id)
                if message is not None:
                    insights = await insight_access.get_for_messages([message.id])
                    result.message = list_item(message, await colors(), insights.get(message.id))
            except Exception as error:
                logger.warning("Bulk action display failed: %s", type(error).__name__)
            return result

    aliases = await message_access.resolve_aliases(body.message_ids)
    targets = {}
    for message_id in body.message_ids:
        targets.setdefault(aliases.get(message_id, message_id), message_id)
    # Resolve the first submitted reference again at dispatch in case its binding was withdrawn.
    return BulkMessageActionOut(results=await asyncio.gather(*(apply(first_id) for first_id in targets.values())))


@router.post("/{message_id}/actions", operation_id="mail_message_action", summary="Apply an undoable message action")
async def message_action(message_id: str, body: MessageActionIn) -> MessageActionOut:
    """Update one message at its provider and retain its previous state for 24-hour undo."""
    try:
        action = await actions.apply_action(message_id, body.kind)
    except actions.ActionUnsupportedError:
        raise refuse(409, "action_unsupported", "Das geht bei dieser Mail nicht.") from None
    except actions.ActionFailedError:
        raise refuse(502, "provider_failed", "Der Anbieter hat die Änderung nicht bestätigt. Prüfe die Mail nach dem nächsten Abruf.") from None
    message = await message_access.get_message(message_id)
    if message is None:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
    insights = await insight_access.get_for_messages([message.id])
    colors = color_indexes(await account_access.list_accounts())
    return MessageActionOut(action_id=action.id, message=list_item(message, colors, insights.get(message.id)))


@router.get("/{message_id}/insight", operation_id="mail_message_insight", summary="Get a concise mail insight")
async def message_insight(message_id: str, locale: Locale | None = None) -> InsightOut:
    """Return the cached insight or generate it for this message."""
    try:
        row = await insights.get_or_compute(message_id, locale=locale)
    except insights.MessageNotFound:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.") from None
    except insights.InsightError as error:
        logger.warning("Insight failed: %s", type(error.__cause__ or error).__name__)
        raise refuse(503, "insight_unavailable", "Das Modell antwortet gerade nicht, deshalb fehlt die Zusammenfassung. Öffne die Mail gleich noch mal.") from None
    return InsightOut(points=json.loads(row.points_json), deadline=row.deadline, needs_reply=row.needs_reply,
                      followups=json.loads(row.followups_json), model=row.model, created_at=row.created_at)


@router.get("/{message_id}/original", operation_id="mail_message_original", summary="Read the original headers and source of one message")
async def message_original(message_id: str) -> MessageOriginalOut:
    """Fetch the original without storing it or changing its read status."""
    message = await message_access.get_message(message_id)
    if message is None:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
    account = await account_access.get_account(message.account_id)
    if not original.available(account, message):
        raise refuse(409, "original_unavailable", "Bei dieser Mail lässt sich das Original nicht abrufen.")
    try:
        result = await original.fetch(account, message)
    except actions.ActionUnsupportedError:
        raise refuse(409, "original_unavailable", "Bei dieser Mail lässt sich das Original nicht abrufen.") from None
    except Exception as error:
        logger.warning("Original fetch failed: %s", type(error).__name__)
        raise refuse(502, "provider_failed", "Das Original ließ sich beim Anbieter nicht abrufen. Versuch es gleich noch mal.") from None
    return MessageOriginalOut(**result)


@router.get("/{message_id}/html", operation_id="mail_message_html_get", summary="Read bounded HTML for a browser mail view")
async def get_message_html(message_id: str, response: Response) -> MessageHtmlOut:
    message = await message_access.get_message(message_id, include_html=True)
    if message is None:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
    response.headers["Cache-Control"] = "no-store"
    return MessageHtmlOut(html=message.body_html if message.html_status == "available" else None)


@router.get("/{message_id}", operation_id="mail_message_get", summary="Read one synced message in full")
async def get_message(message_id: str) -> MessageDetailOut:
    """Return one indexed message or 404 if it does not exist."""
    message = await message_access.get_message(message_id)
    local_reference = message is None
    if local_reference:
        message = await mail_reference_snapshot.read(message_id)
    if message is None:
        raise refuse(404, "message_not_found", "Die Mail ist nicht mehr da.")
    insights = {} if local_reference else await insight_access.get_for_messages([message.id])
    accounts = await account_access.list_accounts()
    colors = color_indexes(accounts)
    account = next((account for account in accounts if account.id == message.account_id), None)
    return MessageDetailOut(
        **list_item(message, colors, insights.get(message.id)).model_dump(),
        to=json.loads(message.recipients_json), cc=json.loads(message.cc_json),
        body_text=message.body_text, attachments=json.loads(message.attachments_json),
        thread_id=message.thread_id, can_unsubscribe=not local_reference and bool(message.list_unsubscribe),
        can_show_original=not local_reference and original.available(account, message), local_reference_only=local_reference,
    )
