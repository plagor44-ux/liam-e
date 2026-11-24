"""Sync progress of one mail account: where the last run stopped and how it ended.

``cursor`` is provider-specific (Gmail ``historyId``, IMAP UIDVALIDITY + last UID, Graph
``@odata.deltaLink``). One row per account.
"""

from datetime import datetime
from hashlib import sha256

from sqlalchemy import JSON, Boolean, Column, UniqueConstraint, false, text
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


CONNECTOR_CURSOR_PREFIX = "mailbox-v1|"


class MailSyncState(SQLModel, table=True):
    __tablename__ = "mail_sync_state"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_mail_sync_state_account"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    account_id: str = Field(index=True)
    cursor: str = ""
    connector_cursor: str = Field(default="", sa_column_kwargs={"server_default": ""})
    # Stable identity of an initial import while a provider has historical pages left.
    rules_initial_import: str = Field(default="", sa_column_kwargs={"server_default": ""})
    # This existing generation also fences native polls against confirmed folder changes.
    connector_generation: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    connector_proof_revision: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    connector_deferred_threads: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False, server_default=text("'[]'")))
    connector_write_evidence: str = Field(default="", sa_column_kwargs={"server_default": ""})
    connector_verified: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=false()))
    status: str = "idle"  # idle | syncing | ok | error
    last_error: str = ""
    last_sync_at: datetime | None = Field(
        default=None, sa_column=utc_datetime_column(nullable=True)
    )
    updated_at: datetime = Field(
        default_factory=utcnow, sa_column=utc_datetime_column()
    )


    def write_evidence(self) -> tuple[str, str, str] | None:
        """Return the pinned binding, retry generation and recorded write outcome."""
        if not self.connector_verified or not self.connector_cursor.startswith(CONNECTOR_CURSOR_PREFIX):
            return None
        encoded, separator, _ = self.connector_cursor.removeprefix(CONNECTOR_CURSOR_PREFIX).partition("|")
        if not separator or not encoded:
            return None
        binding = sha256(encoded.encode()).hexdigest()
        saved, _, tail = self.connector_write_evidence.partition(":")
        generation, _, outcome = tail.partition(":")
        if saved != binding or outcome not in {"ready", "confirmed", "blocked"}:
            return binding, "", "ready"
        return binding, generation, outcome
