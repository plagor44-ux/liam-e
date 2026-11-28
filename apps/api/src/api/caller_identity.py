"""What kind of caller a request through the Studio app gateway comes from (#200).

The gateway forwards neither ``x-api-key`` nor ``Authorization`` to the app. Browser sessions, MCP
sign-ins and, as far as known, workspace-key calls all arrive with a caller token in
``x-apps-token`` (``docs/superpowers/probes/2026-11-25-mcp-route-sign-in.md``). The one signal
Mistral itself verifies is ``GET {gateway}/v1/users/me`` with that token: a user session answers
with ``api_key: null`` and the user, organization and workspace, and a key-authenticated identity
answers with a non-null ``api_key`` (Mistral Users API). :func:`caller_identity` asks that once per
token and keeps the answer briefly:

- per app instance, keyed by a SHA-256 hash of the gateway URL and the token, never the token;
- a user, an API key or a rejection is reused while fresh (:data:`FRESH_SECONDS`);
- concurrent requests for one token share the lookup in flight;
- a failed lookup never becomes a verdict. It is not repeated within the same request (the caller
  gate and the owner check share one lookup), and for :data:`RETRY_SECONDS` the token is not
  looked up again, so a slow or failing Users API costs one wait per token, not one per request;
- with ``allow_stale=True`` a failed lookup falls back to the token's last definite answer, as
  long as it is younger than its grace period (:data:`STALE_SECONDS`). The answer keeps its kind:
  a stale refusal or API key never turns into a user.

A request that brings a gateway token to an app with no gateway configured is not treated as a
local run: the app cannot check the token, so it counts as an unavailable lookup.

Nothing here logs a token, an ID or a response body; the log lines carry fixed codes only.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import logging
import time
from uuid import UUID

import httpx
from mistralai_capabilities.apps.tokens import token_from
from pydantic import BaseModel, ValidationError
from starlette.requests import HTTPConnection
from utils.mistral import MistralNotConfiguredError, caller_credentials

__all__ = [
    "FRESH_SECONDS",
    "RETRY_SECONDS",
    "STALE_SECONDS",
    "CallerIdentity",
    "CallerKind",
    "caller_identity",
]

_LOG = logging.getLogger(__name__)
_LOOKUP_SECONDS = 4
_MAX_IDENTITY_BYTES = 16 * 1024
_MAX_ENTRIES = 512
_CACHE_ATTR = "caller_identity_cache"
_FAILED_ATTR = "caller_identity_failed"  # on request.state: the key whose lookup failed


class CallerKind(StrEnum):
    USER = "user"  # a person's session: browser or MCP sign-in
    API_KEY = "api_key"  # a key-authenticated identity, such as a workspace API key
    REJECTED = "rejected"  # the Users API refused the token (401 or 403)
    NO_TOKEN = "no_token"  # a gateway is configured, but the request carries no caller token
    NO_GATEWAY = "no_gateway"  # no gateway in front of the app, such as local development
    UNAVAILABLE = "unavailable"  # no verdict: timeout, transport, status, answer shape, or no gateway configured


# How long a definite answer is reused. An API-key verdict is kept longer: it is the one a caller
# would want to forget, and keeping it makes a later failed lookup for the same token harmless.
FRESH_SECONDS: dict[CallerKind, float] = {
    CallerKind.USER: 60.0,
    CallerKind.REJECTED: 60.0,
    CallerKind.API_KEY: 600.0,
}
# How long, counted from the lookup, a definite answer may stand in for a failed one. A user's
# grace is short: it keeps the owner's browser working through a brief outage, not a long one. A
# refusal or an API key is kept longer, since standing in for it only ever refuses.
STALE_SECONDS: dict[CallerKind, float] = {
    CallerKind.USER: 900.0,
    CallerKind.REJECTED: 3600.0,
    CallerKind.API_KEY: 3600.0,
}
# After a failed lookup, how long the same token goes without a new one.
RETRY_SECONDS = 10.0


@dataclass(frozen=True, repr=False)
class CallerIdentity:
    """One request's caller. The IDs are set only for :attr:`CallerKind.USER`."""

    kind: CallerKind
    user_id: UUID | None = None
    organization_id: UUID | None = None
    workspace_id: UUID | None = None

    def __repr__(self) -> str:  # keeps IDs out of tracebacks and test output
        return f"CallerIdentity(kind={self.kind.value!r})"


class _Scope(BaseModel):
    id: UUID


class _User(BaseModel):
    id: UUID
    organization: _Scope
    workspace: _Scope
    api_key: None


@dataclass(frozen=True)
class _Entry:
    identity: CallerIdentity
    fetched_at: float


class _Cache:
    """Bounded, least-recently-used maps from a token hash to its last definite answer and to the
    time of its last failed lookup, plus the lookups still in flight."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._failures: OrderedDict[str, float] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[CallerIdentity]] = {}

    def lookup(self, key: str, server_url: str, token: str) -> asyncio.Task[CallerIdentity]:
        """The lookup in flight for ``key`` on this event loop, started if there is none.

        Concurrent requests for one token share it, so a slow Users API costs one wait per token,
        not one per request. The task records its own outcome, so a cancelled request leaves it
        running for the others. A task left on another event loop (an app reused across test
        clients) is never joined.
        """
        loop = asyncio.get_running_loop()
        task = self._inflight.get(key)
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._run(key, server_url, token))
            task.add_done_callback(_retrieve)
            self._inflight[key] = task
        return task

    async def _run(self, key: str, server_url: str, token: str) -> CallerIdentity:
        started = _now()
        try:
            identity = await _lookup(server_url, token)
            if identity.kind is CallerKind.UNAVAILABLE:
                self.failed(key, _now())
            else:
                self.put(key, identity, started)
            return identity
        finally:
            if self._inflight.get(key) is asyncio.current_task():
                del self._inflight[key]

    def get(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key: str, identity: CallerIdentity, now: float) -> None:
        self._entries[key] = _Entry(identity, now)
        self._entries.move_to_end(key)
        while len(self._entries) > _MAX_ENTRIES:
            self._entries.popitem(last=False)
        self._failures.pop(key, None)

    def failed(self, key: str, now: float) -> None:
        self._failures[key] = now
        self._failures.move_to_end(key)
        while len(self._failures) > _MAX_ENTRIES:
            self._failures.popitem(last=False)

    def failed_recently(self, key: str, now: float) -> bool:
        failed_at = self._failures.get(key)
        return failed_at is not None and now - failed_at < RETRY_SECONDS

    def __len__(self) -> int:
        return len(self._entries)


_FALLBACK_CACHE = _Cache()


def _retrieve(task: asyncio.Task[CallerIdentity]) -> None:
    # Every awaiting request may have been cancelled; reading the outcome keeps asyncio from
    # logging an unretrieved exception, whose text could name the gateway.
    if not task.cancelled():
        task.exception()


def _now() -> float:
    return time.monotonic()


def _cache_for(request: HTTPConnection) -> _Cache:
    app = request.scope.get("app")
    state = getattr(app, "state", None)
    if state is None:
        return _FALLBACK_CACHE
    cache = getattr(state, _CACHE_ATTR, None)
    if not isinstance(cache, _Cache):
        cache = _Cache()
        setattr(state, _CACHE_ATTR, cache)
    return cache


def _key(server_url: str, token: str) -> str:
    return hashlib.sha256(f"{server_url}\0{token}".encode()).hexdigest()


def _unavailable(reason: str) -> CallerIdentity:
    _LOG.info("caller_identity=unavailable reason=%s", reason)
    return CallerIdentity(CallerKind.UNAVAILABLE)


def _classify(body: bytes) -> CallerIdentity:
    try:
        data = json.loads(body)
    except ValueError:
        return _unavailable("shape")
    if not isinstance(data, dict):
        return _unavailable("shape")
    if data.get("api_key") is not None:
        return CallerIdentity(CallerKind.API_KEY)
    try:
        user = _User.model_validate(data)
    except ValidationError:
        return _unavailable("shape")
    return CallerIdentity(CallerKind.USER, user.id, user.organization.id, user.workspace.id)


async def _lookup(server_url: str, token: str) -> CallerIdentity:
    # The request's own forwarded bearer, never the app's key: the app key names a deployment.
    try:
        async with asyncio.timeout(_LOOKUP_SECONDS), httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "GET", f"{server_url.rstrip('/')}/v1/users/me",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as response:
                if response.status_code in (401, 403):
                    return CallerIdentity(CallerKind.REJECTED)
                if response.status_code != 200:
                    return _unavailable(f"status_{response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_IDENTITY_BYTES:
                        return _unavailable("oversized")
                    body.extend(chunk)
    except TimeoutError:
        return _unavailable("timeout")
    except (httpx.HTTPError, httpx.InvalidURL):
        return _unavailable("transport")
    return _classify(bytes(body))


async def caller_identity(request: HTTPConnection, *, allow_stale: bool = False) -> CallerIdentity:
    """The kind of caller ``request`` comes from, looked up at most once per fresh period.

    ``allow_stale`` lets a failed lookup fall back to the token's last definite answer within its
    grace period. Use it only where a stale answer is safer than none, such as the ``/api/v1``
    caller gate; an owner check must keep the default.
    """
    try:
        credentials = caller_credentials()
    except MistralNotConfiguredError:
        return CallerIdentity(CallerKind.NO_TOKEN)
    if credentials is None:
        if token_from(request.headers):
            return _unavailable("gateway_unconfigured")
        return CallerIdentity(CallerKind.NO_GATEWAY)
    if not credentials.token:
        return CallerIdentity(CallerKind.NO_TOKEN)

    cache = _cache_for(request)
    key = _key(credentials.server_url, credentials.token)
    entry = cache.get(key)
    now = _now()
    if entry is not None and now - entry.fetched_at < FRESH_SECONDS[entry.identity.kind]:
        return entry.identity

    if getattr(request.state, _FAILED_ATTR, None) == key or cache.failed_recently(key, now):
        identity = CallerIdentity(CallerKind.UNAVAILABLE)
    else:
        identity = await asyncio.shield(cache.lookup(key, credentials.server_url, credentials.token))
    if identity.kind is CallerKind.UNAVAILABLE:
        setattr(request.state, _FAILED_ATTR, key)
        if allow_stale and entry is not None and now - entry.fetched_at < STALE_SECONDS[entry.identity.kind]:
            _LOG.info("caller_identity=stale kind=%s", entry.identity.kind.value)
            return entry.identity
    return identity
