"""A minimal, test-only MCP server over Streamable HTTP for the gateway probe (#157).

It exists to see what the Studio gateway does with MCP traffic on a route flagged ``"mcp": true``,
not to serve liam-e. It is stateless and speaks both protocol eras:

- 2026-07-28: every POST carries ``MCP-Protocol-Version``, ``Mcp-Method`` (and ``Mcp-Name`` for
  ``tools/call``), which must match the body's ``_meta`` and fields; a mismatch is 400 with
  ``HeaderMismatch`` (-32020), an unsupported version 400 with ``UnsupportedProtocolVersionError``
  (-32022), an unknown method 404 with -32601. ``server/discover`` replaces ``initialize``.
- 2025-11-25 and older: ``initialize`` and ``notifications/initialized``, no session ID.

GET and DELETE answer 405, because the server offers no stream and keeps no session. Its three
tools only read: the caller identity report (``identity.report``), the write counter, and the
counter again as an SSE response. The product transport in #36 uses the pinned MCP SDK; this stub
must not be copied there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from probe_app import identity
from probe_app.state import ProbeState

MODERN_VERSION = "2026-07-28"
HANDSHAKE_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = (MODERN_VERSION, *HANDSHAKE_VERSIONS)
# A request without the header comes from a client before 2025-06-18 (Streamable HTTP, rev. 2026-07-28).
HEADERLESS_VERSION = "2025-03-26"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
MAX_BODY_BYTES = 64 * 1024
SERVER_INFO = {"name": "liam-e-mcp-route-probe", "version": "1"}
NO_STORE = {"Cache-Control": "no-store"}

HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}
TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "probe_whoami",
        "description": "Report which caller headers and identity kind the probe app sees. Test-only.",
        "inputSchema": _NO_ARGUMENTS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "probe_counter",
        "description": "Read the probe app's in-memory write counter. Test-only.",
        "inputSchema": _NO_ARGUMENTS,
        "annotations": _READ_ONLY,
    },
    {
        "name": "probe_sse",
        "description": "Read the write counter, answered as a server-sent event stream. Test-only.",
        "inputSchema": _NO_ARGUMENTS,
        "annotations": _READ_ONLY,
    },
)
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


def _error(status: int, request_id: object, code: int, message: str, data: object = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error}, status_code=status, headers=NO_STORE)


def _result(request_id: object, result: dict[str, Any], *, modern: bool) -> dict[str, Any]:
    if modern:
        result = {"resultType": "complete", **result, "_meta": {META_SERVER_INFO: SERVER_INFO}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _tool_result(structured: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
        "structuredContent": structured,
        "isError": False,
    }


def protocol_header_value(headers: Mapping[str, str]) -> str:
    """The received ``MCP-Protocol-Version``, reduced to a known version, ``other`` or ``absent``."""
    value = headers.get("mcp-protocol-version")
    if value is None:
        return "absent"
    return value if value in SUPPORTED_VERSIONS else "other"


def method_not_allowed() -> Response:
    """GET opens no stream and DELETE ends no session: this server keeps neither."""
    return Response(status_code=405, headers={"Allow": "POST", **NO_STORE})


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # server-side clients send none (Streamable HTTP, Security Warning)
    host = request.headers.get("host", "")
    return origin in {f"https://{host}", f"http://{host}"} and bool(host)


async def handle(request: Request, *, route: str, state: ProbeState, lookup: identity.IdentityLookup) -> Response:
    if not _same_origin(request):
        return Response(status_code=403, headers=NO_STORE)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return Response(status_code=415, headers=NO_STORE)
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return Response(status_code=413, headers=NO_STORE)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return Response(status_code=413, headers=NO_STORE)
    try:
        message = json.loads(bytes(body))
    except ValueError:
        return _error(400, None, PARSE_ERROR, "Parse error")
    if not isinstance(message, dict):
        return _error(400, None, INVALID_REQUEST, "Batches are not supported")
    request_id = message.get("id")
    method = message.get("method")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(400, request_id, INVALID_REQUEST, "Invalid request")

    headers = request.headers
    header_version = headers.get("mcp-protocol-version")
    raw_meta = params.get("_meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    meta_version = meta.get(META_VERSION)

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in HANDSHAKE_VERSIONS else HANDSHAKE_VERSIONS[0]
        return JSONResponse(
            _result(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }, modern=False),
            headers=NO_STORE,
        )

    modern = meta_version is not None or header_version == MODERN_VERSION
    if modern:
        if header_version is None:
            return _error(400, request_id, HEADER_MISMATCH, "Missing MCP-Protocol-Version header")
        if meta_version != header_version:
            return _error(400, request_id, HEADER_MISMATCH, "MCP-Protocol-Version does not match _meta")
    version = header_version or HEADERLESS_VERSION
    if version not in SUPPORTED_VERSIONS:
        return _error(400, request_id, UNSUPPORTED_VERSION, "Unsupported protocol version",
                      {"supported": list(SUPPORTED_VERSIONS), "requested": "other"})
    modern = version == MODERN_VERSION
    if modern:
        if headers.get("mcp-method") != method:
            return _error(400, request_id, HEADER_MISMATCH, "Mcp-Method does not match the body")
        if method == "tools/call" and headers.get("mcp-name") != params.get("name"):
            return _error(400, request_id, HEADER_MISMATCH, "Mcp-Name does not match the body")

    if "id" not in message:
        return Response(status_code=202, headers=NO_STORE)  # a notification needs no answer

    if method == "ping":
        return JSONResponse(_result(request_id, {}, modern=modern), headers=NO_STORE)
    if method == "server/discover" and modern:
        return JSONResponse(_result(request_id, {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "ttlMs": 0,
            "cacheScope": "private",
        }, modern=True), headers=NO_STORE)
    if method == "tools/list":
        result: dict[str, Any] = {"tools": list(TOOLS)}
        if modern:
            result |= {"ttlMs": 0, "cacheScope": "private"}
        return JSONResponse(_result(request_id, result, modern=modern), headers=NO_STORE)
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return _error(200, request_id, INVALID_PARAMS, "Unknown tool")
        if name == "probe_whoami":
            structured = await identity.report(
                headers, list(headers.keys()), route=route, salt=state.salt, instance=state.instance, lookup=lookup,
            )
            structured |= {"protocol_header": protocol_header_value(headers),
                           "mcp_method_header": headers.get("mcp-method") is not None}
        else:
            structured = state.snapshot() | {"protocol_header": protocol_header_value(headers)}
        payload = _result(request_id, _tool_result(structured), modern=modern)
        if name == "probe_sse" and "text/event-stream" in headers.get("accept", ""):
            event = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return Response(event, media_type="text/event-stream", headers=NO_STORE)
        return JSONResponse(payload, headers=NO_STORE)
    # The 2026-07-28 rule; older clients read a 404 as an expired session, so they get 200.
    return _error(404 if modern else 200, request_id, METHOD_NOT_FOUND, "Method not found")
