"""tools/mcp_route_probe.py stays offline by default, signs in correctly and never prints values (#157).

The end-to-end tests run the whole live matrix against a fake gateway (``httpx.MockTransport``)
that plays the Studio authorization server and forwards authorized calls to the real probe app.
Every secret the fake hands out is distinctive, so the redaction checks can look for each of them.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS / "probes" / "mcp_route_app"))

from probe_app import identity  # noqa: E402
from probe_app.main import create_app  # noqa: E402


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


probe = load("mcp_route_probe")

BASE = "https://probe-host-secret3.example.test"
ISSUER = "https://auth-host-secret4.example.test"
PROXY = "https://gateway.example.test/v1/proxy"
ACCESS = "access-token-SECRET1-test-only"
REFRESHED = "access-token-SECRET6-test-only"
REFRESH = "refresh-token-SECRET7-test-only"
CLIENT_ID = "client-id-SECRET5-test-only"
CODE = "auth-code-SECRET8-test-only"
USER_ID = "user-id-SECRET2-test-only"
WORKSPACE_ID = "workspace-id-SECRET9-test-only"
FORWARDED = "gateway-forwarded-SECRET10-test-only"
SECRETS = (BASE, ISSUER, "probe-host-secret3", "auth-host-secret4", ACCESS, REFRESHED, REFRESH, CLIENT_ID, CODE,
           USER_ID, WORKSPACE_ID, FORWARDED, "SECRET")


@dataclass
class FakeGateway:
    """The Studio gateway and its authorization server, as #157 hopes to find them."""

    leak_unflagged: bool = False
    ignore_revocation: bool = False
    s256: bool = True
    revoked: bool = False
    prm_resource: str | None = None  # the resource the metadata names; None means the route's own URL
    token_resource: str = f"{BASE}/mcp"  # the resource the authorization server expects
    echo: str | None = None  # "same" or "slash": the registration answer repeats the redirect URIs
    challenge: dict[str, str] = field(default_factory=dict)
    token_forms: list[dict[str, list[str]]] = field(default_factory=list)
    mistral_api_calls: int = 0
    # The kill-switch test (L9): ``deleted`` is set when the probe app is deleted. ``after_delete``
    # says what the gateway does then: "gone" (routes 404, refresh rejected), "ignored" (nothing
    # changes), "refresh_only" (routes gone, refresh still works), "cached" (like "ignored" until
    # ``cache_expired``, then "gone"), "unreachable" (every request fails to connect) or
    # "server_error" (503 everywhere).
    deleted: bool = False
    after_delete: str = "gone"
    cache_expired: bool = False

    def __post_init__(self) -> None:
        async def lookup(proxy: str, token: str) -> identity.Identity:
            assert proxy == PROXY and token == FORWARDED
            return identity.Identity("user", USER_ID, WORKSPACE_ID)

        self.app = TestClient(create_app(lookup=lookup), base_url=BASE)

    def routes_gone(self) -> bool:
        return self.deleted and (self.after_delete in ("gone", "refresh_only")
                                 or (self.after_delete == "cached" and self.cache_expired))

    def grant_gone(self) -> bool:
        return self.deleted and (self.after_delete == "gone" or (self.after_delete == "cached" and self.cache_expired))

    def valid(self, request: httpx.Request) -> bool:
        tokens = {ACCESS, REFRESHED}
        authorization = request.headers.get("authorization", "")
        return authorization.removeprefix("Bearer ") in tokens and (not self.revoked or self.ignore_revocation)

    def forward(self, request: httpx.Request) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in {"authorization", "host", "content-length"}}
        headers["x-apps-token"] = FORWARDED
        answer = self.app.request(request.method, request.url.path, headers=headers, content=request.content)
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if self.deleted and self.after_delete == "unreachable":
            raise httpx.ConnectError("synthetic: the deleted app does not answer", request=request)
        if self.deleted and self.after_delete == "server_error":
            return httpx.Response(503, json={"error": "unavailable"})
        if self.routes_gone() and path in ("/mcp", "/api/mcp"):
            return httpx.Response(404, json={"error": "not_found"})
        if url == probe.MISTRAL_API_PROBE:
            self.mistral_api_calls += 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        if url.startswith(ISSUER):
            return self.authorization_server(request, path)
        if path.startswith("/.well-known/oauth-protected-resource"):
            route = path.removeprefix("/.well-known/oauth-protected-resource") or "/"
            resource = self.prm_resource if self.prm_resource is not None else f"{BASE}{route}"
            return httpx.Response(200, json={"resource": resource, "authorization_servers": [ISSUER],
                                             "scopes_supported": ["mcp"], "bearer_methods_supported": ["header"]})
        if path in ("/mcp", "/api/mcp"):
            if self.valid(request):
                return self.forward(request)
            metadata = f"{BASE}/.well-known/oauth-protected-resource{path}"
            return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}", scope="mcp"'})
        if path.startswith("/api/"):
            if self.leak_unflagged and self.valid(request):
                return self.forward(request)
            return httpx.Response(401, json={"error": "session_expired"})
        return httpx.Response(404)

    def authorization_server(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "registration_endpoint": f"{ISSUER}/oauth/register",
                "revocation_endpoint": f"{ISSUER}/oauth/revoke",
                "code_challenge_methods_supported": ["S256"] if self.s256 else ["plain"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["mcp", "workspace-" + USER_ID],
            })
        if path == "/oauth/register":
            body = json.loads(request.content)
            assert body["token_endpoint_auth_method"] == "none" and body["redirect_uris"][0].startswith("http://127.0.0.1:")
            answer: dict[str, object] = {"client_id": CLIENT_ID}
            if self.echo == "same":
                answer["redirect_uris"] = body["redirect_uris"]
            elif self.echo == "slash":
                answer["redirect_uris"] = [f"{uri}/" for uri in body["redirect_uris"]]
            return httpx.Response(201, json=answer)
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            if form["grant_type"] == ["refresh_token"]:
                if self.grant_gone() or (self.revoked and not self.ignore_revocation):
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": USER_ID})
                return httpx.Response(200, json={"access_token": REFRESHED, "token_type": "Bearer", "expires_in": 3600})
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            ok = (form["code"] == [CODE] and form["client_id"] == [CLIENT_ID] and digest == self.challenge["code_challenge"]
                  and form["resource"] == [self.token_resource] and form["redirect_uri"] == [self.challenge["redirect_uri"]])
            if not ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": ACCESS, "token_type": "Bearer", "expires_in": 3600,
                                             "refresh_token": REFRESH, "scope": "mcp"})
        return httpx.Response(404)


class FakeCallback:
    redirect_uri = "http://127.0.0.1:9/callback"

    def __init__(self, gateway: FakeGateway, *, state_override: str | None = None, error: str | None = None) -> None:
        self.gateway = gateway
        self.state_override = state_override
        self.error = error

    def wait(self, timeout: float) -> dict[str, str] | None:
        state = self.state_override or self.gateway.challenge["state"]
        if self.error is not None:
            return {"error": self.error, "state": state}
        return {"code": CODE, "state": state, "iss": ISSUER}

    def shape(self) -> tuple[tuple[str, object], ...]:
        names = ("error", "state") if self.error is not None else ("code", "iss", "state")
        return (("method", "GET"), ("path", "exact"), ("source", "query"), ("params", names))

    def close(self) -> None:
        return None


def browser_for(gateway: FakeGateway):
    def open_browser(url: str) -> bool:
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == urlsplit(ISSUER).hostname
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        assert query["resource"] == gateway.token_resource and query["client_id"] == CLIENT_ID and query["scope"] == "mcp"
        gateway.challenge = query
        return True

    return open_browser


def run(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch, *, callback: FakeCallback | None = None,
        revoke: bool = True, mistral_api: bool = True, open_browser: Callable[[str], bool] | None = None,
        callback_factory: Callable[[], object] | None = None, pause: Callable[[], None] | None = None,
        **live: object) -> tuple[list, list[str]]:
    monkeypatch.setenv("__APPS_PROXY_URL", PROXY)
    lines: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(gateway), follow_redirects=False)

    def revoke_at_pause() -> None:
        gateway.revoked = True

    outcomes = probe.run_live(BASE, client=client, emit=lambda outcome: lines.append(outcome.line()),
                              open_browser=open_browser or browser_for(gateway),
                              callback_factory=callback_factory or (lambda: callback or FakeCallback(gateway)),
                              check_mistral_api=mistral_api, revocation_step=revoke, pause=pause or revoke_at_pause,
                              **live)
    return outcomes, lines


def by_key(outcomes: list) -> dict:
    return {outcome.key: outcome for outcome in outcomes}


def assert_redacted(lines: list[str]) -> None:
    text = "\n".join(lines)
    for secret in SECRETS:
        assert secret not in text, f"output leaked a value containing {secret[:12]}..."
    assert "://" not in text and "example.test" not in text


# --- offline behaviour -------------------------------------------------------------------------


def test_offline_by_default_sends_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe, "run_live", lambda *a, **k: pytest.fail("offline run reached the network"))
    assert probe.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Offline.") and "Q2 route binding" in out


@pytest.mark.parametrize("url", ["", "http://probe.example.test", "https://probe.example.test/mcp",
                                 "https://probe.example.test/?x=1", "https://user:pw@probe.example.test", "ftp://x"])
def test_live_requires_an_https_base_url(url: str, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(probe.URL_VARIABLE, url)
    monkeypatch.setattr(probe, "run_live", lambda *a, **k: pytest.fail("ran without a valid URL"))
    assert probe.main(["--live"]) == 2
    out = capsys.readouterr()
    assert "probe.example.test" not in out.err + out.out


def test_live_failure_prints_no_exception_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(probe.URL_VARIABLE, BASE)

    def boom(*_args: object, **_kwargs: object) -> list:
        raise ValueError(f"bad url {BASE} token {ACCESS}")

    monkeypatch.setattr(probe, "run_live", boom)
    assert probe.main(["--live"]) == 1
    out = capsys.readouterr()
    assert "probe_failed: ValueError" in out.err
    assert_redacted([out.out, out.err])


@pytest.mark.parametrize("argv", [["--live", "--bogus", f"{BASE}/secret-test-only"], [f"{BASE}/secret-test-only"]])
def test_argument_errors_do_not_echo_values(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        probe.main(argv)
    assert exit_info.value.code == 2
    out = capsys.readouterr()
    assert_redacted([out.out, out.err])


def test_the_probe_names_only_probe_app_routes() -> None:
    assert probe.PRIMARY_ROUTE == "/mcp" and probe.NESTED_ROUTE == "/api/mcp"
    assert all(path.startswith(("/api/health", "/api/probe/")) for path in (probe.HEALTH_PATH, probe.WHOAMI_PATH, probe.WRITE_PATH))
    assert probe.MISTRAL_API_PROBE.endswith("/v1/models"), "the Q3 check is a read-only listing"


# --- building blocks ---------------------------------------------------------------------------


def test_pkce_matches_rfc_7636_appendix_b() -> None:
    assert probe.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    verifier, challenge = probe.pkce_pair()
    assert 43 <= len(verifier) <= 128 and challenge == probe.pkce_challenge(verifier)
    assert probe.pkce_pair()[0] != verifier


def test_challenge_summary_keeps_names_only() -> None:
    header = f'Bearer resource_metadata="{BASE}/.well-known/x", scope="mcp", error="invalid_token", custom="{ACCESS}"'
    challenge = probe.parse_challenge(header)
    assert challenge is not None
    assert challenge.summary() == "bearer[error,other,resource_metadata,scope]" and challenge.error == "invalid_token"
    assert challenge.resource_metadata == f"{BASE}/.well-known/x" and BASE not in repr(challenge)
    assert probe.parse_challenge(f'Bearer error="{ACCESS}"').error == "other"


@pytest.mark.parametrize(("value", "expected"), [("mcp openid", "mcp+openid"), (["mcp", "app:" + USER_ID], "mcp+other"),
                                                 ("", "-"), (None, "-"), (["Workspace-1"], "other")])
def test_scopes_are_reduced(value: object, expected: str) -> None:
    assert probe.safe_scopes(value) == expected


def test_metadata_locations() -> None:
    assert probe.resource_metadata_urls(f"{BASE}/api/mcp") == [
        f"{BASE}/.well-known/oauth-protected-resource/api/mcp", f"{BASE}/.well-known/oauth-protected-resource"]
    assert probe.authorization_metadata_urls(f"{ISSUER}/tenant") == [
        f"{ISSUER}/.well-known/oauth-authorization-server/tenant", f"{ISSUER}/.well-known/openid-configuration/tenant",
        f"{ISSUER}/tenant/.well-known/openid-configuration"]
    assert probe.authorization_metadata_urls(ISSUER)[0] == f"{ISSUER}/.well-known/oauth-authorization-server"


def test_tool_report_validates_every_field() -> None:
    message = {"result": {"structuredContent": {
        "instance": "abcdef012345", "user_fingerprint": USER_ID, "identity": "admin", "count": True,
        "route": "/api/v1/mail", "token_header": "x-apps-token", "authorization_header": "yes",
        "header_names": ["x-test-only-1234", "x-apps-token", ACCESS], "unlisted": ACCESS,
    }}}
    report = dict(probe.tool_report(message))
    assert report == {"instance": "abcdef012345", "route": "other", "token_header": "x-apps-token",
                      "authorization_header": "other", "identity": "other", "user_fingerprint": "other",
                      "count": "other", "header_names": ("x-apps-token",)}
    assert probe.tool_report({"error": {"code": -32602}}) == [("result", "missing")]


def test_sse_answers_are_parsed() -> None:
    response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'event: message\ndata: {"jsonrpc": "2.0", "id": 1, "result": {}}\n\n')
    assert probe.parse_mcp(response) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_tokens_and_clients_hide_their_values() -> None:
    assert ACCESS not in repr(probe.Token(ACCESS, REFRESH)) and CLIENT_ID not in repr(probe.Client(CLIENT_ID, "x"))
    assert BASE not in repr(probe.Discovery(resource=BASE))


def assert_shape_holds_no_values(shape: tuple[tuple[str, object], ...]) -> None:
    text = repr(shape)
    for value in (CODE, USER_ID, ISSUER, "s-test-only", "SECRET", "://"):
        assert value not in text


def test_loopback_callback_takes_one_answer_and_logs_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    callback = probe.LoopbackCallback()
    try:
        assert callback.redirect_uri.startswith("http://127.0.0.1:") and callback.redirect_uri.endswith("/callback")
        with httpx.Client(trust_env=False) as client:
            answer = client.get(f"{callback.redirect_uri}?code={CODE}&state=s-test-only&error_description={USER_ID}")
            assert answer.status_code == 200 and CODE not in answer.text
            assert client.get(f"{callback.redirect_uri}?code=second").status_code == 404
        assert callback.wait(1) == {"code": CODE, "state": "s-test-only"}
        assert callback.shape() == (("method", "GET"), ("path", "exact"), ("source", "query"),
                                    ("params", ("code", "error_description", "state")))
    finally:
        callback.close()
    captured = capfd.readouterr()
    assert CODE not in captured.err + captured.out and USER_ID not in captured.err + captured.out


def test_loopback_callback_accepts_a_trailing_slash(capfd: pytest.CaptureFixture[str]) -> None:
    callback = probe.LoopbackCallback()
    try:
        with httpx.Client(trust_env=False) as client:
            answer = client.get(f"{callback.redirect_uri}/?state=s-test-only&code={CODE}&session_state={USER_ID}")
            assert answer.status_code == 200 and CODE not in answer.text
        assert callback.wait(1) == {"code": CODE, "state": "s-test-only"}
        shape = callback.shape()
        assert shape == (("method", "GET"), ("path", "trailing_slash"), ("source", "query"),
                         ("params", ("code", "session_state", "state")))
        assert_shape_holds_no_values(shape)
    finally:
        callback.close()
    captured = capfd.readouterr()
    assert CODE not in captured.err + captured.out and USER_ID not in captured.err + captured.out


def test_loopback_callback_accepts_a_form_post(capfd: pytest.CaptureFixture[str]) -> None:
    callback = probe.LoopbackCallback()
    try:
        with httpx.Client(trust_env=False) as client:
            answer = client.post(callback.redirect_uri, data={"code": CODE, "state": "s-test-only", "iss": ISSUER})
            assert answer.status_code == 200 and CODE not in answer.text
            assert client.post(callback.redirect_uri, data={"code": "second"}).status_code == 404
        assert callback.wait(1) == {"code": CODE, "state": "s-test-only", "iss": ISSUER}
        shape = callback.shape()
        assert shape == (("method", "POST"), ("path", "exact"), ("source", "form"), ("params", ("code", "iss", "state")))
        assert_shape_holds_no_values(shape)
    finally:
        callback.close()
    captured = capfd.readouterr()
    assert CODE not in captured.err + captured.out and ISSUER not in captured.err + captured.out


def test_loopback_callback_skips_the_favicon_and_reduces_odd_names() -> None:
    callback = probe.LoopbackCallback()
    try:
        origin = callback.redirect_uri.removesuffix(probe.CALLBACK_PATH)
        with httpx.Client(trust_env=False) as client:
            assert client.get(f"{origin}/favicon.ico").status_code == 404
            answer = client.get(f"{origin}/elsewhere?error=access_denied&X-Odd-{USER_ID}=1&error_description={USER_ID}")
            assert answer.status_code == 200
            assert client.get(f"{callback.redirect_uri}?code=second").status_code == 404
        assert callback.wait(1) == {"error": "access_denied"}
        shape = callback.shape()
        assert shape == (("method", "GET"), ("path", "other"), ("source", "query"),
                         ("params", ("error", "error_description", "other")))
        assert_shape_holds_no_values(shape)
    finally:
        callback.close()


def test_loopback_callback_without_an_answer_has_no_shape() -> None:
    callback = probe.LoopbackCallback()
    try:
        assert callback.wait(0.01) is None and callback.shape() == ()
    finally:
        callback.close()


@pytest.mark.parametrize(("advertised", "expected", "relation"), [
    (f"{BASE}/mcp", f"{BASE}/mcp", "equal"),
    (f"{BASE}/mcp/", f"{BASE}/mcp", "equal"),
    (f"{BASE.replace('probe-host', 'PROBE-HOST')}/mcp", f"{BASE}/mcp", "equal"),
    (f"{BASE}:443/mcp", f"{BASE}/mcp", "equal"),
    (BASE, f"{BASE}/mcp", "origin"),
    (f"{BASE}/", f"{BASE}/api/mcp", "origin"),
    (f"{BASE}/api", f"{BASE}/api/mcp", "parent"),
    (f"{BASE}/api/", f"{BASE}/api/mcp", "parent"),
    (f"{BASE}/mc", f"{BASE}/mcp", "other_path"),
    (f"{BASE}/mcp/extra", f"{BASE}/mcp", "other_path"),
    (f"{BASE}/api/mcp", f"{BASE}/mcp", "other_path"),
    (f"{BASE}/mcp?x=1", f"{BASE}/mcp", "other_path"),
    ("https://other.example.test/mcp", f"{BASE}/mcp", "other_host"),
    (f"{BASE}:8443/mcp", f"{BASE}/mcp", "other_host"),
    (BASE.replace("https:", "http:") + "/mcp", f"{BASE}/mcp", "other_host"),
    ("urn:test-only", f"{BASE}/mcp", "other_host"),
    (f"{BASE}:bad/mcp", f"{BASE}/mcp", "other_host"),
    ("", f"{BASE}/mcp", "absent"),
    (None, f"{BASE}/mcp", "absent"),
    (["x"], f"{BASE}/mcp", "absent"),
])
def test_resource_relation(advertised: object, expected: str, relation: str) -> None:
    assert probe.resource_relation(advertised, expected) == relation


REDIRECT = "http://127.0.0.1:9/callback"


@pytest.mark.parametrize(("body", "echoed"), [
    ({"client_id": CLIENT_ID, "redirect_uris": [REDIRECT]}, "yes"),
    ({"redirect_uris": ["http://127.0.0.1:9/other", REDIRECT]}, "yes"),
    ({"redirect_uris": [f"{REDIRECT}/"]}, "no"),
    ({"redirect_uris": ["http://127.0.0.1/callback"]}, "no"),
    ({"redirect_uris": ["http://localhost:9/callback"]}, "no"),
    ({"redirect_uris": []}, "no"),
    ({"redirect_uris": REDIRECT}, "absent"),
    ({"client_id": CLIENT_ID}, "absent"),
    (None, "absent"),
    ([REDIRECT], "absent"),
])
def test_registration_redirect_echo(body: object, echoed: str) -> None:
    assert probe.redirect_echoed(body, REDIRECT) == echoed


# --- the matrix against a fake gateway ---------------------------------------------------------


def test_full_run_against_a_well_behaved_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    outcomes, lines = run(gateway, monkeypatch)
    results = by_key(outcomes)
    assert results["verdict"].status == "GO-CANDIDATE", lines
    assert results["q1.as"].get("s256") is True and results["q1.as"].get("registration_endpoint") is True
    assert results["q1.prm"].get("resource_relation") == "equal" and results["q1.challenge"].status == 401
    assert results["q7.prm"].get("resource_relation") == "equal"
    assert results["q1.register"].get("registration_redirect_echoed") == "absent"
    assert results["q1.sign_in"].get("state_matches") is True and results["q1.sign_in"].get("iss_matches") is True
    assert results["q1.sign_in"].get("path") == "exact" and results["q1.sign_in"].get("method") == "GET"
    assert results["q1.sign_in"].get("params") == ("code", "iss", "state")
    assert results["q1.sign_in"].get("resource_sent") == "route"
    assert results["q1.token"].get("refresh_token") is True and results["q1.token"].get("expires_in") == "<=1h"
    assert results["q4.whoami"].get("identity") == "user" and results["q4.whoami"].get("token_header") == "x-apps-token"
    assert results["q4.whoami"].get("authorization_header") is False
    assert results["q8.tools"].get("expected_tools") is True and results["q8.handshake_tools"].get("tools") == 3
    assert results["q8.initialize"].get("protocol_version") == "2025-11-25" and results["q8.initialized"].status == 202
    assert results["q9.discover"].get("modern_supported") is True and results["q9.sse"].get("content") == "sse"
    assert results["q9.mismatch"].status == 400 and results["q9.mismatch"].get("rpc_error") == -32020
    assert results["q9.unsupported"].get("rpc_error") == -32022 and results["q9.get"].status == 405
    assert results["q2.write"].status == 401 and results["q2.effect"].get("write_took_effect") is False
    assert results["q7.whoami"].get("route") == "/api/mcp" and results["q3.mistral_api"].status == 401
    assert results["q6.refresh"].status == 200 and results["q6.revoked"].status == 401
    assert results["q6.revoked_refresh"].get("error") == "invalid_grant"
    assert gateway.mistral_api_calls == 1
    assert all(form["resource"] == [f"{BASE}/mcp"] for form in gateway.token_forms)
    assert_redacted(lines)


def test_a_token_that_reaches_the_write_route_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes, lines = run(FakeGateway(leak_unflagged=True), monkeypatch)
    results = by_key(outcomes)
    assert results["q2.write"].status == 200 and results["q2.effect"].get("write_took_effect") is True
    assert results["verdict"].status == "BLOCKED"
    assert {"write_route_accepted", "write_took_effect", "unflagged_read_accepted"} <= set(results["verdict"].get("reasons"))
    assert_redacted(lines)


def test_revocation_without_effect_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes, lines = run(FakeGateway(ignore_revocation=True), monkeypatch)
    verdict = by_key(outcomes)["verdict"]
    assert verdict.status == "BLOCKED" and verdict.get("reasons") == ("revocation_not_effective",)
    assert_redacted(lines)


def test_missing_s256_stops_before_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway(s256=False)
    outcomes, lines = run(gateway, monkeypatch)
    results = by_key(outcomes)
    assert results["stop"].status == "no_s256_authorization_server" and "q1.register" not in results
    assert results["verdict"].status == "BLOCKED" and "no_s256" in results["verdict"].get("reasons")
    assert gateway.token_forms == [] and gateway.mistral_api_calls == 0
    assert_redacted(lines)


def test_a_forged_state_stops_before_the_code_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    outcomes, lines = run(gateway, monkeypatch, callback=FakeCallback(gateway, state_override="forged-test-only"))
    results = by_key(outcomes)
    assert results["q1.sign_in"].get("state_matches") is False and "q1.token" not in results
    assert results["stop"].status == "no_token" and results["verdict"].status == "INCOMPLETE"
    assert gateway.token_forms == []
    assert_redacted(lines)


@pytest.mark.parametrize(("error", "code"), [("access_denied", "access_denied"), ("invalid_target", "invalid_target"),
                                            (f"odd-{USER_ID}", "other")])
def test_an_error_redirect_stops_with_sign_in_error(error: str, code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    outcomes, lines = run(gateway, monkeypatch, callback=FakeCallback(gateway, error=error))
    results = by_key(outcomes)
    assert results["stop"].status == "sign_in_error"
    assert results["verdict"].status == "INCOMPLETE" and "sign_in_error" in results["verdict"].get("reasons")
    assert results["q1.sign_in"].status == "callback" and results["q1.sign_in"].get("error") == code
    assert results["q1.sign_in"].get("params") == ("error", "state") and "q1.token" not in results
    assert gateway.token_forms == []
    assert_redacted(lines)


@pytest.mark.parametrize(("echo", "echoed"), [("same", "yes"), ("slash", "no")])
def test_the_registration_answer_is_classified(echo: str, echoed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes, lines = run(FakeGateway(echo=echo), monkeypatch)
    assert by_key(outcomes)["q1.register"].get("registration_redirect_echoed") == echoed
    assert_redacted(lines)


@pytest.mark.parametrize(("method", "suffix", "path", "source"), [
    ("GET", "/", "trailing_slash", "query"),
    ("POST", "", "exact", "form"),
    ("POST", "/", "trailing_slash", "form"),
])
def test_a_real_callback_completes_and_no_line_holds_a_value(method: str, suffix: str, path: str, source: str,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    approve = browser_for(gateway)
    sent: list[str] = []

    def open_browser(url: str) -> bool:
        approve(url)
        values = {"code": CODE, "state": gateway.challenge["state"], "iss": ISSUER,
                  "session_state": "session-SECRET11-test-only"}
        sent.extend(values.values())
        target = gateway.challenge["redirect_uri"] + suffix
        with httpx.Client(trust_env=False) as browser:
            answer = browser.get(target, params=values) if method == "GET" else browser.post(target, data=values)
        assert answer.status_code == 200
        return True

    outcomes, lines = run(gateway, monkeypatch, open_browser=open_browser, callback_factory=probe.LoopbackCallback)
    results = by_key(outcomes)
    sign_in = results["q1.sign_in"]
    assert sign_in.get("method") == method and sign_in.get("path") == path and sign_in.get("source") == source
    assert sign_in.get("params") == ("code", "iss", "session_state", "state") and sign_in.get("state_matches") is True
    assert results["q1.token"].status == 200 and results["verdict"].status == "GO-CANDIDATE", lines
    text = "\n".join(lines)
    assert sent and all(value not in text for value in sent)
    assert_redacted(lines)


def test_resource_from_metadata_sends_the_advertised_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    advertised = f"{BASE}/"
    gateway = FakeGateway(prm_resource=advertised, token_resource=advertised)
    outcomes, lines = run(gateway, monkeypatch, resource_from_metadata=True)
    results = by_key(outcomes)
    assert results["q1.prm"].get("resource_relation") == "origin"
    assert results["q1.sign_in"].get("resource_sent") == "metadata"
    assert gateway.challenge["resource"] == advertised and len(gateway.token_forms) >= 2
    assert all(form["resource"] == [advertised] for form in gateway.token_forms)
    assert results["verdict"].status == "GO-CANDIDATE", lines
    assert_redacted(lines)


def test_by_default_the_route_is_sent_even_if_the_metadata_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway(prm_resource=f"{BASE}/")
    outcomes, lines = run(gateway, monkeypatch)
    results = by_key(outcomes)
    assert results["q1.prm"].get("resource_relation") == "origin"
    assert results["q1.sign_in"].get("resource_sent") == "route"
    assert all(form["resource"] == [f"{BASE}/mcp"] for form in gateway.token_forms)
    assert_redacted(lines)


def test_resource_from_metadata_needs_an_https_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway(prm_resource="urn:test-only")
    outcomes, lines = run(gateway, monkeypatch, resource_from_metadata=True)
    results = by_key(outcomes)
    assert results["stop"].status == "no_advertised_resource" and "q1.register" not in results
    assert "q1.sign_in" not in results and gateway.token_forms == []
    assert results["verdict"].status == "INCOMPLETE" and "no_advertised_resource" in results["verdict"].get("reasons")
    assert_redacted(lines)


def test_the_resource_flag_reaches_the_live_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(probe.URL_VARIABLE, BASE)
    seen: dict[str, object] = {}

    def fake_run_live(base_url: str, **kwargs: object) -> list:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(probe, "run_live", fake_run_live)
    assert probe.main(["--live", "--resource-from-metadata"]) == 0 and seen["resource_from_metadata"] is True
    seen.clear()
    assert probe.main(["--live"]) == 0 and seen["resource_from_metadata"] is False


def test_skipped_checks_leave_the_verdict_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    outcomes, _lines = run(gateway, monkeypatch, revoke=False, mistral_api=False)
    verdict = by_key(outcomes)["verdict"]
    assert verdict.status == "INCOMPLETE"
    assert set(verdict.get("reasons")) == {"mistral_api_not_checked", "revocation_not_checked"}
    assert gateway.mistral_api_calls == 0


@pytest.mark.parametrize(("identity_kind", "reason"), [("api_key", "api_key_identity"), ("lookup_failed", "owner_not_verifiable")])
def test_identity_without_a_user_blocks(identity_kind: str, reason: str) -> None:
    outcomes = [
        probe.Outcome("q1.as", "", 200, (("s256", True),)),
        probe.Outcome("q2.write", "", 401),
        probe.Outcome("q3.mistral_api", "", 401),
        probe.Outcome("q4.whoami", "", 200, (("identity", identity_kind),)),
        probe.Outcome("q6.revoked", "", 401),
    ]
    verdict = probe.evaluate(outcomes)
    assert verdict.status == "BLOCKED" and verdict.get("reasons") == (reason,)


def test_a_token_accepted_by_the_mistral_api_blocks() -> None:
    outcomes = [
        probe.Outcome("q1.as", "", 200, (("s256", True),)),
        probe.Outcome("q2.write", "", 403),
        probe.Outcome("q3.mistral_api", "", 200),
        probe.Outcome("q4.whoami", "", 200, (("identity", "user"),)),
        probe.Outcome("q6.revoked", "", 401),
    ]
    assert probe.evaluate(outcomes).get("reasons") == ("token_accepted_by_mistral_api",)


# --- kill switch (L9): does deleting the probe app end its tokens? -----------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def kill_run(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch, *, after_pause: Callable[[], None] | None = None,
             after_sleep: Callable[[], None] | None = None, clock: Clock | None = None,
             **options: object) -> tuple[dict, list[str], list[float]]:
    sleeps: list[float] = []

    def pause() -> None:
        gateway.deleted = True
        if after_pause is not None:
            after_pause()

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if after_sleep is not None:
            after_sleep()

    outcomes, lines = run(gateway, monkeypatch, revoke=False, mistral_api=False, pause=pause, kill_switch_step=True,
                          sleep=sleep, clock=clock or Clock(), **options)
    return by_key(outcomes), lines, sleeps


def test_kill_switch_effective_when_deletion_ends_calls_and_refresh(monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    gateway = FakeGateway()
    results, lines, sleeps = kill_run(gateway, monkeypatch)

    assert [results[f"k0.{name}"].status for name in ("call", "refresh", "refreshed_call")] == [200, 200, 200]
    for check in ("k1", "k2"):
        assert results[f"{check}.call"].status == 404 and results[f"{check}.call"].get("within_lifetime") == "yes"
        assert results[f"{check}.refresh"].status == 400
        assert f"{check}.refreshed_call" not in results
    assert sleeps == [probe.KILL_SWITCH_RECHECK_SECONDS] and probe.KILL_SWITCH_RECHECK_SECONDS == 120
    kill = results["kill_switch"]
    assert kill.status == "effective" and kill.get("immediate") == "effective"
    # The kill-switch run is not the matrix: no unflagged route, write, Mistral API or revocation call.
    assert "verdict" not in results and "q2.baseline" not in results and "q7.prm" not in results
    assert not any(key.startswith(("q2.", "q3.", "q4.", "q6.", "q8.", "q9.")) for key in results)
    assert "Delete the probe app now" in capsys.readouterr().err
    assert_redacted(lines)


def test_kill_switch_not_effective_when_the_gateway_ignores_the_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    results, lines, _ = kill_run(FakeGateway(after_delete="ignored"), monkeypatch)

    assert [results[f"k1.{name}"].status for name in ("call", "refresh", "refreshed_call")] == [200, 200, 200]
    kill = results["kill_switch"]
    assert kill.status == "not_effective" and kill.get("immediate") == "not_effective"
    assert {"call_accepted", "refresh_accepted", "refreshed_call_accepted"} <= set(kill.get("reasons"))
    assert_redacted(lines)


def test_a_live_refresh_grant_alone_means_not_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    results, _, _ = kill_run(FakeGateway(after_delete="refresh_only"), monkeypatch)

    assert results["k2.call"].status == 404 and results["k2.refresh"].status == 200
    assert results["k2.refreshed_call"].status == 404
    kill = results["kill_switch"]
    assert kill.status == "not_effective" and kill.get("reasons") == ("refresh_accepted",)


def test_a_kill_switch_that_takes_effect_during_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway(after_delete="cached")
    results, _, _ = kill_run(gateway, monkeypatch, after_sleep=lambda: setattr(gateway, "cache_expired", True))

    assert results["k1.call"].status == 200 and results["k2.call"].status == 404 and results["k2.refresh"].status == 400
    kill = results["kill_switch"]
    assert kill.status == "effective" and kill.get("immediate") == "not_effective"


def test_an_app_that_no_longer_answers_counts_as_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    results, lines, _ = kill_run(FakeGateway(after_delete="unreachable"), monkeypatch)

    assert results["k1.call"].status == "transport:ConnectError"
    assert results["k1.refresh"].status == "transport:ConnectError"
    assert results["kill_switch"].status == "effective"
    assert_redacted(lines)


def test_server_errors_leave_the_kill_switch_unclear(monkeypatch: pytest.MonkeyPatch) -> None:
    results, _, _ = kill_run(FakeGateway(after_delete="server_error"), monkeypatch)

    kill = results["kill_switch"]
    assert kill.status == "unclear" and {"call_unclear", "refresh_unclear"} <= set(kill.get("reasons"))


def test_an_expired_access_token_proves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()

    def later() -> None:
        clock.now += 4000  # past the fake's expires_in of 3600 seconds

    results, lines, _ = kill_run(FakeGateway(), monkeypatch, clock=clock, after_pause=later)

    assert results["k1.call"].status == 404 and results["k1.call"].get("within_lifetime") == "no"
    kill = results["kill_switch"]
    assert kill.status == "unclear" and "access_token_expired" in kill.get("reasons")
    assert_redacted(lines)



def test_expiry_during_the_delay_keeps_a_proven_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()

    def later() -> None:
        clock.now += 4000

    results, _, _ = kill_run(FakeGateway(), monkeypatch, clock=clock, after_sleep=later)

    assert results["k1.call"].get("within_lifetime") == "yes" and results["k2.call"].get("within_lifetime") == "no"
    kill = results["kill_switch"]
    # Round 1 proved the rejection while the token was valid; round 2 adds no contrary evidence.
    assert kill.status == "effective" and kill.get("immediate") == "effective" and kill.get("after_delay") == "unclear"

@pytest.mark.parametrize(("gateway_options", "callback_error", "reason"), [
    ({}, "access_denied", "sign_in_error"),
    ({"revoked": True}, None, "baseline_failed"),  # the token does not work even before the deletion
])
def test_no_pause_without_a_working_token(gateway_options: dict, callback_error: str | None, reason: str,
                                          monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gateway = FakeGateway(**gateway_options)
    paused: list[bool] = []
    callback = FakeCallback(gateway, error=callback_error) if callback_error else None
    results, _, sleeps = kill_run(gateway, monkeypatch, after_pause=lambda: paused.append(True), callback=callback)

    assert paused == [] and sleeps == [] and not any(key.startswith(("k1.", "k2.")) for key in results)
    assert results["stop"].status == reason
    kill = results["kill_switch"]
    assert kill.status == "unclear" and kill.get("reasons") == (reason,)
    assert "Delete the probe app" not in capsys.readouterr().err


def outcome(key: str, status: int | str, lifetime: str | None = None) -> object:
    return probe.Outcome(key, key, status, (("within_lifetime", lifetime),) if lifetime else ())


@pytest.mark.parametrize(("call", "refresh", "refreshed", "expected"), [
    (outcome("call", 401, "yes"), outcome("refresh", 400), None, ("effective", ())),
    (outcome("call", 404, "yes"), outcome("refresh", "transport:ConnectError"), None, ("effective", ())),
    (outcome("call", 200, "yes"), outcome("refresh", 400), None, ("not_effective", ("call_accepted",))),
    (outcome("call", 401, "yes"), outcome("refresh", 200), outcome("refreshed_call", 401, "yes"),
     ("not_effective", ("refresh_accepted",))),
    (outcome("call", "transport:ReadTimeout", "yes"), outcome("refresh", 400), None, ("unclear", ("call_unclear",))),
    (outcome("call", 401, "no"), outcome("refresh", 401), None, ("unclear", ("access_token_expired",))),
    (outcome("call", 401, "unknown"), outcome("refresh", 400), None, ("unclear", ("lifetime_unknown",))),
    (outcome("call", 401, "yes"), None, None, ("unclear", ("refresh_not_tried",))),
    (outcome("call", 503, "yes"), outcome("refresh", 503), None, ("unclear", ("call_unclear", "refresh_unclear"))),
])
def test_kill_round_classification(call: object, refresh: object, refreshed: object,
                                   expected: tuple[str, tuple[str, ...]]) -> None:
    assert probe.kill_round(call, refresh, refreshed) == expected


def test_the_kill_switch_flag_reaches_the_live_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(probe.URL_VARIABLE, BASE)
    seen: dict[str, object] = {}

    def fake_run_live(base_url: str, **kwargs: object) -> list:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(probe, "run_live", fake_run_live)
    assert probe.main(["--live", "--kill-switch-step", "--resource-from-metadata"]) == 0
    assert seen["kill_switch_step"] is True and seen["resource_from_metadata"] is True
    seen.clear()
    assert probe.main(["--live"]) == 0 and seen["kill_switch_step"] is False


@pytest.mark.parametrize("matrix_flag", ["--revocation-step", "--q3-mistral-api"])
def test_the_kill_switch_runs_without_matrix_flags(matrix_flag: str, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(probe.URL_VARIABLE, BASE)
    monkeypatch.setattr(probe, "run_live", lambda *a, **k: pytest.fail("ran with conflicting flags"))
    assert probe.main(["--live", "--kill-switch-step", matrix_flag]) == 2
    err = capsys.readouterr().err
    assert "--kill-switch-step" in err
    assert_redacted([err])


def test_offline_lists_the_kill_switch_mode(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(probe, "run_live", lambda *a, **k: pytest.fail("offline run reached the network"))
    assert probe.main([]) == 0
    assert "--kill-switch-step" in capsys.readouterr().out
