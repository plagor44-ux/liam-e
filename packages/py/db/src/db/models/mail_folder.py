"""Owner-defined global app folders and labels, independent of providers."""

from datetime import datetime

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


class MailFolder(SQLModel, table=True):
    __tablename__ = "mail_folder"
    __table_args__ = (
        CheckConstraint("kind IN ('folder', 'label')", name="ck_mail_folder_kind"),
        UniqueConstraint("kind", "name", name="uq_mail_folder_kind_name"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str
    kind: str
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
