"""One connected mail account, whatever the provider.

``provider`` is ``google``, ``microsoft`` or ``imap``; ``auth_type`` is ``oauth2`` for the first
two and ``imap_password`` for the rest. Credentials never live on this row — they are encrypted in
``mail_credential`` and referenced here by ``id`` only (models carry no cross-table foreign keys).
"""

from datetime import datetime

from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow
from db.models.mail_sync_state import MailSyncState


def inbox_only(account) -> bool:
    return not account.email_address or account.email_address.casefold().endswith("@connector.mistral")


class ProviderActionUnsupported(ValueError):
    """The provider connection does not offer message label or folder changes."""


class ProviderActionPermissionMissing(ValueError):
    """The bound credential needs reauthentication before another attempt."""


GMAIL_STUDIO_ACTIONS = frozenset({"archive", "trash", "restore", "mark_read", "mark_unread"})


def gmail_studio(account: "MailAccount") -> bool:
    return account.auth_type == "mistral_connector" and account.provider in {"gmail", "google_mail"}


def provider_action_state(account: "MailAccount", state: MailSyncState | None, action: str) -> str:
    if not gmail_studio(account):
        return "available"
    evidence = state.write_evidence() if state else None
    if action not in GMAIL_STUDIO_ACTIONS or not account.is_active or inbox_only(account) or evidence is None:
        return "unsupported"
    return "blocked" if evidence[2] == "blocked" else "available"


class MailAccount(SQLModel, table=True):
    __tablename__ = "mail_account"

    id: str = Field(default_factory=new_id, primary_key=True)
    provider: str = Field(index=True)
    auth_type: str
    email_address: str = Field(index=True, unique=True)
    display_name: str = ""
    is_active: bool = True
    created_at: datetime = Field(
        default_factory=utcnow, sa_column=utc_datetime_column()
    )
    updated_at: datetime = Field(
        default_factory=utcnow, sa_column=utc_datetime_column()
    )
