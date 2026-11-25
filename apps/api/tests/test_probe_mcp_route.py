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
    challenge: dict[str, str] = field(default_factory=dict)
    token_forms: list[dict[str, list[str]]] = field(default_factory=list)
    mistral_api_calls: int = 0

    def __post_init__(self) -> None:
        async def lookup(proxy: str, token: str) -> identity.Identity:
            assert proxy == PROXY and token == FORWARDED
            return identity.Identity("user", USER_ID, WORKSPACE_ID)

        self.app = TestClient(create_app(lookup=lookup), base_url=BASE)

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
        if url == probe.MISTRAL_API_PROBE:
            self.mistral_api_calls += 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        if url.startswith(ISSUER):
            return self.authorization_server(request, path)
        if path.startswith("/.well-known/oauth-protected-resource"):
            route = path.removeprefix("/.well-known/oauth-protected-resource") or "/"
            return httpx.Response(200, json={"resource": f"{BASE}{route}", "authorization_servers": [ISSUER],
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
            return httpx.Response(201, json={"client_id": CLIENT_ID})
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            self.token_forms.append(form)
            if form["grant_type"] == ["refresh_token"]:
                if self.revoked and not self.ignore_revocation:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": USER_ID})
                return httpx.Response(200, json={"access_token": REFRESHED, "token_type": "Bearer", "expires_in": 3600})
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            ok = (form["code"] == [CODE] and form["client_id"] == [CLIENT_ID] and digest == self.challenge["code_challenge"]
                  and form["resource"] == [f"{BASE}/mcp"] and form["redirect_uri"] == [self.challenge["redirect_uri"]])
            if not ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": ACCESS, "token_type": "Bearer", "expires_in": 3600,
                                             "refresh_token": REFRESH, "scope": "mcp"})
        return httpx.Response(404)


class FakeCallback:
    redirect_uri = "http://127.0.0.1:9/callback"

    def __init__(self, gateway: FakeGateway, *, state_override: str | None = None) -> None:
        self.gateway = gateway
        self.state_override = state_override

    def wait(self, timeout: float) -> dict[str, str] | None:
        state = self.state_override or self.gateway.challenge["state"]
        return {"code": CODE, "state": state, "iss": ISSUER}

    def close(self) -> None:
        return None


def browser_for(gateway: FakeGateway):
    def open_browser(url: str) -> bool:
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == urlsplit(ISSUER).hostname
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        assert query["resource"] == f"{BASE}/mcp" and query["client_id"] == CLIENT_ID and query["scope"] == "mcp"
        gateway.challenge = query
        return True

    return open_browser


def run(gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch, *, callback: FakeCallback | None = None,
        revoke: bool = True, mistral_api: bool = True) -> tuple[list, list[str]]:
    monkeypatch.setenv("__APPS_PROXY_URL", PROXY)
    lines: list[str] = []
    client = httpx.Client(transport=httpx.MockTransport(gateway), follow_redirects=False)

    def pause() -> None:
        gateway.revoked = True

    outcomes = probe.run_live(BASE, client=client, emit=lambda outcome: lines.append(outcome.line()),
                              open_browser=browser_for(gateway), callback_factory=lambda: callback or FakeCallback(gateway),
                              check_mistral_api=mistral_api, revocation_step=revoke, pause=pause)
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


def test_loopback_callback_takes_one_answer_and_logs_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    callback = probe.LoopbackCallback()
    try:
        assert callback.redirect_uri.startswith("http://127.0.0.1:") and callback.redirect_uri.endswith("/callback")
        with httpx.Client() as client:
            assert client.get(callback.redirect_uri.replace("/callback", "/other")).status_code == 404
            answer = client.get(f"{callback.redirect_uri}?code={CODE}&state=s-test-only&error_description={USER_ID}")
            assert answer.status_code == 200 and CODE not in answer.text
            assert client.get(f"{callback.redirect_uri}?code=second").status_code == 404
        assert callback.wait(1) == {"code": CODE, "state": "s-test-only"}
    finally:
        callback.close()
    captured = capfd.readouterr()
    assert CODE not in captured.err + captured.out and USER_ID not in captured.err + captured.out


# --- the matrix against a fake gateway ---------------------------------------------------------


def test_full_run_against_a_well_behaved_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGateway()
    outcomes, lines = run(gateway, monkeypatch)
    results = by_key(outcomes)
    assert results["verdict"].status == "GO-CANDIDATE", lines
    assert results["q1.as"].get("s256") is True and results["q1.as"].get("registration_endpoint") is True
    assert results["q1.prm"].get("resource_matches") is True and results["q1.challenge"].status == 401
    assert results["q1.sign_in"].get("state_matches") is True and results["q1.sign_in"].get("iss_matches") is True
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
