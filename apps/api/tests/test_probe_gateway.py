"""tools/gateway_access_probe.py stays offline by default and never prints values (#34)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[3] / "tools"
SECRET = "test-only-0123456789"
PRIVATE_TEXT = "Private Notiz an someone@example.org"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


gateway = load("gateway_access_probe")


def test_offline_by_default_sends_nothing(monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(gateway, "run_live", lambda *a, **k: pytest.fail("offline run reached the network"))
    assert gateway.main([]) == 0
    assert "Offline" in capsys.readouterr().out


def test_gateway_probe_reads_only() -> None:
    v1_reads = {"/api/v1/settings/agent-access", "/api/v1/audio/capabilities"}
    assert all(
        path.startswith(("/api/health", "/api/agent-read/", "/.well-known/")) or path in v1_reads
        for _, path, _ in gateway.CASES
    )
    assert {path for _, path, _ in gateway.CASES} >= v1_reads


def test_gateway_redaction_recognizes_the_caller_gate_codes() -> None:
    for code in ("agent_operation_refused", "caller_identity_unavailable"):
        assert gateway.redact("case", 403, {"detail": code}, []).error_code == code


def test_gateway_redaction_keeps_codes_and_header_names_only() -> None:
    outcome = gateway.redact("case", 401, {"error": "session_expired", "message": PRIVATE_TEXT,
                                          "token": SECRET}, ["Set-Cookie", "X-Request-Id", "Content-Type"])
    line = outcome.line()
    assert line == "case: 401 error=session_expired headers=set-cookie"
    assert SECRET not in line and "Notiz" not in line


def test_gateway_redaction_drops_free_text_error_fields() -> None:
    outcome = gateway.redact("case", 403, {"error": PRIVATE_TEXT}, [])
    assert outcome.error_code == "other" and "Notiz" not in outcome.line()


def test_gateway_redaction_recognizes_agent_access_disabled_detail() -> None:
    outcome = gateway.redact(
        "read facade, workspace key as x-api-key",
        403,
        {"detail": "agent_access_disabled"},
        ["Cache-Control"],
    )
    assert outcome.line() == "read facade, workspace key as x-api-key: 403 error=agent_access_disabled headers=-"
    unknown = gateway.redact("case", 403, {"detail": PRIVATE_TEXT}, [])
    assert unknown.error_code == "other" and "Notiz" not in unknown.line()


def test_gateway_live_requires_https_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIAM_E_APP_URL", raising=False)
    monkeypatch.setattr(gateway, "run_live", lambda *a, **k: pytest.fail("ran without a URL"))
    assert gateway.main(["--live"]) == 2


def test_gateway_redaction_reports_unknown_codes_as_other() -> None:
    outcome = gateway.redact("case", 401, {"error": "synthetic_secret"}, [])
    assert outcome.error_code == "other"


def test_gateway_live_failure_prints_no_exception_text(monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LIAM_E_APP_URL", "https://app.example.org:synthetic_secret")

    def boom(base_url: str) -> list:
        raise ValueError(f"bad url {base_url} token {SECRET}")

    monkeypatch.setattr(gateway, "run_live", boom)
    assert gateway.main(["--live"]) == 1
    out = capsys.readouterr()
    assert "probe_failed: ValueError" in out.err
    for text in (out.out, out.err):
        assert SECRET not in text and "synthetic_secret" not in text and "example.org" not in text


@pytest.mark.parametrize("argv", [["--live", "--bogus", "https://private.example.org/secret-test-only"],
                                  ["https://private.example.org/secret-test-only"]])
def test_gateway_argument_errors_do_not_echo_values(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        gateway.main(argv)
    assert exit_info.value.code == 2
    out = capsys.readouterr()
    for text in (out.out, out.err):
        assert "private.example.org" not in text and "secret-test-only" not in text
