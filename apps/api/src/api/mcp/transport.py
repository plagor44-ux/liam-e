"""Fail-closed ASGI mount and request guards for ``/api/mcp``.

Every request passes through:
1. ``ensure_agent_access(path_group="mcp")`` — the owner's outside-access switch, before any
   verifier runs;
2. explicit ``TransportSecuritySettings`` Host (421) and Origin (403) checks;
3. request body cap (413);
4. ``get_read_access_verifier()`` (respecting ``app.dependency_overrides``) returning a validated
   :class:`ReadGrant`, bound into a request-scoped ContextVar;
5. per-principal in-process rate limiting (429);
6. stateless HTTP method enforcement (405 ``Allow: POST`` for GET/DELETE and other methods).
"""

from __future__ import annotations

from collections import deque
import inspect
import logging
import math
import time
from urllib.parse import urlsplit

from env.api import env as http_env
from env.mail import env as mail_env
from env.mcp import env as mcp_env
from fastapi import FastAPI, HTTPException
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.mcp.access import ensure_agent_access, get_read_access_verifier, validate_grant
from api.mcp.server import bind_read_grant, create_mcp_server, create_streamable_http_app

_LOG = logging.getLogger(__name__)
MCP_MOUNT_PATH = "/api/mcp"
_NO_STORE = {"Cache-Control": "no-store"}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _origin_and_hosts(url: str) -> tuple[str | None, list[str], bool]:
    raw = url.strip().rstrip("/")
    if not raw:
        return None, [], False
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        return None, [], False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hosts = [parsed.netloc]
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if hostname not in hosts:
        hosts.append(hostname)
    return origin, hosts, parsed.hostname in _LOOPBACK_HOSTS


def build_transport_security() -> TransportSecuritySettings:
    """Build explicit DNS-rebinding protection settings from app configuration."""
    allowed_hosts: list[str] = []
    allowed_origins: list[str] = []
    loopback = False

    for candidate in (http_env.cors_origin, mail_env.mail_oauth_redirect_base_url):
        origin, hosts, is_loopback = _origin_and_hosts(candidate)
        if origin and origin not in allowed_origins:
            allowed_origins.append(origin)
        for host in hosts:
            if host not in allowed_hosts:
                allowed_hosts.append(host)
        loopback = loopback or is_loopback

    if loopback:
        for host in ("localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"):
            if host not in allowed_hosts:
                allowed_hosts.append(host)

    for host in _split_csv(mcp_env.mcp_allowed_hosts):
        if host not in allowed_hosts:
            allowed_hosts.append(host)
    for origin in _split_csv(mcp_env.mcp_allowed_origins):
        normalized = origin.rstrip("/")
        if normalized not in allowed_origins:
            allowed_origins.append(normalized)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class _PrincipalRateLimiter:
    """In-process sliding-window rate limiter keyed by verified principal."""

    def __init__(self, limit: int, window_seconds: float, *, max_principals: int = 1024) -> None:
        self._limit = max(1, limit)
        self._window = max(0.001, window_seconds)
        self._max_principals = max_principals
        self._buckets: dict[str, deque[float]] = {}

    def check(self, principal: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self._window
        bucket = self._buckets.get(principal)
        if bucket is None:
            if len(self._buckets) >= self._max_principals:
                self._evict(cutoff)
            bucket = deque()
            self._buckets[principal] = bucket

        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= self._limit:
            retry_after = max(1, math.ceil(self._window - (now - bucket[0])))
            return False, retry_after

        bucket.append(now)
        return True, 0

    def _evict(self, cutoff: float) -> None:
        stale = [key for key, timestamps in self._buckets.items() if not timestamps or timestamps[-1] <= cutoff]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) >= self._max_principals:
            oldest = next(iter(self._buckets))
            self._buckets.pop(oldest, None)


class McpTransportApp:
    """Guarded ASGI application mounted at ``/api/mcp``."""

    def __init__(
        self,
        *,
        app: FastAPI,
        inner: ASGIApp,
        security_settings: TransportSecuritySettings,
        max_body_bytes: int,
        rate_limit_requests: int,
        rate_limit_window_seconds: float,
    ) -> None:
        self._app = app
        self._inner = inner
        self._security = TransportSecurityMiddleware(security_settings)
        self._max_body_bytes = max(1, max_body_bytes)
        self._rate_limiter = _PrincipalRateLimiter(rate_limit_requests, rate_limit_window_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return

        # 1. Not-Aus outside-access switch runs first, before any verifier or transport code.
        try:
            await ensure_agent_access(path_group="mcp")
        except HTTPException as error:
            headers = dict(_NO_STORE)
            if error.headers:
                headers.update(error.headers)
            response = JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=headers,
            )
            await response(scope, receive, send)
            return

        request = Request(scope, receive)

        # 2. Explicit Host (421) and Origin (403) validation.
        security_error = await self._security.validate_request(request, is_post=False)
        if security_error is not None:
            security_error.headers.update(_NO_STORE)
            await security_error(scope, receive, send)
            return

        # 3. Early Content-Length cap before identity verification (inner transport also enforces streamed cap).
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = -1
            if declared_size > self._max_body_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "request_too_large"},
                    headers=_NO_STORE,
                )
                await response(scope, receive, send)
                return

        # 4. Per-request caller verification using the shared read-access verifier dependency.
        provider = self._app.dependency_overrides.get(get_read_access_verifier, get_read_access_verifier)
        try:
            verifier = provider()
            if inspect.isawaitable(verifier):
                verifier = await verifier
            grant = validate_grant(await verifier.verify(request))
        except HTTPException as error:
            headers = dict(_NO_STORE)
            if error.headers:
                headers.update(error.headers)
            response = JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=headers,
            )
            await response(scope, receive, send)
            return
        except Exception as error:
            _LOG.warning("mcp_verifier=error error=%s", type(error).__name__)
            response = JSONResponse(
                status_code=401,
                content={"detail": "unauthorized"},
                headers=_NO_STORE,
            )
            await response(scope, receive, send)
            return

        # 5. Per-principal in-process rate limit.
        allowed, retry_after = self._rate_limiter.check(grant.principal)
        if not allowed:
            _LOG.info("mcp_transport=rate_limited")
            response = JSONResponse(
                status_code=429,
                content={"detail": "rate_limited"},
                headers={**_NO_STORE, "Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # 6. Stateless transport accepts POST only across both protocol eras (GET/DELETE -> 405).
        if request.method != "POST":
            response = Response(status_code=405, headers={**_NO_STORE, "Allow": "POST"})
            await response(scope, receive, send)
            return

        # 7. Dispatch with the verified grant bound in a contextvar and no-store cache headers.
        async def send_no_store(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        with bind_read_grant(grant):
            await self._inner(scope, receive, send_no_store)


def mount_mcp_transport(app: FastAPI) -> MCPServer:
    """Mount the fail-closed Streamable HTTP MCP transport at ``/api/mcp``."""
    server = create_mcp_server(call_timeout_seconds=mcp_env.mcp_call_timeout_seconds)
    security = build_transport_security()
    inner = create_streamable_http_app(
        server,
        path=MCP_MOUNT_PATH,
        max_request_body_size=mcp_env.mcp_max_body_bytes,
        transport_security=security,
    )
    transport_app = McpTransportApp(
        app=app,
        inner=inner,
        security_settings=security,
        max_body_bytes=mcp_env.mcp_max_body_bytes,
        rate_limit_requests=mcp_env.mcp_rate_limit_requests,
        rate_limit_window_seconds=mcp_env.mcp_rate_limit_window_seconds,
    )
    app.router.routes.append(
        Route(
            MCP_MOUNT_PATH,
            endpoint=transport_app,
            include_in_schema=False,
            name="mcp",
        )
    )
    app.state.mcp_server = server
    app.state.mcp_transport_security = security
    return server
