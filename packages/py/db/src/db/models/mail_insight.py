"""Model-generated insight for a single indexed message."""

from datetime import date, datetime

from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow


class MailInsight(SQLModel, table=True):
    __tablename__ = "mail_insight"

    id: str = Field(default_factory=new_id, primary_key=True)
    message_id: str = Field(unique=True)
    points_json: str = "[]"
    deadline: date | None = None
    needs_reply: bool = False
    followups_json: str = "[]"
    locale: str | None = None
    model: str
    created_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
