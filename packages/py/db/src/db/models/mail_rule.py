"""Versioned owner-defined mail classification rules."""

from datetime import datetime

from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


class MailRule(SQLModel, table=True):
    __tablename__ = "mail_rule"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    account_id: str | None = None
    sender_address: str = ""
    sender_domain: str = ""
    subject_contains: str = ""
    recipient: str = ""
    folder_id: str | None = None
    label_id: str | None = None
    mark_read: bool = False
    mark_unread: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    archive: bool = False
    position: int = 0
    is_active: bool = True
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
