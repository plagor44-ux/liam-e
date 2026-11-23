"""Inbox overview across connected accounts."""

from datetime import datetime
from typing import Literal

from db.accessors import chat_settings, mail_account, mail_job, mail_message, mail_sync_state
from fastapi import APIRouter
from pydantic import BaseModel

from api.mail import action_capabilities, newsletters, state
from api.mail.action_capabilities import MailActionCapabilities

router = APIRouter()


class AccountSummaryOut(BaseModel):
    id: str
    display_name: str
    email_address: str
    provider: str
    connection: Literal["connector", "oauth", "imap"]
    action_capabilities: MailActionCapabilities
    color_index: int
    unread: int
    state: Literal["ok", "syncing", "auth_expired", "error", "never"]
    error_code: str
    last_sync_at: datetime | None


class MailSummaryOut(BaseModel):
    mail_scope_version: int | None = None
    unread_total: int
    accounts: list[AccountSummaryOut]
    last_sync_at: datetime | None
    newsletter_senders: int


async def account_summary(account, colors: dict[str, int], unread: dict[str, int], sync_state) -> AccountSummaryOut:
    job = await mail_job.latest_for_account(account.id, kind="sync", open_only=True)
    status = state.account_status(account, sync_state, job)
    return AccountSummaryOut(id=account.id, display_name=account.display_name, email_address=account.email_address, provider=account.provider, connection=state.connection(account), action_capabilities=action_capabilities.for_account(account), color_index=colors[account.id], unread=unread.get(account.id, 0), state=status.state, error_code=status.error_code, last_sync_at=status.last_sync_at)


@router.get("", operation_id="mail_summary")
async def mail_summary(chat_scope: bool = False) -> MailSummaryOut:
    """Return inbox counts and sync status, optionally limited to the current chat grant."""
    accounts = await mail_account.list_accounts()
    colors = state.color_indexes(accounts)
    version = None
    ids = None
    if chat_scope:
        grants = await chat_settings.get()
        version = grants.version
        accounts = [a for a in accounts if a.is_active and a.id in grants.account_ids]
        ids = [a.id for a in accounts]
    unread = await mail_message.count_unread(folder="INBOX", account_ids=ids, chat_only=chat_scope)
    summaries = [await account_summary(a, colors, unread, await mail_sync_state.get_state(a.id)) for a in accounts]
    dates = [a.last_sync_at for a in summaries if a.last_sync_at is not None]
    senders = await newsletters.find_newsletters(account_ids=ids, chat_only=chat_scope)
    return MailSummaryOut(mail_scope_version=version, unread_total=sum(a.unread for a in summaries), accounts=summaries, last_sync_at=max(dates, default=None), newsletter_senders=len(senders))
