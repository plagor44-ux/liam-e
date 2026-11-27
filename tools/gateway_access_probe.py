"""Which credentials get a request through the Studio app gateway (#34).

Offline by default: prints the probe plan and runs nothing. With ``--live`` it sends read-only
``GET`` requests to the deployed app and prints one redacted line per case: HTTP status, the
gateway's fixed error code (only if it looks like a code), and the names of a few response headers.
It never prints the app URL, a token, a response body or a header value.

    LIAM_E_APP_URL=<app-url> bash tools/uv.sh run --no-sync python tools/gateway_access_probe.py --live

The workspace key comes from the app's own credential resolution (``utils.mistral``), so nothing
has to be pasted into the shell. The probe never calls a mutating route.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import NoReturn

# Only read routes. ``/api/agent-read`` is the read facade from #35: with the owner's outside-access
# switch off (default) a key caller gets 403 ``agent_access_disabled`` before the verifier runs, and
# with the switch on the default verifier rejects every request with 401; ``/.well-known`` paths
# show whether OAuth discovery reaches the app.
CASES: tuple[tuple[str, str, str], ...] = (
    ("health, no credential", "/api/health", "none"),
    ("health, invalid bearer", "/api/health", "bearer-invalid"),
    ("health, workspace key as bearer", "/api/health", "bearer-key"),
    ("health, invalid x-api-key", "/api/health", "x-api-key-invalid"),
    ("health, workspace key as x-api-key", "/api/health", "x-api-key"),
    ("read facade, no credential", "/api/agent-read/v1/mail/accounts", "none"),
    ("read facade, workspace key as x-api-key", "/api/agent-read/v1/mail/accounts", "x-api-key"),
    ("oauth resource metadata, no credential", "/.well-known/oauth-protected-resource", "none"),
)
REPORTED_HEADERS = ("www-authenticate", "location", "set-cookie")
# Error codes the gateway or app access gate returns. Anything else is reported as "other", never verbatim.
KNOWN_ERROR_CODES = frozenset({
    "session_expired",
    "access_denied",
    "unauthorized",
    "forbidden",
    "not_found",
    "agent_access_disabled",
    "agent_access_unavailable",
    "agent_operation_refused",
})
INVALID = "invalid-test-only"


@dataclass(frozen=True)
class Outcome:
    label: str
    status: int | str
    error_code: str | None
    headers: tuple[str, ...]

    def line(self) -> str:
        code = self.error_code or "-"
        names = ",".join(self.headers) or "-"
        return f"{self.label}: {self.status} error={code} headers={names}"


def headers_for(kind: str, key: str) -> dict[str, str]:
    """The request headers of one credential variant."""
    return {
        "none": {},
        "bearer-invalid": {"Authorization": f"Bearer {INVALID}"},
        "bearer-key": {"Authorization": f"Bearer {key}"},
        "x-api-key-invalid": {"x-api-key": INVALID},
        "x-api-key": {"x-api-key": key},
    }[kind]


def redact(label: str, status: int | str, body: object, header_names: list[str]) -> Outcome:
    """Keep the status, a fixed-looking error code and header names; drop everything else."""
    code = None
    if isinstance(body, dict):
        for field in ("error", "code", "detail"):
            value = body.get(field)
            if isinstance(value, str) and value:
                code = value if value in KNOWN_ERROR_CODES else "other"
                break
    names = tuple(sorted({h.lower() for h in header_names} & set(REPORTED_HEADERS)))
    return Outcome(label, status, code, names)


def run_live(base_url: str) -> list[Outcome]:
    import httpx
    from utils.mistral import mistral_app_token

    key = mistral_app_token()
    outcomes = []
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=20, follow_redirects=False) as client:
        for label, path, kind in CASES:
            try:
                response = client.get(path, headers=headers_for(kind, key))
            except httpx.HTTPError as exc:
                outcomes.append(Outcome(label, f"transport:{type(exc).__name__}", None, ()))
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            outcomes.append(redact(label, response.status_code, body, list(response.headers.keys())))
    return outcomes


class _QuietParser(argparse.ArgumentParser):
    """Reports a fixed usage error; argparse would echo argument values, which can be URLs or tokens."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, "invalid arguments; see --help\n")


def main(argv: list[str] | None = None) -> int:
    parser = _QuietParser(description="Read-only probe of the credentials the app gateway accepts")
    parser.add_argument("--live", action="store_true", help="send the read-only requests")
    args = parser.parse_args(argv)
    if not args.live:
        print("Offline. Cases that --live would send (GET only):")
        for label, path, kind in CASES:
            print(f"  {label}: {path} [{kind}]")
        return 0
    base_url = os.environ.get("LIAM_E_APP_URL", "")
    if not base_url.startswith("https://"):
        print("LIAM_E_APP_URL must hold the app's https URL", file=sys.stderr)
        return 2
    try:
        outcomes = run_live(base_url)
    except Exception as exc:  # noqa: BLE001 - exception texts can carry the URL or a token path
        print(f"probe_failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    for outcome in outcomes:
        print(outcome.line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
