"""Owner-defined global app folders and labels, independent of providers."""

from datetime import datetime
from typing import Literal

from sqlalchemy import CheckConstraint, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow

DestinationColor = Literal['none', 'moss', 'olive', 'teal', 'lake', 'denim', 'indigo', 'plum', 'berry']


class MailFolder(SQLModel, table=True):
    __tablename__ = "mail_folder"
    __table_args__ = (
        CheckConstraint("kind IN ('folder', 'label')", name="ck_mail_folder_kind"),
        UniqueConstraint("kind", "name", name="uq_mail_folder_kind_name"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    kind: str
    color: DestinationColor = Field(default='none', sa_column=Column(String, nullable=False, server_default='none'))
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
