"""Runtime exposure inventory and the agent caller policy for ``/api/v1``.

``BROWSER_ONLY_GET_OPERATIONS`` is the single source of truth shared with
``apps/api/tests/test_mcp_exposure.py``: every schema-published GET operation outside this set is
part of the read-only inventory documented in AGENTS.md.

:func:`agent_caller_policy` decides per request whether the caller counts as an agent (#197 D1 = B,
#200). Behind the Studio gateway the app never sees ``x-api-key``, so the caller kind comes from
:func:`api.caller_identity.caller_identity`.

- A user session passes, and so does every request without a gateway in front (local runs).
- An ``x-api-key`` header, an API-key identity, a request without a caller token and a token the
  Users API rejects count as agents. While the switch is off they get 403
  ``agent_access_disabled``. While it is on they may use only the read-only GETs, and everything
  else gets 403 ``agent_operation_refused``.
- Turning access off (``agent_access_disable``) passes for every caller without any lookup, so the
  emergency stop never depends on the Users API.
- When the lookup fails and there is no recent definite answer for the token, a GET or HEAD passes
  and anything else gets 503 ``caller_identity_unavailable``. Both are logged with fixed codes. An
  outage therefore never locks the owner's browser out of reading, and a key holder who could make
  the lookup fail still cannot change or send anything.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.requests import HTTPConnection
from starlette.routing import compile_path

from api.caller_identity import CallerKind, caller_identity
from api.mcp.access import ensure_agent_access

_LOG = logging.getLogger(__name__)
_POLICY_INDEX_ATTR = "v1_agent_policy_index"
# Turning access off only ever reduces access, so anyone may do it, with no lookup at all.
ALWAYS_ALLOWED_OPERATIONS: frozenset[str] = frozenset({"agent_access_disable"})
_AGENT_KINDS: dict[CallerKind, str] = {
    CallerKind.API_KEY: "v1-api-key",
    CallerKind.NO_TOKEN: "v1-no-token",
    CallerKind.REJECTED: "v1-rejected",
}
_NO_STORE = {"Cache-Control": "no-store"}

# Private preference/settings reads also stay browser-only: read-only does not mean safe
# to expose personal values to an agent. GETs that change state: OAuth belongs to the browser; its callback
# stores credentials and enqueues a sync. They are part of the OpenAPI, so they are named here
# rather than left out of the read-only list silently.
BROWSER_ONLY_GET_OPERATIONS: frozenset[str] = frozenset(
    {
        "agent_access_get",  # Owner switch state is browser-only.
        "audio_voices_list",
        "mail_message_html_get",  # Browser-only untrusted markup.
        "mail_folders_list",  # Private owner-managed folder and label names.
        "mail_folders_page",  # The paginated inventory is also private.
        "mail_filing_quick_actions",  # Installation usage history is browser-only.
        "mail_rules_list",
        "mail_rule_runs_list",
        "audio_capabilities_get",  # Owner-only application capability.
        "audio_device_test_get",
        "audio_device_test_sample",
        # Provider calls expose private calendar names; never an agent discovery tool.
        "calendar_google_calendars_list",
        "calendar_occurrence_live",
        "calendar_write_get",
        "preferences_list",
        "preference_candidates_list",  # Private candidate values and own source messages.
        "preference_context_get",
        "settings_get",
        "chat_settings_get",
        "chat_model_settings_get",
        "calendar_settings_get",
        "mcp_settings_get",
        "chat_mail_reference_get",
        "mail_account_oauth_authorize",
        "mail_account_oauth_callback",
        "skill_tools_list",
        "skills_list",
        "skill_get",
    }
)


@dataclass(frozen=True)
class _V1PolicyIndex:
    read_only_get_operations: frozenset[str]
    http_routes: tuple[tuple[re.Pattern[str], frozenset[str], APIRoute], ...]


def _build_policy_index(app: FastAPI) -> _V1PolicyIndex:
    read_only: set[str] = set()
    http_routes: list[tuple[re.Pattern[str], frozenset[str], APIRoute]] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.original_route
        if isinstance(route, APIRoute):
            methods = frozenset(route.methods or ())
            if ctx.path is not None:
                path_regex, _, _ = compile_path(ctx.path)
                http_routes.append((path_regex, methods, route))
            if route.include_in_schema and "GET" in methods:
                op_id = route.operation_id or route.unique_id
                if op_id not in BROWSER_ONLY_GET_OPERATIONS:
                    read_only.add(op_id)
    return _V1PolicyIndex(
        read_only_get_operations=frozenset(read_only),
        http_routes=tuple(http_routes),
    )


def _policy_index(app: FastAPI) -> _V1PolicyIndex:
    cached = getattr(app.state, _POLICY_INDEX_ATTR, None)
    if not isinstance(cached, _V1PolicyIndex):
        cached = _build_policy_index(app)
        setattr(app.state, _POLICY_INDEX_ATTR, cached)
    return cached


def read_only_get_operations(app: FastAPI) -> frozenset[str]:
    """The app's served read-only GET operation IDs, computed once per app instance."""
    return _policy_index(app).read_only_get_operations


def _resolve_route(request: HTTPConnection, index: _V1PolicyIndex) -> object | None:
    route = request.scope.get("route")
    if route is not None:
        return route
    if request.scope.get("type") != "http":
        return None
    path = request.scope.get("path", "")
    method = request.scope.get("method", "")
    for path_regex, methods, candidate in index.http_routes:
        if (method in methods or (method == "HEAD" and "GET" in methods)) and path_regex.match(path):
            return candidate
    return None


def _operation_id(route: object | None) -> str | None:
    return (route.operation_id or route.unique_id) if isinstance(route, APIRoute) else None


def _is_read(request: HTTPConnection) -> bool:
    return request.scope.get("type") == "http" and request.scope.get("method") in {"GET", "HEAD"}


async def _apply_agent_policy(request: HTTPConnection, route: object | None, index: _V1PolicyIndex, path_group: str) -> None:
    await ensure_agent_access(path_group=path_group)
    if _is_read(request) and _operation_id(route) in index.read_only_get_operations:
        return
    _LOG.info("agent_access=refused path_group=%s", path_group)
    raise HTTPException(status_code=403, detail="agent_operation_refused", headers=_NO_STORE)


async def agent_caller_policy(request: HTTPConnection) -> None:
    """Treat agent callers on ``/api/v1`` as the owner's switch allows; see the module docstring."""
    index = _policy_index(request.app)
    route = _resolve_route(request, index)
    if _operation_id(route) in ALWAYS_ALLOWED_OPERATIONS:
        return
    if "x-api-key" in request.headers:
        await _apply_agent_policy(request, route, index, "v1-key")
        return

    kind = (await caller_identity(request, allow_stale=True)).kind
    if kind in (CallerKind.USER, CallerKind.NO_GATEWAY):
        return
    if kind is CallerKind.UNAVAILABLE:
        if _is_read(request):
            _LOG.warning("agent_caller=unverified outcome=read_allowed")
            return
        _LOG.warning("agent_caller=unverified outcome=refused")
        raise HTTPException(
            status_code=503,
            detail="caller_identity_unavailable",
            headers={**_NO_STORE, "Retry-After": "5"},
        )
    await _apply_agent_policy(request, route, index, _AGENT_KINDS[kind])
