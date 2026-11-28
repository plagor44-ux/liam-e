"""Fail-closed Studio MCP caller verifier built on :mod:`api.caller_identity`.

Uses the dedicated per-deployment ``MCP_STUDIO_OWNER`` binding (D5, separate from
``AUDIO_STUDIO_OWNER`` and without a browser ``origin`` requirement). A caller passes only when:

- ``MCP_STUDIO_OWNER`` parses to valid ``user_id``, ``organization_id``, and ``workspace_id`` UUIDs;
- no ``x-api-key`` header is present;
- :func:`api.caller_identity.caller_identity` (with ``allow_stale=False``) returns
  :attr:`CallerKind.USER` whose user, organization, and workspace match the binding.

The returned :class:`ReadGrant` carries the server-side ``mcp_settings`` account grant intersected
with active mail accounts. An empty grant returns an empty :class:`ReadGrant`, which yields empty
results rather than all accounts. Binding values and caller tokens are never logged.
"""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from db.accessors import mcp_settings
from env.mcp import env as mcp_env
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.caller_identity import CallerIdentity, CallerKind, caller_identity
from api.mcp.access import ReadGrant

_LOG = logging.getLogger(__name__)
_MAX_BINDING_BYTES = 1024
_NO_STORE = {"Cache-Control": "no-store"}


class McpStudioOwner(BaseModel):
    """Per-deployment Studio owner binding for MCP callers (no browser origin)."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    organization_id: UUID
    workspace_id: UUID


def mcp_owner_binding() -> McpStudioOwner | None:
    """Return the configured ``MCP_STUDIO_OWNER`` binding, or ``None`` if unset or malformed."""
    configured = mcp_env.mcp_studio_owner
    if not configured or len(configured) > _MAX_BINDING_BYTES:
        return None
    try:
        return McpStudioOwner.model_validate_json(configured)
    except ValidationError:
        return None


def is_bound_mcp_owner(identity: CallerIdentity, owner: McpStudioOwner | None = None) -> bool:
    """Whether ``identity`` is a verified ``USER`` matching ``MCP_STUDIO_OWNER``."""
    bound = owner if owner is not None else mcp_owner_binding()
    if bound is None or identity.kind is not CallerKind.USER:
        return False
    return (
        identity.user_id == bound.user_id
        and identity.organization_id == bound.organization_id
        and identity.workspace_id == bound.workspace_id
    )


def _principal_key(owner: McpStudioOwner) -> str:
    digest = hashlib.sha256(f"{owner.user_id}:{owner.organization_id}:{owner.workspace_id}".encode()).hexdigest()
    return f"studio-{digest[:16]}"


class StudioMcpReadVerifier:
    """Verify a Studio MCP caller against ``MCP_STUDIO_OWNER`` and resolve the DB account grant."""

    async def verify(self, request: Request) -> ReadGrant:
        owner = mcp_owner_binding()
        if owner is None:
            _LOG.info("mcp_caller=refused reason=owner_unbound")
            raise HTTPException(status_code=401, detail="unauthorized", headers=_NO_STORE)

        if "x-api-key" in request.headers:
            _LOG.info("mcp_caller=refused reason=api_key_header")
            raise HTTPException(status_code=401, detail="unauthorized", headers=_NO_STORE)

        # Owner verification never accepts stale cached verdicts.
        identity = await caller_identity(request, allow_stale=False)
        if not is_bound_mcp_owner(identity, owner):
            _LOG.info("mcp_caller=refused kind=%s", identity.kind.value)
            raise HTTPException(status_code=401, detail="unauthorized", headers=_NO_STORE)

        try:
            account_ids = await mcp_settings.effective_account_ids()
        except (SQLAlchemyError, OSError):
            _LOG.warning("mcp_grant=unavailable")
            raise HTTPException(
                status_code=503,
                detail="mcp_grant_unavailable",
                headers=_NO_STORE,
            ) from None

        return ReadGrant(account_ids=account_ids, principal=_principal_key(owner))
