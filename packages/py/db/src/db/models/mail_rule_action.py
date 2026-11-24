"""At-most-once identity of one provider action of a rule on a mail, across rule versions.

A row exists once any run of the rule claimed that action for the mail. ``run_id`` names the run
allowed to dispatch it. ``dispatched`` is set when a dispatch was attempted or cannot be ruled out;
only a skip recorded without any dispatch may be claimed again by an explicit manual apply.
"""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


class MailRuleAction(SQLModel, table=True):
    __tablename__ = "mail_rule_action"
    __table_args__ = (UniqueConstraint("rule_id", "message_id", "kind", name="uq_mail_rule_action_identity"),)

    id: str = Field(default_factory=new_id, primary_key=True)
    rule_id: str
    message_id: str = Field(index=True)
    kind: str
    run_id: str
    status: str = "pending"
    dispatched: bool = False
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
    updated_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
