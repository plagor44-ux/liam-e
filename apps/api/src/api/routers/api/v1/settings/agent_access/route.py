"""The owner's switch for agent access from outside liam-e ("Zugriff von außen", #197).

Browser-only. Reading passes the ``/api/v1`` caller gate like any browser read
(:mod:`api.mcp.exposure`), also while the Users API is down. Turning access off needs nothing
beyond the gateway: off is the emergency stop, so the caller gate lets every caller through to
it, and it takes no revision, makes no identity lookup and always succeeds, even without an owner
binding or a reachable Mistral API. It always advances the revision, also when
access is already off, so a turn-on confirmed earlier, for example in another tab while its
identity lookup was still running, fails as stale instead of reopening access. Turning access on
needs the bound owner's own browser session (:mod:`api.owner_access`: no workspace key, the
bound origin, the matching Studio identity) and the current revision.

``verifier_configured`` reports whether a real read-access verifier is installed; until then the
read facade keeps answering 401 even with access on. ``owner_bound`` reports whether turning on
can work at all. Neither exposes a binding value. Every answer is ``Cache-Control: no-store``.
"""

from datetime import datetime
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from api.errors import refuse
from api.mcp.access import DenyReadAccess, ReadAccessVerifier, get_read_access_verifier
from api.owner_access import OwnerVerifier, owner_binding, verify_studio_owner
from db.accessors import agent_access
from db.models.agent_access import AgentAccess

_LOG = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "no-store"}


def _refuse(status: int, code: str, message: str):
    error = refuse(status, code, message)
    error.headers = _NO_STORE
    return error


class AgentAccessRoute(APIRoute):
    """No echo of a rejected body, fail closed on storage errors, never cached."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                raise _refuse(422, "agent_access_invalid", "Send the current expected_revision and nothing else.") from None
            except (SQLAlchemyError, OSError):
                _LOG.warning("agent_access=unavailable")
                raise _refuse(503, "agent_access_unavailable", "The agent access state is unavailable; outside access stays refused.") from None
            response.headers.update(_NO_STORE)
            return response

        return guarded


router = APIRouter(route_class=AgentAccessRoute, responses={
    503: {"description": "The switch cannot be read or changed; outside access stays refused"},
})


class AgentAccessOut(BaseModel):
    enabled: bool
    revision: int
    changed_at: datetime | None
    verifier_configured: bool
    owner_bound: bool


class AgentAccessEnable(BaseModel):
    """Turning access on; the owner scope and identity are not part of the contract."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: Annotated[int, Field(ge=1, strict=True)]


def get_agent_access_owner_verifier() -> OwnerVerifier:
    return verify_studio_owner


def _out(row: AgentAccess, verifier: ReadAccessVerifier) -> AgentAccessOut:
    return AgentAccessOut(
        enabled=row.enabled,
        revision=row.revision,
        changed_at=row.changed_at,
        verifier_configured=not isinstance(verifier, DenyReadAccess),
        owner_bound=owner_binding() is not None,
    )


@router.get("", operation_id="agent_access_get")
async def get_agent_access(
    verifier: Annotated[ReadAccessVerifier, Depends(get_read_access_verifier)],
) -> AgentAccessOut:
    """The switch; off when it was never turned on."""
    return _out(await agent_access.state(), verifier)


@router.post("/disable", operation_id="agent_access_disable")
async def disable_agent_access(
    verifier: Annotated[ReadAccessVerifier, Depends(get_read_access_verifier)],
) -> AgentAccessOut:
    """Turn outside access off at once. No revision or identity check; always advances the revision."""
    row = await agent_access.disable()
    _LOG.info("agent_access=disabled")
    return _out(row, verifier)


@router.post("/enable", operation_id="agent_access_enable", responses={
    403: {"description": "Not the bound owner's own browser session"},
    409: {"description": "No owner binding, or the expected revision is stale"},
    422: {"description": "Invalid body; the rejected payload is not echoed"},
})
async def enable_agent_access(
    body: AgentAccessEnable,
    request: Request,
    verify: Annotated[OwnerVerifier, Depends(get_agent_access_owner_verifier)],
    verifier: Annotated[ReadAccessVerifier, Depends(get_read_access_verifier)],
) -> AgentAccessOut:
    """Turn outside access on, for the verified owner holding the current revision."""
    if owner_binding() is None:
        raise _refuse(409, "agent_access_owner_unbound", "No owner is bound to this app, so access cannot be turned on.")
    if await verify(request) is not True:
        raise _refuse(403, "agent_access_owner_required", "Only the owner's own signed-in browser can turn access on.")
    try:
        row = await agent_access.enable(body.expected_revision)
    except agent_access.StaleRevision:
        raise _refuse(409, "agent_access_conflict", "Access was changed in the meantime. Reload and check the current state.") from None
    _LOG.info("agent_access=enabled")
    return _out(row, verifier)
