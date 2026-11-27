"""The owner's switch for agent access from outside liam-e ("Zugriff von außen").

One row per installation, keyed by the fixed server-side ``OWNER_SCOPE`` like the other
singletons. A missing row means the switch is off: the migration creates no row, and nothing
creates one on read. ``revision`` increases with every change of ``enabled`` and guards turning
access on against a stale page. The row stores no identity, reason or request data.
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, false
from sqlmodel import Field, SQLModel

from db.models._common import utc_datetime_column
from db.models.prompt_template import OWNER_SCOPE


class AgentAccess(SQLModel, table=True):
    __tablename__ = "agent_access"

    owner_scope: str = Field(default=OWNER_SCOPE, primary_key=True)
    enabled: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, server_default=false()))
    revision: int = 1
    changed_at: datetime | None = Field(default=None, sa_column=utc_datetime_column(nullable=True))
