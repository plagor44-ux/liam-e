"""Runtime exposure inventory and workspace-key caller policy for ``/api/v1``.

``BROWSER_ONLY_GET_OPERATIONS`` is the single source of truth shared with
``apps/api/tests/test_mcp_exposure.py``: every schema-published GET operation outside this set is
part of the read-only inventory documented in AGENTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.requests import HTTPConnection
from starlette.routing import compile_path

from api.mcp.access import ensure_agent_access

_LOG = logging.getLogger(__name__)
_POLICY_INDEX_ATTR = "v1_agent_policy_index"

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


async def agent_caller_policy(request: HTTPConnection) -> None:
    """Enforce the outside-access switch and read-only boundary for ``x-api-key`` callers on ``/api/v1``.

    Requests without ``x-api-key`` pass immediately with no database read so browser traffic is
    unchanged. Callers with ``x-api-key`` are treated as agents: refused with 403
    ``agent_access_disabled`` while the switch is off, limited to the read-only GET inventory
    while on, and refused with 403 ``agent_operation_refused`` for browser-only GETs and all
    mutations.
    """
    if "x-api-key" not in request.headers:
        return

    index = _policy_index(request.app)
    route = _resolve_route(request, index)

    await ensure_agent_access(path_group="v1-key")

    if (
        request.scope.get("type") == "http"
        and request.scope.get("method") in {"GET", "HEAD"}
        and isinstance(route, APIRoute)
    ):
        operation_id = route.operation_id or route.unique_id
        if operation_id in index.read_only_get_operations:
            return

    _LOG.info("agent_access=refused path_group=v1-key")
    raise HTTPException(
        status_code=403,
        detail="agent_operation_refused",
        headers={"Cache-Control": "no-store"},
    )
