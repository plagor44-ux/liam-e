"""Installation-wide mail account grants for the native MCP transport.

Account IDs survive credential renewal and default to an empty list when no row exists yet.
"""

from sqlalchemy import JSON, Column, text
from sqlmodel import Field, SQLModel

from db.models.prompt_template import OWNER_SCOPE


class McpSettings(SQLModel, table=True):
    __tablename__ = "mcp_settings"

    owner_scope: str = Field(default=OWNER_SCOPE, primary_key=True)
    account_ids: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False, server_default=text("'[]'::json")),
    )
    version: int = 1
