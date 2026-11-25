"""What the app can learn about the caller of one request, reduced to safe values.

The report answers #157's Q4: which header carries the caller token, whether the gateway also
forwards ``Authorization``, ``x-api-key`` or cookies, and whether ``/v1/users/me`` at the gateway
proxy names a user rather than an API key. It never contains a token, a header value or an ID.
User and workspace appear only as salted fingerprints that match within one process
(``state.ProbeState``), so the owner can compare a browser reading with an MCP reading without the
probe ever printing who they are.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx

# The names the gateway sends the caller token under (mistralai_capabilities.apps.tokens).
TOKEN_HEADERS = ("x-apps-token", "x-space-token")
# The runtime injects the proxy URL under one of these names (packages/py/env/src/env/gateway.py).
PROXY_URL_VARIABLES = ("__APPS_PROXY_URL", "__SPACES_PROXY_URL")
MAX_IDENTITY_BYTES = 16 * 1024
LOOKUP_SECONDS = 4.0
MAX_HEADER_NAMES = 60
# Header names are reported so that MCP traffic can be told apart from a browser session. Only
# names made of up to five short letter-only words are kept, so a name that embeds a token, an ID
# or any digits is dropped.
_SAFE_HEADER_NAME = re.compile(r"^[a-z]{1,20}(?:-[a-z]{1,20}){0,4}$")


@dataclass(frozen=True)
class Identity:
    """The outcome of one identity lookup. ``user_id`` and ``workspace_id`` never leave this module."""

    kind: str  # user | api_key | unknown | lookup_rejected | lookup_failed | no_token | no_proxy
    user_id: str | None = None
    workspace_id: str | None = None

    def __repr__(self) -> str:  # keeps IDs out of tracebacks and test output
        return f"Identity(kind={self.kind!r})"


IdentityLookup = Callable[[str, str], Awaitable[Identity]]


def proxy_url(environ: Mapping[str, str] = os.environ) -> str | None:
    for name in PROXY_URL_VARIABLES:
        value = environ.get(name)
        if value:
            return value
    return None


def token_header(headers: Mapping[str, str]) -> str:
    for name in TOKEN_HEADERS:
        if headers.get(name):
            return name
    return "none"


def caller_token(headers: Mapping[str, str]) -> str | None:
    for name in TOKEN_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def safe_header_names(names: list[str]) -> list[str]:
    kept = sorted({name.lower() for name in names if _SAFE_HEADER_NAME.match(name.lower())})
    return kept[:MAX_HEADER_NAMES]


def fingerprint(salt: bytes, value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(salt + value.encode()).hexdigest()[:12]


def classify(status: int, body: object) -> Identity:
    """Map a ``/v1/users/me`` answer to an identity kind without keeping anything else."""
    if status in (401, 403):
        return Identity("lookup_rejected")
    if status != 200 or not isinstance(body, dict):
        return Identity("lookup_failed")
    user_id = body.get("id")
    workspace = body.get("workspace")
    workspace_id = workspace.get("id") if isinstance(workspace, dict) else None
    if "api_key" in body and body["api_key"] is None and isinstance(user_id, str) and user_id:
        return Identity("user", user_id, workspace_id if isinstance(workspace_id, str) else None)
    if body.get("api_key") is not None:
        return Identity("api_key")
    return Identity("unknown")


async def lookup_identity(proxy: str, token: str) -> Identity:
    """Ask the gateway proxy who the token belongs to, as ``api/audio/access.py`` does."""
    try:
        async with asyncio.timeout(LOOKUP_SECONDS), httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "GET",
                f"{proxy.rstrip('/')}/v1/users/me",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_IDENTITY_BYTES:
                        return Identity("lookup_failed")
                    body.extend(chunk)
                status = response.status_code
    except (TimeoutError, httpx.HTTPError, httpx.InvalidURL):
        return Identity("lookup_failed")
    try:
        parsed = json.loads(bytes(body)) if body else None
    except ValueError:
        parsed = None
    return classify(status, parsed)


async def report(
    headers: Mapping[str, str],
    header_names: list[str],
    *,
    route: str,
    salt: bytes,
    instance: str,
    lookup: IdentityLookup,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, object]:
    """The redacted identity report one probe route or tool returns."""
    token = caller_token(headers)
    proxy = proxy_url(environ)
    if token is None:
        identity = Identity("no_token")
    elif proxy is None:
        identity = Identity("no_proxy")
    else:
        identity = await lookup(proxy, token)
    return {
        "instance": instance,
        "route": route,
        "token_header": token_header(headers),
        "authorization_header": bool(headers.get("authorization")),
        "x_api_key_header": bool(headers.get("x-api-key")),
        "cookie_header": bool(headers.get("cookie")),
        "identity": identity.kind,
        "user_fingerprint": fingerprint(salt, identity.user_id),
        "workspace_fingerprint": fingerprint(salt, identity.workspace_id),
        "header_names": safe_header_names(header_names),
    }
