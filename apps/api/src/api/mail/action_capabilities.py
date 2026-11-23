"""Provider policy for label-based and folder-based mail actions."""

from typing import Literal

from db.models import mail_account
from fastapi import HTTPException
from pydantic import BaseModel

from api.errors import refuse

WriteAction = Literal["archive", "trash", "restore", "spam", "mark_read", "mark_unread", "move", "label"]
ERROR_CODE = "provider_action_unsupported"
AGENT_NOTE = ("The Gmail connector in Mistral Studio does not support changing labels or folders. "
              "The requested action cannot be performed. Reading, searching, summarizing and sending "
              "after confirmation remain available.")


class MailActionCapabilities(BaseModel):
    archive: bool
    trash: bool
    restore: bool
    spam: bool
    mark_read: bool
    mark_unread: bool
    move: bool
    label: bool


def for_account(account: mail_account.MailAccount) -> MailActionCapabilities:
    allowed = mail_account.label_writes_supported(account.provider, account.auth_type)
    return MailActionCapabilities(**dict.fromkeys(MailActionCapabilities.model_fields, allowed))


def unsupported() -> HTTPException:
    return refuse(409, ERROR_CODE, "Gmail in Mistral Studio unterstützt diese Mail-Aktion nicht.")


def require_action(account: mail_account.MailAccount, action: WriteAction) -> None:
    if not getattr(for_account(account), action):
        raise unsupported()
