"""Owner-managed mail account grants for the native MCP read transport (D4)."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from api.errors import quiet_validation_route, refuse
from db.accessors import mcp_settings
from db.accessors.prompt_template import StaleVersion

router = APIRouter(route_class=quiet_validation_route("mcp_settings_invalid", "Check the account selection."))


class McpSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    account_ids: list[str]
    version: int


class McpSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    account_ids: list[str] = Field(max_length=mcp_settings.MAX_GRANT_ACCOUNTS)


@router.get("", operation_id="mcp_settings_get")
async def get_mcp_settings() -> McpSettingsOut:
    try:
        return McpSettingsOut.model_validate(await mcp_settings.get())
    except SQLAlchemyError:
        raise refuse(503, "settings_unavailable", "Settings are currently unavailable.") from None


@router.patch("", operation_id="mcp_settings_update")
async def update_mcp_settings(body: McpSettingsUpdate) -> McpSettingsOut:
    try:
        row = await mcp_settings.update(body.expected_version, body.account_ids)
        return McpSettingsOut.model_validate(row)
    except StaleVersion:
        raise refuse(409, "version_conflict", "The MCP account grant changed in the meantime. Reload and review it.") from None
    except mcp_settings.AccountNotAvailable:
        raise refuse(422, "mcp_settings_invalid", "At least one selected account is missing or inactive.") from None
    except SQLAlchemyError:
        raise refuse(503, "settings_unavailable", "Settings are currently unavailable.") from None
