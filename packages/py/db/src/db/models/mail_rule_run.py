"""Durable, deduplicated execution of a particular rule version on a mail."""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


class MailRuleRun(SQLModel, table=True):
    __tablename__ = "mail_rule_run"
    __table_args__ = (UniqueConstraint("rule_id", "rule_version", "message_id", name="uq_mail_rule_run_version_message"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    rule_id: str
    rule_version: int
    message_id: str = Field(index=True)
    account_id: str
    status: str = "queued"
    provider_results_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
