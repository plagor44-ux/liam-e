"""Fail-closed integration boundary for a future independently verified read grant.

The default verifier deliberately rejects every request. A future transport must install a
verified implementation and pass its grant to the same services as the HTTP facade.
"""

from dataclasses import dataclass
import logging
import re
from typing import Literal, Protocol

from db.accessors import agent_access
from fastapi import Depends, HTTPException, Request

_LOG = logging.getLogger(__name__)
PathGroup = Literal["agent-read", "mcp", "v1-key"]


async def ensure_agent_access(*, path_group: PathGroup = "agent-read") -> None:
    """Fail closed unless the owner's outside-access switch is enabled.

    Reads the single-row switch on every call with no process-local cache so a change applies to
    the next request across all instances.
    """
    try:
        enabled = await agent_access.is_enabled()
    except Exception as error:
        _LOG.warning("agent_access=unavailable path_group=%s error=%s", path_group, type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="agent_access_unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None
    if enabled is not True:
        _LOG.info("agent_access=refused path_group=%s", path_group)
        raise HTTPException(
            status_code=403,
            detail="agent_access_disabled",
            headers={"Cache-Control": "no-store"},
        )


@dataclass(frozen=True)
class ReadGrant:
    account_ids: frozenset[str]


class ReadAccessVerifier(Protocol):
    async def verify(self, request: Request) -> ReadGrant: ...


class DenyReadAccess:
    async def verify(self, request: Request) -> ReadGrant:
        raise HTTPException(status_code=401, detail="unauthorized")


def get_read_access_verifier() -> ReadAccessVerifier:
    return DenyReadAccess()


async def require_read_access(
    request: Request, verifier: ReadAccessVerifier = Depends(get_read_access_verifier)
) -> ReadGrant:
    await ensure_agent_access()
    return validate_grant(await verifier.verify(request))


def validate_grant(grant: ReadGrant) -> ReadGrant:
    """Reject malformed or oversized grants rather than returning an unbounded envelope."""
    if (not isinstance(grant, ReadGrant) or not isinstance(grant.account_ids, frozenset)
            or len(grant.account_ids) > 100
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
                   for value in grant.account_ids)):
        raise HTTPException(status_code=401, detail="unauthorized")
    return grant


def restrict_accounts(grant: ReadGrant, requested: list[str] | None) -> frozenset[str]:
    """Resolve an optional selection without ever widening the verified grant."""
    validate_grant(grant)
    if requested is None:
        return grant.account_ids
    selected = frozenset(requested)
    if not selected <= grant.account_ids:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    return selected
