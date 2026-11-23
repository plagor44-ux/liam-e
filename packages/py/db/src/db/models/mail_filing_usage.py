"""Bounded installation-wide filing history; no message data is stored."""

from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlmodel import Field, SQLModel

from db.models._common import utc_datetime_column, utcnow


class MailFilingUsage(SQLModel, table=True):
    __tablename__ = "mail_filing_usage"
    __table_args__ = (CheckConstraint("use_count > 0", name="ck_mail_filing_usage_count"),)

    destination_id: str = Field(primary_key=True)
    use_count: int = 1
    last_used_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column(index=True, nullable=False))
