"""Server-side proof that a request comes from the bound Studio owner's own browser session.

Shared by browser audio and the agent access switch. The binding is the workspace secret
``AUDIO_STUDIO_OWNER`` (user, organization and workspace IDs plus the browser origin); its name
predates the second user. A request passes only when all of these hold:

- no workspace key (``x-api-key``) is present;
- a non-GET request or websocket carries the bound browser origin;
- ``GET {gateway}/v1/users/me`` with the request's own forwarded bearer returns the bound user,
  organization and workspace, and no API key as the credential type.

Anything else, including a missing or malformed binding, a timeout or an oversized answer, is
``False``. The identity lookup itself, with its short per-token cache, is
:func:`api.caller_identity.caller_identity`; :func:`is_bound_owner` is the comparison for callers
that are not browsers, such as an MCP verifier. The binding's values never leave this module.
"""

from collections.abc import Awaitable, Callable
from uuid import UUID

from env.audio import env
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import HTTPConnection

from api.caller_identity import CallerIdentity, CallerKind, caller_identity

OwnerVerifier = Callable[[HTTPConnection], Awaitable[bool]]
_MAX_BINDING_BYTES = 1024


class StudioOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    origin: str = Field(pattern=r"^https://[a-zA-Z0-9.-]+(?::[0-9]+)?$")


def owner_binding() -> StudioOwner | None:
    """The configured owner binding, or ``None`` when it is missing or malformed."""
    configured = env.audio_studio_owner
    if not configured or len(configured) > _MAX_BINDING_BYTES:
        return None
    try:
        return StudioOwner.model_validate_json(configured)
    except ValidationError:
        return None


def is_bound_owner(identity: CallerIdentity) -> bool:
    """Whether ``identity`` is the bound owner's user session; ``False`` without a valid binding."""
    owner = owner_binding()
    return (
        owner is not None
        and identity.kind is CallerKind.USER
        and identity.user_id == owner.user_id
        and identity.organization_id == owner.organization_id
        and identity.workspace_id == owner.workspace_id
    )


async def verify_studio_owner(request: HTTPConnection) -> bool:
    owner = owner_binding()
    if owner is None or "x-api-key" in request.headers:
        return False
    if (request.scope["type"] == "websocket" or request.scope.get("method") not in {"GET", "HEAD"}) and request.headers.get("origin") != owner.origin:
        return False
    # A fresh answer only: an owner check never accepts a stale identity.
    return is_bound_owner(await caller_identity(request))
