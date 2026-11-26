"""The synthetic MCP-route probe app (#157): manifest, packaging, identity report and MCP stub.

Everything here runs offline. The app is test-only and never part of liam-e's image; these tests
keep it from drifting into a second liam-e deployment or leaking identifiers. The deploy helper's
checks are in test_probe_mcp_route_deploy.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
PROBE_DIR = ROOT / "tools" / "probes" / "mcp_route_app"
sys.path.insert(0, str(PROBE_DIR))

from probe_app import identity, mcp_stub  # noqa: E402
from probe_app.main import create_app  # noqa: E402

BASE = "https://probe.example.test"
PROXY = "https://gateway.example.test/v1/proxy"
USER_ID = "user-test-only-4b1d"
WORKSPACE_ID = "workspace-test-only-77aa"
CALLER_TOKEN = "caller-token-test-only-0123456789"
MODERN = mcp_stub.MODERN_VERSION
META = mcp_stub.META_VERSION


async def owner_lookup(proxy: str, token: str) -> identity.Identity:
    assert proxy == PROXY and token == CALLER_TOKEN
    return identity.Identity("user", USER_ID, WORKSPACE_ID)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("__APPS_PROXY_URL", PROXY)
    monkeypatch.delenv("__SPACES_PROXY_URL", raising=False)
    return TestClient(create_app(lookup=owner_lookup), base_url=BASE)


def modern(method: str, *, name: str | None = None, request_id: int | None = 1,
           params: dict | None = None, version: str = MODERN) -> tuple[dict, dict]:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": {**(params or {}), "_meta": {META: version}}}
    if request_id is not None:
        body["id"] = request_id
    headers = {"Content-Type": "application/json", "MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
        body["params"]["name"] = name
    return body, headers


def call_tool(client: TestClient, name: str, **headers: str) -> httpx.Response:
    body, base_headers = modern("tools/call", name=name)
    return client.post("/mcp", json=body, headers={**base_headers, **headers})


# --- manifest and packaging --------------------------------------------------------------------


def manifest() -> dict:
    return json.loads((PROBE_DIR / "mistral.json").read_text(encoding="utf-8"))


def test_manifest_flags_exactly_the_mcp_routes() -> None:
    data = manifest()
    assert "deployment" not in data, "IDs are injected at deploy time by deploy.sh, never committed"
    assert data["name"] == "liam-e-mcp-route-probe" and data["name"] != "liam-e"
    (module,) = data["modules"]
    assert module["routes"] == [{"path": "/api"}, {"path": "/mcp", "mcp": True}, {"path": "/api/mcp", "mcp": True}]
    for route in module["routes"]:
        assert set(route) <= {"path", "mcp"}  # the CLI's route schema is strict


def test_manifest_uses_only_schema_keys_and_the_port_variable() -> None:
    (module,) = manifest()["modules"]
    assert set(module) <= {"type", "name", "path", "routes", "port", "serve", "runtime", "build", "platform", "dev", "deploy"}
    assert module["serve"] == "server" and module["runtime"] == "python" and module["type"] == "app"
    command = module["deploy"]["startCommand"]
    assert '"$PORT"' in command and not re.search(r"--port[=\s]*\d", command)
    assert "--no-access-log" in command


def test_no_module_outside_the_probe() -> None:
    assert manifest()["modules"][0]["build"] == {"context": ".", "dockerfile": "Dockerfile", "target": "probe"}
    assert all(module.get("type") != "managed" for module in manifest()["modules"]), "the probe needs no database"


def test_example_deployment_file_holds_placeholders_only() -> None:
    example = json.loads((PROBE_DIR / "mistral.deployment.example.json").read_text(encoding="utf-8"))
    assert example == {"organizationId": "<organization-id>", "workspaceId": "<workspace-id>"}


def test_probe_deployment_file_is_ignored_by_git_and_docker() -> None:
    path = "tools/probes/mcp_route_app/mistral.deployment.json"
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, f"{path} must be git-ignored"
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "**/mistral.deployment.json" in patterns


def lock_versions() -> dict[str, tuple[str, str]]:
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    versions = {}
    for block in text.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.M)
        version = re.search(r'^version = "([^"]+)"', block, re.M)
        if name and version:
            versions[name.group(1)] = (version.group(1), block)
    return versions


def test_requirements_match_the_root_lock_with_hashes() -> None:
    text = (PROBE_DIR / "requirements.txt").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^([a-z0-9_.-]+)==([^\s\\]+)", text, re.M))
    assert {"starlette", "httpx", "uvicorn"} <= set(pins)
    locked = lock_versions()
    for name, version in pins.items():
        assert name in locked and locked[name][0] == version, f"{name} differs from uv.lock"
    hashes = re.findall(r"--hash=(sha256:[0-9a-f]{64})", text)
    assert len(hashes) >= len(pins)
    lock_text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert all(digest in lock_text for digest in hashes), "every hash must be one uv.lock already trusts"


def test_dockerfile_installs_only_hashed_wheels() -> None:
    dockerfile = (PROBE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile and "--no-deps" in dockerfile and "--only-binary=:all:" in dockerfile
    assert "USER 1000:1000" in dockerfile


# --- unflagged routes and identity -------------------------------------------------------------


def test_health_and_counter(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    first = client.get("/api/probe/counter").json()
    written = client.post("/api/probe/write").json()
    assert written == {"instance": first["instance"], "count": first["count"] + 1}
    assert client.get("/api/probe/write").status_code == 405


def test_whoami_reports_fingerprints_never_ids(client: TestClient) -> None:
    response = client.get("/api/probe/whoami", headers={"x-apps-token": CALLER_TOKEN, "Cookie": "session=test-only"})
    report = response.json()
    assert report["identity"] == "user" and report["token_header"] == "x-apps-token" and report["route"] == "/api"
    assert report["cookie_header"] is True and report["authorization_header"] is False
    assert re.fullmatch(r"[0-9a-f]{12}", report["user_fingerprint"])
    assert re.fullmatch(r"[0-9a-f]{12}", report["workspace_fingerprint"])
    for secret in (USER_ID, WORKSPACE_ID, CALLER_TOKEN, "test-only"):
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_fingerprints_match_within_one_process_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("__APPS_PROXY_URL", PROXY)
    one = TestClient(create_app(lookup=owner_lookup), base_url=BASE)
    other = TestClient(create_app(lookup=owner_lookup), base_url=BASE)
    headers = {"x-apps-token": CALLER_TOKEN}
    browser = one.get("/api/probe/whoami", headers=headers).json()
    tool = call_tool(one, "probe_whoami", **headers).json()["result"]["structuredContent"]
    assert browser["user_fingerprint"] == tool["user_fingerprint"] and browser["instance"] == tool["instance"]
    assert other.get("/api/probe/whoami", headers=headers).json()["user_fingerprint"] != browser["user_fingerprint"]


def test_whoami_without_token_or_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("__APPS_PROXY_URL", raising=False)
    monkeypatch.delenv("__SPACES_PROXY_URL", raising=False)
    app = TestClient(create_app(lookup=owner_lookup), base_url=BASE)
    assert app.get("/api/probe/whoami").json()["identity"] == "no_token"
    assert app.get("/api/probe/whoami", headers={"x-space-token": CALLER_TOKEN}).json()["identity"] == "no_proxy"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (200, {"id": USER_ID, "api_key": None, "workspace": {"id": WORKSPACE_ID}}, "user"),
        (200, {"id": USER_ID, "api_key": {"id": "key-test-only"}}, "api_key"),
        (200, {"id": USER_ID}, "unknown"),
        (401, {"detail": "test-only"}, "lookup_rejected"),
        (403, None, "lookup_rejected"),
        (500, None, "lookup_failed"),
        (200, ["not", "an", "object"], "lookup_failed"),
    ],
)
def test_identity_classification(status: int, body: object, kind: str) -> None:
    result = identity.classify(status, body)
    assert result.kind == kind
    assert USER_ID not in repr(result)


async def test_lookup_uses_only_the_caller_token_and_caps_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    oversized = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if oversized["value"]:
            return httpx.Response(200, content=b"x" * (identity.MAX_IDENTITY_BYTES + 1))
        return httpx.Response(200, json={"id": USER_ID, "api_key": None, "workspace": {"id": WORKSPACE_ID}})

    real = httpx.AsyncClient
    monkeypatch.setattr(identity.httpx, "AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    assert (await identity.lookup_identity(PROXY, CALLER_TOKEN)).kind == "user"
    assert str(seen[0].url) == f"{PROXY}/v1/users/me"
    assert seen[0].headers["authorization"] == f"Bearer {CALLER_TOKEN}"
    oversized["value"] = True
    assert (await identity.lookup_identity(PROXY, CALLER_TOKEN)).kind == "lookup_failed"


def test_header_names_drop_identifier_like_names() -> None:
    # Test-only placeholders; the order keeps the secret scan from reading a name as a key value.
    names = ["x-test-only-1234", "X-Apps-Token", "Bad Header", "x-request-id", "x-" + "a" * 70,
             "access-token-secret1-test-only", "a-b-c-d-e-f"]
    assert identity.safe_header_names(names) == ["x-apps-token", "x-request-id"]


# --- MCP stub: transport rules -----------------------------------------------------------------


@pytest.mark.parametrize("route", ["/mcp", "/api/mcp"])
@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_stream_and_session_methods_are_not_allowed(client: TestClient, route: str, method: str) -> None:
    response = client.request(method, route, headers={"Mcp-Session-Id": "test-only", "Last-Event-ID": "1"})
    assert response.status_code == 405 and response.headers["allow"] == "POST"


def test_modern_tools_list(client: TestClient) -> None:
    body, headers = modern("tools/list")
    result = client.post("/api/mcp", json=body, headers=headers).json()["result"]
    assert result["resultType"] == "complete" and result["ttlMs"] == 0 and result["cacheScope"] == "private"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "liam-e-mcp-route-probe"
    assert {tool["name"] for tool in result["tools"]} == {"probe_whoami", "probe_counter", "probe_sse"}
    assert all(tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["destructiveHint"] is False
               for tool in result["tools"])


def test_modern_server_discover(client: TestClient) -> None:
    body, headers = modern("server/discover")
    result = client.post("/mcp", json=body, headers=headers).json()["result"]
    assert result["supportedVersions"][0] == MODERN and "2025-11-25" in result["supportedVersions"]
    assert result["capabilities"] == {"tools": {}}


def rpc_error(response: httpx.Response) -> int:
    return response.json()["error"]["code"]


def test_protocol_header_must_match_meta(client: TestClient) -> None:
    body, headers = modern("tools/list")
    body["params"]["_meta"][META] = "2025-11-25"
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400 and rpc_error(response) == mcp_stub.HEADER_MISMATCH


def test_modern_request_without_version_header_is_rejected(client: TestClient) -> None:
    body, headers = modern("tools/list")
    del headers["MCP-Protocol-Version"]
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400 and rpc_error(response) == mcp_stub.HEADER_MISMATCH


@pytest.mark.parametrize(("header", "value"), [("Mcp-Method", "ping"), ("Mcp-Name", "probe_counter")])
def test_routing_headers_must_match_the_body(client: TestClient, header: str, value: str) -> None:
    body, headers = modern("tools/call", name="probe_whoami")
    headers[header] = value
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400 and rpc_error(response) == mcp_stub.HEADER_MISMATCH


def test_unsupported_version_lists_supported_without_echoing(client: TestClient) -> None:
    body, headers = modern("tools/list", version="2099-01-01-test-only")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400 and rpc_error(response) == mcp_stub.UNSUPPORTED_VERSION
    assert response.json()["error"]["data"]["supported"][0] == MODERN
    assert "test-only" not in response.text


def test_unknown_method_and_tool(client: TestClient) -> None:
    body, headers = modern("resources/list")
    response = client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 404 and rpc_error(response) == mcp_stub.METHOD_NOT_FOUND
    response = call_tool(client, "send_email")
    assert response.status_code == 200 and rpc_error(response) == mcp_stub.INVALID_PARAMS
    handshake = client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "resources/list"},
                            headers={"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"})
    assert handshake.status_code == 200 and rpc_error(handshake) == mcp_stub.METHOD_NOT_FOUND


# --- MCP stub: handshake era -------------------------------------------------------------------


@pytest.mark.parametrize(("requested", "answered"), [("2025-11-25", "2025-11-25"), ("2025-03-26", "2025-03-26"),
                                                     ("2024-11-05", "2025-11-25")])
def test_initialize_negotiates_a_handshake_version(client: TestClient, requested: str, answered: str) -> None:
    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": requested, "capabilities": {}}},
                           headers={"Content-Type": "application/json"})
    result = response.json()["result"]
    assert result["protocolVersion"] == answered and "tools" in result["capabilities"]
    assert "mcp-session-id" not in response.headers, "the stub is stateless"


def test_handshake_notification_ping_and_tools(client: TestClient) -> None:
    headers = {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    assert note.status_code == 202 and note.content == b""
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"}, headers=headers).json()["result"] == {}
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=headers).json()
    assert "resultType" not in tools["result"] and len(tools["result"]["tools"]) == 3
    legacy = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                         headers={"Content-Type": "application/json"})
    assert legacy.status_code == 200, "a request without the header counts as 2025-03-26"


# --- MCP stub: tools ---------------------------------------------------------------------------


def test_whoami_tool_reports_the_received_headers(client: TestClient) -> None:
    structured = call_tool(client, "probe_whoami", **{"x-apps-token": CALLER_TOKEN}).json()["result"]["structuredContent"]
    assert structured["route"] == "/mcp" and structured["identity"] == "user"
    assert structured["protocol_header"] == MODERN and structured["mcp_method_header"] is True
    assert "mcp-protocol-version" in structured["header_names"]


def test_tools_never_write(client: TestClient) -> None:
    before = client.get("/api/probe/counter").json()["count"]
    for name in ("probe_whoami", "probe_counter", "probe_sse"):
        assert call_tool(client, name).status_code == 200
    assert client.get("/api/probe/counter").json()["count"] == before


def test_sse_answer_only_when_accepted(client: TestClient) -> None:
    streamed = call_tool(client, "probe_sse", Accept="application/json, text/event-stream")
    assert streamed.headers["content-type"].startswith("text/event-stream")
    data = [line for line in streamed.text.splitlines() if line.startswith("data: ")]
    assert json.loads(data[0][6:])["result"]["structuredContent"]["count"] >= 0
    plain = call_tool(client, "probe_sse", Accept="application/json")
    assert plain.headers["content-type"].startswith("application/json")


# --- MCP stub: input limits --------------------------------------------------------------------


def test_input_rules(client: TestClient) -> None:
    body, headers = modern("tools/list")
    assert client.post("/mcp", content=json.dumps(body), headers={**headers, "Content-Type": "text/plain"}).status_code == 415
    big = {**body, "params": {**body["params"], "pad": "x" * (mcp_stub.MAX_BODY_BYTES + 1)}}
    assert client.post("/mcp", json=big, headers=headers).status_code == 413
    batch = client.post("/mcp", json=[body], headers=headers)
    assert batch.status_code == 400 and rpc_error(batch) == mcp_stub.INVALID_REQUEST
    broken = client.post("/mcp", content=b"{not json", headers=headers)
    assert broken.status_code == 400 and rpc_error(broken) == mcp_stub.PARSE_ERROR


def test_foreign_origin_is_rejected(client: TestClient) -> None:
    body, headers = modern("tools/list")
    assert client.post("/mcp", json=body, headers={**headers, "Origin": "https://evil.example.test"}).status_code == 403
    assert client.post("/mcp", json=body, headers={**headers, "Origin": BASE}).status_code == 200
