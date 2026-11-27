"""Server-side proof that a request comes from the bound Studio owner's own browser session.

Shared by browser audio and the agent access switch. The binding is the workspace secret
``AUDIO_STUDIO_OWNER`` (user, organization and workspace IDs plus the browser origin); its name
predates the second user. A request passes only when all of these hold:

- no workspace key (``x-api-key``) is present;
- a non-GET request or websocket carries the bound browser origin;
- ``GET {gateway}/v1/users/me`` with the request's own forwarded bearer returns the bound user,
  organization and workspace, and no API key as the credential type.

Anything else, including a missing or malformed binding, a timeout or an oversized answer, is
``False``. The binding's values never leave this module.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from uuid import UUID

from env.audio import env
from starlette.requests import HTTPConnection
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from utils.mistral import MistralNotConfiguredError, caller_credentials

OwnerVerifier = Callable[[HTTPConnection], Awaitable[bool]]
_LOG = logging.getLogger(__name__)
_MAX_IDENTITY_BYTES = 16 * 1024
_MAX_BINDING_BYTES = 1024


class StudioOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    origin: str = Field(pattern=r"^https://[a-zA-Z0-9.-]+(?::[0-9]+)?$")


class _StudioScope(BaseModel):
    id: UUID


class _StudioUser(BaseModel):
    id: UUID
    organization: _StudioScope
    workspace: _StudioScope
    api_key: None


def owner_binding() -> StudioOwner | None:
    """The configured owner binding, or ``None`` when it is missing or malformed."""
    configured = env.audio_studio_owner
    if not configured or len(configured) > _MAX_BINDING_BYTES:
        return None
    try:
        return StudioOwner.model_validate_json(configured)
    except ValidationError:
        return None


async def verify_studio_owner(request: HTTPConnection) -> bool:
    owner = owner_binding()
    if owner is None or "x-api-key" in request.headers:
        return False
    if (request.scope["type"] == "websocket" or request.scope.get("method") not in {"GET", "HEAD"}) and request.headers.get("origin") != owner.origin:
        return False
    try:
        credentials = caller_credentials()
    except MistralNotConfiguredError:
        return False
    if not credentials:
        return False

    # The gateway proxy is installed by the runtime. The user lookup must use the current
    # request's bearer alone. The app key identifies a deployment, even if its creator is the owner.
    try:
        async with asyncio.timeout(4), httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "GET", f"{credentials.server_url.rstrip('/')}/v1/users/me",
                headers={"Authorization": f"Bearer {credentials.token}", "Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    _LOG.info("Studio owner identity lookup returned HTTP %d", response.status_code)
                    return False
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_IDENTITY_BYTES:
                        return False
                    body.extend(chunk)
        identity = _StudioUser.model_validate_json(body)
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, ValidationError):
        _LOG.info("Studio owner identity lookup did not establish a scoped browser session")
        return False
    return (
        identity.id == owner.user_id
        and identity.organization.id == owner.organization_id
        and identity.workspace.id == owner.workspace_id
    )
