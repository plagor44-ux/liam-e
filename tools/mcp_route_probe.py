"""Probe the Studio gateway's MCP-route sign-in against the synthetic probe app (#157).

Offline by default: prints the cases and sends nothing. With ``--live`` it runs the matrix of
``docs/superpowers/probes/2026-11-25-mcp-route-sign-in.md`` against the deployed probe app
(``tools/probes/mcp_route_app``), whose HTTPS base URL is read from ``MCP_ROUTE_PROBE_URL``:

    MCP_ROUTE_PROBE_URL=<probe-app-url> bash tools/uv.sh run --no-sync python tools/mcp_route_probe.py --live

A live run needs the owner's approval. It discovers the authorization server from the flagged
route's bearer challenge, registers a public client dynamically, opens the owner's browser for the
sign-in (authorization code with PKCE ``S256``, ``resource`` and a loopback redirect on
``127.0.0.1``) and then calls the probe app with the token. The token stays in memory and is never
written or printed. ``resource`` is ``<app>/mcp`` unless ``--resource-from-metadata`` sends the
value the route's protected-resource metadata names; the output shows which (``resource_sent``).

Output is one line per case: the HTTP status, fixed codes, header and field names, booleans, counts
and duration buckets. It never prints a URL, token, client ID, user or workspace ID, or response
text. The probe app's own fingerprints are salted per process and only compared, never decoded.
Exception texts are dropped because they can carry a URL. Never call liam-e's own app with this
tool: it is made for the probe app, whose routes it names.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, NoReturn, Protocol, TypeGuard
from urllib.parse import parse_qs, urlencode, urlsplit

URL_VARIABLE = "MCP_ROUTE_PROBE_URL"
PRIMARY_ROUTE = "/mcp"
NESTED_ROUTE = "/api/mcp"
HEALTH_PATH = "/api/health"
WHOAMI_PATH = "/api/probe/whoami"
WRITE_PATH = "/api/probe/write"
# Read-only and free of charge. Sent only with --q3-mistral-api, to see whether the gateway's token
# is bound to the probe app or also works as a general Mistral credential.
MISTRAL_API_PROBE = "https://api.mistral.ai/v1/models"
MODERN = "2026-07-28"
HANDSHAKE = "2025-11-25"
KNOWN_VERSIONS = frozenset({MODERN, HANDSHAKE, "2025-06-18", "2025-03-26"})
EXPECTED_TOOLS = frozenset({"probe_whoami", "probe_counter", "probe_sse"})
CALLBACK_PATH = "/callback"
CALLBACK_VALUES = ("code", "state", "error", "iss")  # the only values the callback keeps, and only in memory
CALLBACK_BODY_LIMIT = 64 * 1024
CALLBACK_FIELD_LIMIT = 64
SIGN_IN_SECONDS = 300
TIMEOUT_SECONDS = 20
USER_AGENT = "liam-e-mcp-route-probe/1"

# Codes the gateway, an OAuth server or the probe app may return. Anything else becomes "other".
KNOWN_ERROR_CODES = frozenset({
    "session_expired", "access_denied", "unauthorized", "forbidden", "not_found", "invalid_token",
    "insufficient_scope", "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "invalid_redirect_uri", "invalid_client_metadata",
    "server_error", "temporarily_unavailable", "login_required", "consent_required",
    "interaction_required", "method_not_allowed",
    "invalid_target",  # RFC 8707: the authorization server refuses the resource parameter
})
KNOWN_RPC_CODES = frozenset({-32700, -32600, -32601, -32602, -32603, -32020, -32022})
CHALLENGE_PARAMS = frozenset({"resource_metadata", "scope", "error", "error_description", "realm", "resource"})
REPORTED_HEADERS = ("www-authenticate", "location", "set-cookie", "allow", "mcp-session-id")
IDENTITY_KINDS = frozenset({"user", "api_key", "unknown", "lookup_rejected", "lookup_failed", "no_token", "no_proxy"})
TOKEN_HEADERS = frozenset({"x-apps-token", "x-space-token", "none"})
ROUTES = frozenset({PRIMARY_ROUTE, NESTED_ROUTE, "/api"})
_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_SCOPE = re.compile(r"^[a-z][a-z_.:-]{0,40}$")
# The probe app's rule (probe_app/identity.py): up to five short letter-only words, so a name that
# embeds a token, an ID or any digits is dropped.
_HEADER_NAME = re.compile(r"^[a-z]{1,20}(?:-[a-z]{1,20}){0,4}$")
# Callback parameter names such as code, state, iss or error_description; a name with digits,
# capitals or dashes could embed an identifier and becomes "other".
_PARAM_NAME = re.compile(r"^[a-z][a-z_]{0,31}$")
_CHALLENGE_PARAM = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))')

# What --live does, in order; the offline run prints this list.
CASES: tuple[str, ...] = (
    "Q2 baseline: GET /api/health without a credential",
    "Q1 discovery: POST /mcp without a credential; bearer challenge, resource and authorization server metadata",
    "Q7 discovery: POST /api/mcp without a credential (flagged route under the unflagged /api prefix)",
    "Q1 sign-in: dynamic client registration, browser sign-in with PKCE S256 and resource, code exchange "
    "(resource <app>/mcp, or with --resource-from-metadata the one the metadata names)",
    "Q8/Q9 modern era on /mcp: server/discover, tools/list, probe_whoami, probe_counter, probe_sse",
    "Q9 protocol checks on /mcp: header mismatch, unsupported version, GET",
    "Q8 handshake era on /mcp: initialize, notifications/initialized, tools/list",
    "Q2 route binding: the token on POST /api/probe/write, GET /api/probe/whoami and GET /api/health",
    "Q2 write effect: probe_counter after the write attempt",
    "Q7/Q3 the token on /api/mcp",
    "Q3 (only with --q3-mistral-api) the token at the Mistral API, GET /v1/models",
    "Q6 refresh grant, if a refresh token was issued",
    "Q6 (only with --revocation-step) pause for the owner to revoke, then repeat a call and a refresh",
    "verdict: GO-CANDIDATE, INCOMPLETE or BLOCKED with fixed reasons",
)


# --- safe values -------------------------------------------------------------------------------


def safe_code(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value if value in KNOWN_ERROR_CODES else "other"
    return None


def safe_rpc_code(value: object) -> int | str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value in KNOWN_RPC_CODES else "other"
    return None


def safe_scopes(value: object) -> str:
    """Scope names that look like plain words; anything that could hold an identifier is "other"."""
    if isinstance(value, str):
        tokens = value.split()
    elif isinstance(value, list):
        tokens = [item for item in value if isinstance(item, str)]
    else:
        return "-"
    if not tokens:
        return "-"
    return "+".join(token if _SCOPE.match(token) else "other" for token in tokens[:8])


def safe_header_names(names: Iterable[object]) -> tuple[str, ...]:
    kept = {name.lower() for name in names if isinstance(name, str)}
    return tuple(sorted(name for name in kept if _HEADER_NAME.match(name)))


def safe_param_names(names: Iterable[object]) -> tuple[str, ...]:
    """The sorted names of the callback's parameters, never their values."""
    return tuple(sorted({name if isinstance(name, str) and _PARAM_NAME.match(name) else "other" for name in names}))


def callback_path_class(path: str) -> str:
    """Where the authorization server sent the browser: ``exact``, ``trailing_slash`` or ``other``."""
    if path == CALLBACK_PATH:
        return "exact"
    if path == f"{CALLBACK_PATH}/":
        return "trailing_slash"
    return "other"


def content_kind(content_type: str | None) -> str:
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    return {"application/json": "json", "text/event-stream": "sse", "text/html": "html", "": "none"}.get(kind, "other")


def bucket_seconds(value: object) -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return "absent"
    for limit, label in ((300, "<=5m"), (3600, "<=1h"), (86400, "<=24h"), (2592000, "<=30d")):
        if value <= limit:
            return label
    return ">30d"


@dataclass(frozen=True)
class Challenge:
    """A ``WWW-Authenticate`` header. Only ``summary`` is ever printed."""

    scheme: str
    params: tuple[str, ...]
    error: str | None
    resource_metadata: str | None = field(default=None, repr=False)
    scope: str | None = field(default=None, repr=False)

    def summary(self) -> str:
        return f"{self.scheme}[{','.join(self.params) or '-'}]"


def parse_challenge(header: str | None) -> Challenge | None:
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    values = {match.group(1).lower(): match.group(2) if match.group(2) is not None else match.group(3)
              for match in _CHALLENGE_PARAM.finditer(rest)}
    names = tuple(sorted({name if name in CHALLENGE_PARAMS else "other" for name in values}))
    return Challenge(
        scheme=scheme.lower() if scheme.lower() in {"bearer", "basic", "dpop"} else "other",
        params=names,
        error=safe_code(values.get("error")),
        resource_metadata=values.get("resource_metadata"),
        scope=values.get("scope"),
    )


# --- output ------------------------------------------------------------------------------------


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, tuple | list):
        return ",".join(str(item) for item in value) or "-"
    return str(value)


@dataclass(frozen=True)
class Outcome:
    """One printed line. ``key`` identifies the case for the verdict; ``fields`` hold safe values only."""

    key: str
    label: str
    status: int | str
    fields: tuple[tuple[str, object], ...] = ()

    def get(self, name: str) -> object:
        return dict(self.fields).get(name)

    def line(self) -> str:
        details = " ".join(f"{name}={_fmt(value)}" for name, value in self.fields)
        return f"{self.label}: {self.status}" + (f" {details}" if details else "")


def response_fields(response: Any) -> list[tuple[str, object]]:
    """Content kind, a fixed error code, the challenge summary and reported header names."""
    fields: list[tuple[str, object]] = [("content", content_kind(response.headers.get("content-type")))]
    body = _json_or_none(response)
    if isinstance(body, dict):
        code = safe_code(body.get("error") if isinstance(body.get("error"), str) else body.get("code"))
        if code:
            fields.append(("error", code))
        rpc = body.get("error") if isinstance(body.get("error"), dict) else None
        if rpc is not None:
            fields.append(("rpc_error", safe_rpc_code(rpc.get("code"))))
    challenge = parse_challenge(response.headers.get("www-authenticate"))
    if challenge is not None:
        fields.append(("challenge", challenge.summary()))
        if challenge.error:
            fields.append(("challenge_error", challenge.error))
    names = tuple(sorted({h.lower() for h in response.headers.keys()} & set(REPORTED_HEADERS)))
    fields.append(("headers", names))
    return fields


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _json_or_none(response: Any) -> object:
    try:
        return response.json()
    except ValueError:
        return None


# --- MCP messages ------------------------------------------------------------------------------


def mcp_request(era: str, request_id: int | None, method: str, params: dict[str, Any] | None = None,
                *, meta_version: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    payload = dict(params or {})
    if era == MODERN:
        payload["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": meta_version or MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": USER_AGENT, "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    if payload:
        body["params"] = payload
    return body


def mcp_headers(era: str, method: str, *, name: str | None = None, token: str | None = None,
                version_header: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if era == MODERN:
        headers["MCP-Protocol-Version"] = version_header or MODERN
        headers["Mcp-Method"] = method
        if name is not None:
            headers["Mcp-Name"] = name
    elif version_header:
        headers["MCP-Protocol-Version"] = version_header
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_mcp(response: Any) -> dict[str, Any] | None:
    """The JSON-RPC message of a JSON or SSE answer."""
    if content_kind(response.headers.get("content-type")) == "sse":
        message = None
        for event in response.text.split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if data:
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    message = parsed
        return message
    body = _json_or_none(response)
    return body if isinstance(body, dict) else None


def _validated(name: str, value: object) -> object:
    if name in {"instance", "user_fingerprint", "workspace_fingerprint"}:
        return value if isinstance(value, str) and _HEX12.match(value) else (None if value is None else "other")
    if name in {"authorization_header", "x_api_key_header", "cookie_header", "mcp_method_header"}:
        return value if isinstance(value, bool) else "other"
    if name == "count":
        return value if isinstance(value, int) and not isinstance(value, bool) else "other"
    if name == "route":
        return value if value in ROUTES else "other"
    if name == "token_header":
        return value if value in TOKEN_HEADERS else "other"
    if name == "identity":
        return value if value in IDENTITY_KINDS else "other"
    if name == "protocol_header":
        return value if value in KNOWN_VERSIONS | {"absent", "other"} else "other"
    if name == "header_names":
        return safe_header_names(value) if isinstance(value, list) else ()
    return "other"


REPORT_FIELDS = ("instance", "route", "token_header", "authorization_header", "x_api_key_header", "cookie_header",
                 "identity", "user_fingerprint", "workspace_fingerprint", "protocol_header", "mcp_method_header",
                 "count", "header_names")


def tool_report(message: dict[str, Any] | None) -> list[tuple[str, object]]:
    """The probe app's structured tool result, reduced to validated fields."""
    result = message.get("result") if isinstance(message, dict) else None
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if not isinstance(structured, dict):
        return [("result", "missing")]
    return [(name, _validated(name, structured[name])) for name in REPORT_FIELDS if name in structured]


# --- OAuth -------------------------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_https(url: object) -> TypeGuard[str]:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password


def same_host(first: str, second: str) -> bool:
    return (urlsplit(first).hostname or "").lower() == (urlsplit(second).hostname or "").lower()


def _origin(url: str) -> tuple[str, str, int | None] | None:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port if port is not None else {"https": 443, "http": 80}.get(parts.scheme)


def resource_relation(advertised: object, expected: str) -> str:
    """How the resource that the metadata names relates to the route URL the probe sends.

    ``equal`` (ignoring a trailing slash), ``origin`` (the bare origin), ``parent`` (a path prefix
    of the route), ``other_path``, ``other_host`` (another scheme, host or port, or no URL at all),
    or ``absent``. Only the class is printed, never the value.
    """
    if not isinstance(advertised, str) or not advertised:
        return "absent"
    origin = _origin(advertised)
    if origin is None or origin != _origin(expected):
        return "other_host"
    first, second = urlsplit(advertised), urlsplit(expected)
    if (first.query, first.fragment) != (second.query, second.fragment):
        return "other_path"
    path, route = first.path.rstrip("/"), second.path.rstrip("/")
    if path == route:
        return "equal"
    if not path:
        return "origin"
    return "parent" if route.startswith(f"{path}/") else "other_path"


def redirect_echoed(body: object, redirect_uri: str) -> str:
    """Whether a registration answer lists the registered redirect URI unchanged: ``yes``, ``no`` or ``absent``."""
    uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if not isinstance(uris, list):
        return "absent"
    return "yes" if redirect_uri in uris else "no"


def resource_metadata_urls(resource: str) -> list[str]:
    """RFC 9728 well-known locations for ``resource``, path-specific first."""
    parts = urlsplit(resource)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    urls = [f"{origin}/.well-known/oauth-protected-resource{path}"] if path else []
    return [*urls, f"{origin}/.well-known/oauth-protected-resource"]


def authorization_metadata_urls(issuer: str) -> list[str]:
    """RFC 8414 and OpenID discovery locations in the order MCP clients try them."""
    parts = urlsplit(issuer)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    if not path:
        return [f"{origin}/.well-known/oauth-authorization-server", f"{origin}/.well-known/openid-configuration"]
    return [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{origin}{path}/.well-known/openid-configuration",
    ]


@dataclass
class Token:
    access: str = field(repr=False)
    refresh: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "Token(<redacted>)"


@dataclass
class Discovery:
    resource: str = field(repr=False)  # the route URL the probe calls
    scope: str | None = field(default=None, repr=False)
    metadata: dict[str, Any] | None = field(default=None, repr=False)
    metadata_url: str | None = field(default=None, repr=False)
    issuer: str | None = field(default=None, repr=False)
    advertised_resource: str | None = field(default=None, repr=False)  # the metadata's resource, if https
    use_advertised: bool = False  # set by --resource-from-metadata

    @property
    def parameter(self) -> str:
        """The ``resource`` parameter (RFC 8707) of the authorization and token requests."""
        return self.advertised_resource if self.use_advertised and self.advertised_resource else self.resource

    @property
    def sent(self) -> str:
        return "metadata" if self.use_advertised and self.advertised_resource else "route"

    def __repr__(self) -> str:
        return "Discovery(<redacted>)"


@dataclass
class Client:
    client_id: str = field(repr=False)
    client_secret: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "Client(<redacted>)"


class Callback(Protocol):
    redirect_uri: str

    def wait(self, timeout: float) -> dict[str, str] | None: ...

    def shape(self) -> tuple[tuple[str, object], ...]: ...

    def close(self) -> None: ...


def _parse_form(text: str) -> dict[str, list[str]]:
    try:
        return parse_qs(text, keep_blank_values=True, max_num_fields=CALLBACK_FIELD_LIMIT)
    except ValueError:
        return {"": [""]}  # too many fields: reported as the single name "other"


class LoopbackCallback:
    """A one-shot HTTP listener on 127.0.0.1 for the authorization response.

    The first GET or POST on any path except ``/favicon.ico`` is the answer, so a server that sends
    the browser to ``/callback/`` or posts the response (``response_mode=form_post``) still reaches
    the probe. ``wait`` returns only ``code``, ``state``, ``error`` and ``iss``, from the query or a
    form body. ``shape`` reports how the answer came: method, path class, where the parameters were
    and their sorted names, never a value. Every other request gets an empty 404.
    """

    def __init__(self) -> None:
        received: dict[str, str] = {}
        shape: list[tuple[str, object]] = []
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                self._answer("GET")

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                self._answer("POST")

            def _answer(self, method: str) -> None:
                parts = urlsplit(self.path)
                if parts.path == "/favicon.ico" or done.is_set():
                    self._reply(404, b"")
                    return
                query = _parse_form(parts.query)
                form = _parse_form(self._form_body()) if method == "POST" else {}
                for name in CALLBACK_VALUES:
                    value = next((values[0] for values in (form.get(name), query.get(name)) if values and values[0]), None)
                    if value:
                        received[name] = value
                sources = [label for label, found in (("query", query), ("form", form)) if found]
                shape.extend([("method", method), ("path", callback_path_class(parts.path)),
                              ("source", "+".join(sources) or "none"), ("params", safe_param_names({*query, *form}))])
                done.set()
                self._reply(200, b"Sign-in finished. You can close this tab and return to the probe.")

            def _form_body(self) -> str:
                kind = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return ""
                if kind != "application/x-www-form-urlencoded" or not 0 < length <= CALLBACK_BODY_LIMIT:
                    return ""
                return self.rfile.read(length).decode("utf-8", "replace")

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                if body:
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the default logs the code
                del format, args

        self._received = received
        self._shape = shape
        self._done = done
        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}{CALLBACK_PATH}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: float) -> dict[str, str] | None:
        return dict(self._received) if self._done.wait(timeout) else None

    def shape(self) -> tuple[tuple[str, object], ...]:
        return tuple(self._shape) if self._done.is_set() else ()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- the live run ------------------------------------------------------------------------------


class Probe:
    """Runs the matrix with one ``httpx.Client``. Every outcome goes to ``emit`` as soon as it exists."""

    def __init__(self, client: Any, base_url: str, emit: Callable[[Outcome], None]) -> None:
        self.client = client
        self.base = base_url.rstrip("/")
        self.emit = emit
        self.outcomes: list[Outcome] = []
        self._next_id = 0

    def record(self, key: str, label: str, status: int | str, fields: Iterable[tuple[str, object]] = ()) -> Outcome:
        outcome = Outcome(key, label, status, tuple(fields))
        self.outcomes.append(outcome)
        self.emit(outcome)
        return outcome

    def send(self, method: str, url: str, **kwargs: Any) -> tuple[Any | None, str | None]:
        try:
            return self.client.request(method, url, **kwargs), None
        except Exception as exc:  # noqa: BLE001 - only the class name is kept; texts can hold the URL
            return None, f"transport:{type(exc).__name__}"

    def plain(self, key: str, label: str, method: str, url: str, **kwargs: Any) -> Outcome:
        response, failure = self.send(method, url, **kwargs)
        if response is None:
            return self.record(key, label, failure or "transport")
        return self.record(key, label, response.status_code, response_fields(response))

    def rpc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def mcp(self, key: str, label: str, route: str, era: str, method: str, *, token: Token | None,
            params: dict[str, Any] | None = None, name: str | None = None, notification: bool = False,
            version_header: str | None = None, meta_version: str | None = None,
            extra: Callable[[dict[str, Any] | None], Iterable[tuple[str, object]]] | None = None) -> Outcome:
        body = mcp_request(era, None if notification else self.rpc_id(), method, params, meta_version=meta_version)
        headers = mcp_headers(era, method, name=name, token=token.access if token else None, version_header=version_header)
        response, failure = self.send("POST", f"{self.base}{route}", json=body, headers=headers)
        if response is None:
            return self.record(key, label, failure or "transport")
        fields = response_fields(response)
        if response.status_code < 300 and not notification:
            message = parse_mcp(response)
            error = message.get("error") if isinstance(message, dict) else None
            if isinstance(error, dict):
                if all(name != "rpc_error" for name, _ in fields):  # an SSE body carries it only here
                    fields.append(("rpc_error", safe_rpc_code(error.get("code"))))
            elif extra is not None:
                fields.extend(extra(message))
        return self.record(key, label, response.status_code, fields)

    # -- discovery and sign-in

    def discover(self, route: str, key: str) -> Discovery:
        resource = f"{self.base}{route}"
        discovery = Discovery(resource=resource)
        response, failure = self.send("POST", resource, json=mcp_request(MODERN, self.rpc_id(), "tools/list"),
                                      headers=mcp_headers(MODERN, "tools/list"))
        if response is None:
            self.record(f"{key}.challenge", f"{key.upper()} challenge {route}, no credential", failure or "transport")
            return discovery
        challenge = parse_challenge(response.headers.get("www-authenticate"))
        self.record(f"{key}.challenge", f"{key.upper()} challenge {route}, no credential", response.status_code,
                    response_fields(response))
        discovery.scope = challenge.scope if challenge else None
        source = "challenge" if challenge and challenge.resource_metadata else "well-known"
        candidates = [challenge.resource_metadata] if challenge and challenge.resource_metadata else resource_metadata_urls(resource)
        prm, prm_status, prm_url = None, "not_found", None
        for url in candidates:
            if not is_https(url):
                prm_status = "insecure_url"
                break
            got, failure = self.send("GET", url, headers={"Accept": "application/json"})
            if got is None:
                prm_status = failure or "transport"
                continue
            prm_status = got.status_code
            body = _json_or_none(got)
            if got.status_code == 200 and isinstance(body, dict):
                prm, prm_url = body, url
                break
        servers = prm.get("authorization_servers") if isinstance(prm, dict) else None
        servers = [item for item in servers if isinstance(item, str)] if isinstance(servers, list) else []
        prm_fields: list[tuple[str, object]] = [("source", source)]
        if prm is not None and prm_url is not None:
            prm_fields += [
                ("same_host", same_host(prm_url, resource)),
                ("fields", tuple(sorted(name for name in prm if name in {
                    "resource", "authorization_servers", "scopes_supported", "bearer_methods_supported",
                    "resource_name", "resource_documentation"}))),
                ("resource_relation", resource_relation(prm.get("resource"), resource)),
                ("authorization_servers", len(servers)),
                ("scopes", safe_scopes(prm.get("scopes_supported"))),
            ]
            discovery.advertised_resource = prm["resource"] if is_https(prm.get("resource")) else None
            if discovery.scope is None and isinstance(prm.get("scopes_supported"), list):
                scopes = [item for item in prm["scopes_supported"] if isinstance(item, str)]
                discovery.scope = " ".join(scopes) or None
        self.record(f"{key}.prm", f"{key.upper()} resource metadata {route}", prm_status, prm_fields)
        if not servers or key != "q1":
            return discovery
        issuer = servers[0]
        if not is_https(issuer):
            self.record("q1.as", "Q1 authorization server", "insecure_url")
            return discovery
        as_status: int | str = "not_found"
        for url in authorization_metadata_urls(issuer):
            got, failure = self.send("GET", url, headers={"Accept": "application/json"})
            if got is None:
                as_status = failure or "transport"
                continue
            as_status = got.status_code
            body = _json_or_none(got)
            if got.status_code == 200 and isinstance(body, dict):
                discovery.metadata, discovery.metadata_url, discovery.issuer = body, url, issuer
                break
        meta = discovery.metadata or {}
        methods = _strings(meta.get("code_challenge_methods_supported"))
        grants = _strings(meta.get("grant_types_supported"))
        auth_methods = _strings(meta.get("token_endpoint_auth_methods_supported"))
        as_fields: list[tuple[str, object]] = []
        if discovery.metadata is not None:
            as_fields = [
                ("issuer_same_host", same_host(issuer, resource)),
                ("issuer_matches", isinstance(meta.get("issuer"), str) and meta["issuer"].rstrip("/") == issuer.rstrip("/")),
                ("s256", "S256" in methods),
                ("plain", "plain" in methods),
                ("registration_endpoint", is_https(meta.get("registration_endpoint"))),
                ("cimd", meta.get("client_id_metadata_document_supported") is True),
                ("refresh_grant", "refresh_token" in grants),
                ("auth_method_none", "none" in auth_methods),
                ("revocation_endpoint", is_https(meta.get("revocation_endpoint"))),
                ("endpoints_https", is_https(meta.get("authorization_endpoint")) and is_https(meta.get("token_endpoint"))),
                ("scopes", safe_scopes(meta.get("scopes_supported"))),
            ]
        self.record("q1.as", "Q1 authorization server", as_status, as_fields)
        return discovery

    def register(self, discovery: Discovery, redirect_uri: str) -> Client | None:
        meta = discovery.metadata or {}
        endpoint = meta.get("registration_endpoint")
        if not is_https(endpoint):
            self.record("q1.register", "Q1 client registration", "no_registration_endpoint")
            return None
        response, failure = self.send("POST", endpoint, headers={"Accept": "application/json"}, json={
            "client_name": "liam-e MCP route probe (test-only)",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        if response is None:
            self.record("q1.register", "Q1 client registration", failure or "transport")
            return None
        body = _json_or_none(response)
        client_id = body.get("client_id") if isinstance(body, dict) else None
        secret = body.get("client_secret") if isinstance(body, dict) else None
        self.record("q1.register", "Q1 client registration", response.status_code, [
            *response_fields(response), ("client_id", isinstance(client_id, str) and bool(client_id)),
            ("secret_issued", isinstance(secret, str) and bool(secret)),
            ("registration_redirect_echoed", redirect_echoed(body, redirect_uri)),
        ])
        if response.status_code not in (200, 201) or not isinstance(client_id, str) or not client_id:
            return None
        return Client(client_id, secret if isinstance(secret, str) and secret else None)

    def token_request(self, key: str, label: str, discovery: Discovery, client: Client,
                      form: dict[str, str]) -> Token | None:
        meta = discovery.metadata or {}
        endpoint = meta.get("token_endpoint")
        if not is_https(endpoint):
            self.record(key, label, "insecure_url")
            return None
        data = {**form, "client_id": client.client_id, "resource": discovery.parameter}
        if client.client_secret:
            data["client_secret"] = client.client_secret
        response, failure = self.send("POST", endpoint, data=data, headers={"Accept": "application/json"})
        if response is None:
            self.record(key, label, failure or "transport")
            return None
        body = _json_or_none(response)
        body = body if isinstance(body, dict) else {}
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        self.record(key, label, response.status_code, [
            *response_fields(response),
            ("bearer", isinstance(body.get("token_type"), str) and body["token_type"].lower() == "bearer"),
            ("expires_in", bucket_seconds(body.get("expires_in"))),
            ("refresh_token", isinstance(refresh, str) and bool(refresh)),
            ("scope", safe_scopes(body.get("scope"))),
        ])
        if response.status_code != 200 or not isinstance(access, str) or not access:
            return None
        return Token(access, refresh if isinstance(refresh, str) and refresh else None)

    def sign_in(self, discovery: Discovery, *, open_browser: Callable[[str], bool],
                callback_factory: Callable[[], Callback], show_url: Callable[[str], None] | None,
                timeout: float) -> tuple[Token | None, Client | None]:
        meta = discovery.metadata or {}
        if not is_https(meta.get("authorization_endpoint")):
            self.record("q1.sign_in", "Q1 sign-in", "insecure_url")
            return None, None
        callback = callback_factory()
        try:
            client = self.register(discovery, callback.redirect_uri)
            if client is None:
                return None, None
            verifier, challenge = pkce_pair()
            state = secrets.token_urlsafe(24)
            query = {
                "response_type": "code",
                "client_id": client.client_id,
                "redirect_uri": callback.redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "resource": discovery.parameter,
            }
            if discovery.scope:
                query["scope"] = discovery.scope
            separator = "&" if "?" in meta["authorization_endpoint"] else "?"
            url = f"{meta['authorization_endpoint']}{separator}{urlencode(query)}"
            sent = ("resource_sent", discovery.sent)
            if not open_browser(url):
                if show_url is None:
                    self.record("q1.sign_in", "Q1 sign-in", "no_browser", [sent])
                    return None, client
                show_url(url)
            started = time.monotonic()
            received = callback.wait(timeout)
            elapsed = "<=1m" if time.monotonic() - started <= 60 else ">1m"
            if received is None:
                self.record("q1.sign_in", "Q1 sign-in", "timeout", [sent])
                return None, client
            state_ok = secrets.compare_digest(received.get("state", ""), state)
            fields: list[tuple[str, object]] = [
                ("state_matches", state_ok), ("code", bool(received.get("code"))),
                ("error", safe_code(received.get("error"))), ("duration", elapsed),
            ]
            if "iss" in received:
                fields.append(("iss_matches", discovery.issuer is not None and received["iss"].rstrip("/") == discovery.issuer.rstrip("/")))
            fields += [*callback.shape(), sent]
            outcome = self.record("q1.sign_in", "Q1 sign-in", "callback", fields)
            if not state_ok or not received.get("code") or outcome.get("error"):
                return None, client
            token = self.token_request("q1.token", "Q1 code exchange", discovery, client, {
                "grant_type": "authorization_code",
                "code": received["code"],
                "redirect_uri": callback.redirect_uri,
                "code_verifier": verifier,
            })
            return token, client
        finally:
            callback.close()


def _discover_fields(message: dict[str, Any] | None) -> list[tuple[str, object]]:
    result = message.get("result") if isinstance(message, dict) else None
    versions = result.get("supportedVersions") if isinstance(result, dict) else None
    return [("modern_supported", isinstance(versions, list) and MODERN in versions),
            ("result_type", isinstance(result, dict) and result.get("resultType") == "complete")]


def _tools_fields(message: dict[str, Any] | None) -> list[tuple[str, object]]:
    result = message.get("result") if isinstance(message, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    names = {tool.get("name") for tool in tools if isinstance(tool, dict)} if isinstance(tools, list) else set()
    return [("tools", len(names)), ("expected_tools", names == EXPECTED_TOOLS)]


def _initialize_fields(message: dict[str, Any] | None) -> list[tuple[str, object]]:
    result = message.get("result") if isinstance(message, dict) else None
    version = result.get("protocolVersion") if isinstance(result, dict) else None
    return [("protocol_version", version if version in KNOWN_VERSIONS else "other")]


def run_live(
    base_url: str,
    *,
    client: Any = None,
    emit: Callable[[Outcome], None] = lambda outcome: print(outcome.line(), flush=True),
    open_browser: Callable[[str], bool] = webbrowser.open,
    callback_factory: Callable[[], Callback] = LoopbackCallback,
    show_url: Callable[[str], None] | None = None,
    check_mistral_api: bool = False,
    revocation_step: bool = False,
    resource_from_metadata: bool = False,
    pause: Callable[[], object] = input,
    sign_in_seconds: float = SIGN_IN_SECONDS,
) -> list[Outcome]:
    owned = client is None
    if owned:
        import httpx

        client = httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
                              headers={"User-Agent": USER_AGENT})
    try:
        probe = Probe(client, base_url, emit)
        _run_matrix(probe, open_browser=open_browser, callback_factory=callback_factory, show_url=show_url,
                    check_mistral_api=check_mistral_api, revocation_step=revocation_step,
                    resource_from_metadata=resource_from_metadata, pause=pause, sign_in_seconds=sign_in_seconds)
        verdict = evaluate(probe.outcomes)
        probe.record("verdict", "verdict", verdict.status, verdict.fields)
        return probe.outcomes
    finally:
        if owned:
            client.close()


def _run_matrix(probe: Probe, *, open_browser: Callable[[str], bool], callback_factory: Callable[[], Callback],
                show_url: Callable[[str], None] | None, check_mistral_api: bool, revocation_step: bool,
                resource_from_metadata: bool, pause: Callable[[], object], sign_in_seconds: float) -> None:
    base = probe.base
    probe.plain("q2.baseline", "Q2 baseline GET /api/health, no credential", "GET", f"{base}{HEALTH_PATH}")
    discovery = probe.discover(PRIMARY_ROUTE, "q1")
    probe.discover(NESTED_ROUTE, "q7")
    meta = discovery.metadata or {}
    methods = meta.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        probe.record("stop", "stop", "no_s256_authorization_server")
        return
    if resource_from_metadata:
        if discovery.advertised_resource is None:  # before registering, so no client is created for nothing
            probe.record("stop", "stop", "no_advertised_resource")
            return
        discovery.use_advertised = True
    token, oauth_client = probe.sign_in(discovery, open_browser=open_browser, callback_factory=callback_factory,
                                        show_url=show_url, timeout=sign_in_seconds)
    if token is None:
        signed = next((outcome for outcome in probe.outcomes if outcome.key == "q1.sign_in"), None)
        probe.record("stop", "stop", "sign_in_error" if signed is not None and signed.get("error") else "no_token")
        return

    probe.mcp("q9.discover", "Q9 modern server/discover", PRIMARY_ROUTE, MODERN, "server/discover", token=token,
              extra=_discover_fields)
    probe.mcp("q8.tools", "Q8 modern tools/list", PRIMARY_ROUTE, MODERN, "tools/list", token=token, extra=_tools_fields)
    probe.mcp("q4.whoami", "Q4 probe_whoami on /mcp", PRIMARY_ROUTE, MODERN, "tools/call", token=token,
              params={"name": "probe_whoami", "arguments": {}}, name="probe_whoami", extra=tool_report)
    before = probe.mcp("q2.before", "Q2 counter before", PRIMARY_ROUTE, MODERN, "tools/call", token=token,
                       params={"name": "probe_counter", "arguments": {}}, name="probe_counter", extra=tool_report)
    probe.mcp("q9.sse", "Q9 probe_sse", PRIMARY_ROUTE, MODERN, "tools/call", token=token,
              params={"name": "probe_sse", "arguments": {}}, name="probe_sse", extra=tool_report)
    probe.mcp("q9.mismatch", "Q9 header mismatch", PRIMARY_ROUTE, MODERN, "tools/list", token=token,
              meta_version=HANDSHAKE)
    probe.mcp("q9.unsupported", "Q9 unsupported version", PRIMARY_ROUTE, MODERN, "tools/list", token=token,
              version_header="2099-01-01", meta_version="2099-01-01")
    probe.plain("q9.get", "Q9 GET /mcp", "GET", f"{base}{PRIMARY_ROUTE}",
                headers={"Authorization": f"Bearer {token.access}", "Accept": "text/event-stream"})
    probe.mcp("q8.initialize", "Q8 handshake initialize", PRIMARY_ROUTE, HANDSHAKE, "initialize", token=token,
              params={"protocolVersion": HANDSHAKE, "capabilities": {},
                      "clientInfo": {"name": USER_AGENT, "version": "1"}}, extra=_initialize_fields)
    probe.mcp("q8.initialized", "Q8 handshake notifications/initialized", PRIMARY_ROUTE, HANDSHAKE,
              "notifications/initialized", token=token, notification=True, version_header=HANDSHAKE)
    probe.mcp("q8.handshake_tools", "Q8 handshake tools/list", PRIMARY_ROUTE, HANDSHAKE, "tools/list", token=token,
              version_header=HANDSHAKE, extra=_tools_fields)

    bearer = {"Authorization": f"Bearer {token.access}"}
    probe.plain("q2.write", "Q2 POST /api/probe/write, MCP token", "POST", f"{base}{WRITE_PATH}",
                headers={**bearer, "Content-Type": "application/json"}, content=b"{}")
    probe.plain("q2.whoami", "Q2 GET /api/probe/whoami, MCP token", "GET", f"{base}{WHOAMI_PATH}", headers=bearer)
    probe.plain("q2.health", "Q2 GET /api/health, MCP token", "GET", f"{base}{HEALTH_PATH}", headers=bearer)
    after = probe.mcp("q2.after", "Q2 counter after", PRIMARY_ROUTE, MODERN, "tools/call", token=token,
                      params={"name": "probe_counter", "arguments": {}}, name="probe_counter", extra=tool_report)
    probe.record("q2.effect", "Q2 write effect", "compared", write_effect(before, after))

    probe.mcp("q7.tools", "Q7 tools/list on /api/mcp, /mcp token", NESTED_ROUTE, MODERN, "tools/list", token=token,
              extra=_tools_fields)
    probe.mcp("q7.whoami", "Q7 probe_whoami on /api/mcp, /mcp token", NESTED_ROUTE, MODERN, "tools/call",
              token=token, params={"name": "probe_whoami", "arguments": {}}, name="probe_whoami", extra=tool_report)

    if check_mistral_api:
        probe.plain("q3.mistral_api", "Q3 Mistral API GET /v1/models, MCP token", "GET", MISTRAL_API_PROBE,
                    headers={**bearer, "Accept": "application/json"})
    else:
        probe.record("q3.mistral_api", "Q3 Mistral API GET /v1/models, MCP token", "skipped")

    current = token
    if token.refresh and oauth_client is not None:
        refreshed = probe.token_request("q6.refresh", "Q6 refresh grant", discovery, oauth_client,
                                        {"grant_type": "refresh_token", "refresh_token": token.refresh})
        if refreshed is not None:
            current = refreshed
            probe.mcp("q6.refreshed_call", "Q6 tools/list with the refreshed token", PRIMARY_ROUTE, MODERN,
                      "tools/list", token=refreshed, extra=_tools_fields)
    else:
        probe.record("q6.refresh", "Q6 refresh grant", "no_refresh_token")

    if not revocation_step:
        probe.record("q6.revoked", "Q6 call after revocation", "skipped")
        return
    print("Revoke the probe's access now as the runbook describes, then press Enter.", file=sys.stderr, flush=True)
    pause()
    probe.mcp("q6.revoked", "Q6 call after revocation", PRIMARY_ROUTE, MODERN, "tools/list", token=current,
              extra=_tools_fields)
    if token is not current:
        probe.mcp("q6.revoked_original", "Q6 original token after revocation", PRIMARY_ROUTE, MODERN, "tools/list",
                  token=token, extra=_tools_fields)
    refresh = current.refresh or token.refresh
    if refresh and oauth_client is not None:
        probe.token_request("q6.revoked_refresh", "Q6 refresh after revocation", discovery, oauth_client,
                            {"grant_type": "refresh_token", "refresh_token": refresh})


def write_effect(before: Outcome, after: Outcome) -> list[tuple[str, object]]:
    first, second = before.get("count"), after.get("count")
    same = before.get("instance") is not None and before.get("instance") == after.get("instance")
    if not (isinstance(first, int) and isinstance(second, int)):
        return [("same_instance", same), ("write_took_effect", "unknown")]
    return [("same_instance", same), ("write_took_effect", second > first if same else "unknown")]


# --- verdict -----------------------------------------------------------------------------------


def _ok(status: object) -> bool:
    return isinstance(status, int) and 200 <= status < 300


def _rejected(status: object) -> bool:
    return status in (401, 403)


def evaluate(outcomes: Iterable[Outcome]) -> Outcome:
    """GO-CANDIDATE only if every automated GO condition of #157 holds. The owner's fingerprint
    comparison and the manual checks of the runbook still decide the final GO."""
    by_key = {outcome.key: outcome for outcome in outcomes}
    blocked: list[str] = []
    incomplete: list[str] = []

    def outcome(key: str) -> Outcome | None:
        return by_key.get(key)

    authorization = outcome("q1.as")
    if authorization is None or authorization.get("s256") is None:
        incomplete.append("no_authorization_server")
    elif authorization.get("s256") is not True:
        blocked.append("no_s256")
    if "stop" in by_key:
        incomplete.append(str(by_key["stop"].status))

    write = outcome("q2.write")
    if write is None:
        incomplete.append("write_route_not_checked")
    elif _ok(write.status):
        blocked.append("write_route_accepted")
    elif not _rejected(write.status):
        incomplete.append("write_route_unclear")
    effect = outcome("q2.effect")
    if effect is not None and effect.get("write_took_effect") is True:
        blocked.append("write_took_effect")
    for key, reason in (("q2.whoami", "unflagged_read_accepted"), ("q2.health", "unflagged_read_accepted")):
        found = outcome(key)
        if found is not None and _ok(found.status) and reason not in blocked:
            blocked.append(reason)

    mistral = outcome("q3.mistral_api")
    if mistral is None or mistral.status == "skipped":
        incomplete.append("mistral_api_not_checked")
    elif _ok(mistral.status):
        blocked.append("token_accepted_by_mistral_api")
    elif not _rejected(mistral.status):
        incomplete.append("mistral_api_unclear")

    whoami = outcome("q4.whoami")
    if whoami is None:
        incomplete.append("identity_not_checked")
    elif whoami.get("identity") == "api_key":
        blocked.append("api_key_identity")
    elif whoami.get("identity") != "user":
        blocked.append("owner_not_verifiable")

    revoked = outcome("q6.revoked")
    if revoked is None or revoked.status == "skipped":
        incomplete.append("revocation_not_checked")
    elif _ok(revoked.status):
        blocked.append("revocation_not_effective")
    elif not _rejected(revoked.status):
        incomplete.append("revocation_unclear")
    for key in ("q6.revoked_original", "q6.revoked_refresh"):
        found = outcome(key)
        if found is not None and _ok(found.status):
            blocked.append("revocation_not_effective")

    if blocked:
        return Outcome("verdict", "verdict", "BLOCKED", (("reasons", tuple(dict.fromkeys(blocked))),))
    if incomplete:
        return Outcome("verdict", "verdict", "INCOMPLETE", (("reasons", tuple(dict.fromkeys(incomplete))),))
    return Outcome("verdict", "verdict", "GO-CANDIDATE", (("reasons", ("owner_fingerprint_check_pending",)),))


# --- command line ------------------------------------------------------------------------------


class _QuietParser(argparse.ArgumentParser):
    """Reports a fixed usage error; argparse would echo argument values, which can be URLs or tokens."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, "invalid arguments; see --help\n")


def valid_base_url(value: str) -> bool:
    if not is_https(value):
        return False
    parts = urlsplit(value)
    return not parts.query and not parts.fragment and parts.path in {"", "/"}


def main(argv: list[str] | None = None) -> int:
    parser = _QuietParser(description="Probe the Studio MCP-route sign-in against the synthetic probe app")
    parser.add_argument("--live", action="store_true", help="run the matrix against the probe app (needs approval)")
    parser.add_argument("--q3-mistral-api", action="store_true",
                        help="also send the token once to the Mistral API (GET /v1/models) to check its audience")
    parser.add_argument("--revocation-step", action="store_true",
                        help="pause for the owner to revoke access, then check that calls and refresh fail")
    parser.add_argument("--show-sign-in-url", action="store_true",
                        help="print the sign-in URL to stderr if no browser opens; never copy it into a log")
    parser.add_argument("--resource-from-metadata", action="store_true",
                        help="send the resource that the /mcp metadata names instead of <app>/mcp in the sign-in "
                             "and token requests; for a deliberate diagnostic attempt only")
    args = parser.parse_args(argv)
    if not args.live:
        print("Offline. Cases that --live would run against the probe app:")
        for case in CASES:
            print(f"  {case}")
        return 0
    base_url = os.environ.get(URL_VARIABLE, "")
    if not valid_base_url(base_url):
        print(f"{URL_VARIABLE} must hold the probe app's https base URL, without path or query", file=sys.stderr)
        return 2
    show_url = (lambda url: print(f"sign-in URL (do not log): {url}", file=sys.stderr, flush=True)) \
        if args.show_sign_in_url else None
    try:
        run_live(base_url, check_mistral_api=args.q3_mistral_api, revocation_step=args.revocation_step,
                 resource_from_metadata=args.resource_from_metadata, show_url=show_url)
    except Exception as exc:  # noqa: BLE001 - exception texts can carry the URL or a token
        print(f"probe_failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
