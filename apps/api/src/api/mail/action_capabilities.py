"""Provider policy for label-based and folder-based mail actions."""

from typing import Literal

from db.accessors import mail_sync_state
from db.models import mail_account
from db.models.mail_sync_state import MailSyncState
from fastapi import HTTPException
from pydantic import BaseModel

from api.errors import refuse
from api.mail.connector_write_results import PERMISSION_MISSING

WriteAction = Literal["archive", "trash", "restore", "spam", "mark_read", "mark_unread", "move", "label"]
ERROR_CODE = "provider_action_unsupported"
AGENT_NOTE = "This action is unavailable for this Gmail account in Mistral Studio. App-side filing is still available."
REAUTH_NOTE = "Reconnect Gmail in Mistral Studio under Context > Connectors > Gmail, then allow another attempt in liam-e."


class MailActionCapabilities(BaseModel):
    archive: bool
    trash: bool
    restore: bool
    spam: bool
    mark_read: bool
    mark_unread: bool
    move: bool
    label: bool


def for_account(account: mail_account.MailAccount, state: MailSyncState | None = None) -> MailActionCapabilities:
    return MailActionCapabilities(**{
        action: mail_account.provider_action_state(account, state, action) == "available"
        for action in MailActionCapabilities.model_fields
    })


def blocked(account: mail_account.MailAccount, state: MailSyncState | None) -> bool:
    return mail_account.provider_action_state(account, state, "mark_read") == "blocked"


def unsupported() -> HTTPException:
    return refuse(409, ERROR_CODE, "Für dieses Konto ist diese Mail-Aktion nicht verfügbar.")


def permission_missing() -> HTTPException:
    return refuse(403, PERMISSION_MISSING,
                  "Melde Gmail in Mistral Studio unter Kontext → Connectors → Gmail erneut an und erlaube in liam-e einen neuen Versuch.")


async def require_action(account, action: str) -> MailSyncState | None:
    state = await mail_sync_state.get_state(account.id) if mail_account.gmail_studio(account) else None
    outcome = mail_account.provider_action_state(account, state, action)
    if outcome == "blocked":
        raise permission_missing()
    if outcome == "unsupported":
        raise unsupported()
    return state
