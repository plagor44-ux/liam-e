"""The one installation-wide UI configuration; there is no per-request owner.

Like the prompt library, the owner is a deployment-level server constant (``OWNER_SCOPE``),
never a request identity: callers cannot choose or override it. The single row holds the
device-independent swipe assignment and language preference. The revision guards against
lost updates from concurrent devices.
"""

from datetime import datetime

from sqlalchemy import Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from db.models._common import new_id, utc_datetime_column, utcnow

OWNER_SCOPE = "installation"
BROWSER_LOCALE = "browser"


class UiSettings(SQLModel, table=True):
    __tablename__ = "ui_settings"
    __table_args__ = (
        UniqueConstraint("owner_scope", name="uq_ui_settings_owner_scope"),
    )

    id: str = Field(default_factory=new_id, primary_key=True)
    owner_scope: str = Field(default=OWNER_SCOPE)
    swipe_left: str
    swipe_right: str
    locale: str = Field(default=BROWSER_LOCALE, sa_column=Column(String, nullable=False, server_default=BROWSER_LOCALE))
    browser_locale: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    speech_pace: str = Field(default="normal", sa_column=Column(String, nullable=False, server_default="normal"))
    voice_model: str = Field(default="chat", sa_column=Column(String, nullable=False, server_default="chat"))
    revision: int = 1
    updated_at: datetime = Field(default_factory=utcnow, sa_column=utc_datetime_column())
