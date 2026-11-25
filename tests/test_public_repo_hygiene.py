"""Tracked files stay publishable: no private identifiers of the owner or of the environment.

liam-e is meant to become public (AGENTS.md, section "Publication and private data"). These
checks cover what a secret scanner does not look for: the Mistral tenant IDs, which no tracked
file holds (`tools/deploy.sh` reads them from an ignored local file), a `deployment` block in
`mistral.json`, links to private claude.ai resources, home paths (absolute, under `~/` or under
`~<user>/`), and mail addresses that could belong to a real person. Credentials are the job of
gitleaks (`.gitleaks.toml`, CI job `secrets`).

The checks read `git ls-files`, so they see exactly what a clone would publish. Tenant IDs and
private links are looked for in every tracked text file. The address and home-path heuristics
skip lock files, the generated API client and vendored agent skills, which nobody writes by hand
and which hold upstream examples.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SKIPPED_PREFIXES = (".agents/", ".mistral/", "apps/web/src/api/generated/")
SKIPPED_NAMES = {"bun.lock", "uv.lock", "package-lock.json"}

# Domains that can never reach a real mailbox (RFC 2606, RFC 6761), plus the pseudo-domain that
# api/mail connector accounts get when the connector names no sender address.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org", "connector.mistral")
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
# Local parts that name nobody. Provider presets (gmail.com, icloud.com, ...) are configuration,
# and tests that exercise them use one of these.
SYNTHETIC_LOCAL_PART = re.compile(r"^(?:[a-z]|me|test|user|noreply|no-reply)$")

ADDRESS = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w.-]*\w)")
# Absolute home directories, and paths under "~/" or "~<user>/", which name a home directory just
# as well.
HOME_PATH = re.compile(
    r"(?<![\w.~-])/(?:home|Users)/[A-Za-z_][\w.-]*(?![\w-])"
    r"|(?<![\w.~/-])~/[\w.][\w.-]*"
    r"|(?<![\w.~/-])~[A-Za-z_][\w.-]*/"
)
# One or two backslashes: a plain path or one escaped in JSON or a string literal.
WINDOWS_HOME = re.compile(r"(?i)(?<![\w])[a-z]:\\{1,2}Users\\{1,2}[^\\\s\"'<>]+")
# The host must be claude.ai itself: after "//" (with a scheme or protocol-relative, but not a
# doubled slash inside another URL's path), or standing alone (not a path segment or the tail of
# another host). Hosts are case-insensitive.
PRIVATE_LINK = re.compile(
    r"(?i)(?:(?<=//)(?<![\w.-]//)|(?<![\w./-]))(?:www\.)?claude\.ai/(?:artifact|chat|code|project|public|share)/[a-z0-9_-]+"
)
# Paths that match the home-path rule but name nobody's environment, such as a web route under
# "/home" or a tool's documented per-user config file. Each entry is (file, the whole path as
# written there) with a comment saying why it is harmless. The same path in another file, or a
# longer path that starts with it, is still reported.
NOT_HOME_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        # Bun's per-user config file, named to explain the registry pin and the release-age
        # quarantine. Split so that this file holds no home path itself.
        ("bunfig.toml", "~" + "/.bunfig.toml"),
    }
)
WHOLE_PATH = re.compile(r"[^\s\"'`<>()\[\]{},;]+")
DEPLOYMENT_KEYS = ("organizationId", "workspaceId", "appId")
# SHA-256 of the lowercase organization, workspace and app IDs of the owner's Studio app, so that
# the scan recognises them without this file holding them. They left mistral.json on 2026-11-22
# and remain in older commits (docs/publishing.md). The ignored deployment files of a checkout that
# deploys (tools/deploy.sh, tools/probes/mcp_route_app/deploy.sh) add their own values.
TENANT_ID_SHA256 = frozenset(
    {
        "0616dbc12055327684ace052b5d9b11d57619a6d3e6b442ab8facf4e9d76b5f3",
        "b9bf8d4a58798dee78c1744db9318f726fefa8dc656ba6f48a2ff424ca00c2f2",
        "209600b74a49d1313b390abea5c730d8e10dfdb1d97851c5947a91129ec257d6",
    }
)
UUID = re.compile(r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f-])")


@cache
def tracked_texts(*, everything: bool = False) -> dict[str, str]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        listing = b""
    if not listing:
        pytest.skip("not a git checkout")
    texts: dict[str, str] = {}
    for raw in listing.split(b"\0"):
        path = raw.decode()
        if not path:
            continue
        if not everything and (path.startswith(SKIPPED_PREFIXES) or Path(path).name in SKIPPED_NAMES):
            continue
        file = ROOT / path
        if not file.is_file():
            continue
        try:
            texts[path] = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return texts


def hits(pattern: re.Pattern[str], *, keep=lambda _match: True, everything: bool = False) -> list[str]:
    found = []
    for path, text in tracked_texts(everything=everything).items():
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                if keep(match):
                    found.append(f"{path}:{number}: {match.group(0)}")
    return found


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.lower().encode()).hexdigest()


# Where each deploy script reads its IDs: (environment override, default path in the checkout).
DEPLOYMENT_FILES = (
    ("LIAM_E_DEPLOYMENT_FILE", "mistral.deployment.json"),
    ("MCP_ROUTE_PROBE_DEPLOYMENT_FILE", "tools/probes/mcp_route_app/mistral.deployment.json"),
)


def local_tenant_fingerprints() -> frozenset[str]:
    """Fingerprints of the IDs in this checkout's deployment files, if it has any."""
    found: set[str] = set()
    for variable, default in DEPLOYMENT_FILES:
        path = Path(os.environ.get(variable) or ROOT / default)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found |= {fingerprint(value) for key in DEPLOYMENT_KEYS if isinstance(value := data.get(key), str)}
    return frozenset(found)


def tenant_id_hits(texts: dict[str, str], fingerprints: frozenset[str]) -> list[str]:
    """Lines that hold one of the fingerprinted IDs. Reports the place, never the value."""
    return [
        f"{path}:{number}"
        for path, text in texts.items()
        for number, line in enumerate(text.splitlines(), start=1)
        if any(fingerprint(match.group(0)) in fingerprints for match in UUID.finditer(line))
    ]


def test_no_tracked_file_holds_mistral_tenant_ids() -> None:
    found = tenant_id_hits(tracked_texts(everything=True), TENANT_ID_SHA256 | local_tenant_fingerprints())
    assert found == [], (
        "Mistral tenant IDs never belong in the repository; write <organization-id> etc. and deploy with tools/deploy.sh"
    )


def test_tenant_id_scan_reports_a_fingerprinted_id_in_any_case() -> None:
    synthetic = "12345678-1234-4123-8123-123456789abc"
    texts = {
        "mistral.json": f'  "appId": "{synthetic.upper()}"',
        "docs/deploy.md": "<app-id>, 00000000-0000-4000-8000-000000000000 and x" + synthetic + "0",
    }
    assert tenant_id_hits(texts, frozenset({fingerprint(synthetic)})) == ["mistral.json:1"]


def has_deployment_block(manifest: object) -> bool:
    """A `deployment` block would publish the IDs, or with placeholders break every deploy.
    Without it `mistral deploy` creates the deployer's own app; tools/deploy.sh adds the block to a
    temporary copy only."""
    return isinstance(manifest, dict) and "deployment" in manifest


def tracked_manifests() -> list[str]:
    return sorted(path for path in tracked_texts(everything=True) if Path(path).name == "mistral.json")


def test_mistral_json_binds_no_deployment() -> None:
    manifests = tracked_manifests()
    assert "mistral.json" in manifests and "tools/probes/mcp_route_app/mistral.json" in manifests
    # Paths only, so that a failure does not print a manifest and any IDs in it.
    bound = [path for path in manifests if has_deployment_block(json.loads(tracked_texts(everything=True)[path]))]
    assert bound == [], "remove the deployment block; deploy with tools/deploy.sh or the probe's deploy.sh"


@pytest.mark.parametrize(("variable", "default"), DEPLOYMENT_FILES)
def test_deployment_files_are_git_ignored(variable: str, default: str) -> None:
    del variable
    tracked_texts()  # skips outside a git checkout
    result = subprocess.run(["git", "check-ignore", "-q", default], cwd=ROOT, check=False)
    assert result.returncode == 0, f"{default} holds tenant IDs and must stay git-ignored"


def test_local_fingerprints_cover_every_deployment_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
    for (variable, _default), value in zip(DEPLOYMENT_FILES, synthetic, strict=True):
        path = tmp_path / f"{variable}.json"
        path.write_text(json.dumps({"organizationId": value}), encoding="utf-8")
        monkeypatch.setenv(variable, str(path))
    assert local_tenant_fingerprints() == {fingerprint(value) for value in synthetic}


@pytest.mark.parametrize(
    ("manifest", "flagged"),
    [
        ({"schemaVersion": 2, "kind": "app", "name": "liam-e", "modules": []}, False),
        (
            {
                "name": "liam-e",
                "deployment": {
                    "organizationId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "workspaceId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "appId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                },
            },
            True,
        ),
        ({"name": "liam-e", "deployment": {"appId": "<app-id>"}}, True),
        ({"name": "liam-e", "deployment": {}}, True),
    ],
)
def test_deployment_block_rule(manifest: dict[str, object], flagged: bool) -> None:
    assert has_deployment_block(manifest) is flagged


def test_no_links_to_private_claude_resources() -> None:
    assert hits(PRIVATE_LINK, everything=True) == [], "name the artifact or session instead of linking it"


def home_path_hits(texts: dict[str, str], allowed: frozenset[tuple[str, str]]) -> list[str]:
    found = []
    for path, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            for match in HOME_PATH.finditer(line):
                whole = WHOLE_PATH.match(line, match.start()).group(0)
                if (path, whole) not in allowed:
                    found.append(f"{path}:{number}: {match.group(0)}")
    return found


def test_no_absolute_home_paths() -> None:
    found = home_path_hits(tracked_texts(), NOT_HOME_PATHS) + hits(WINDOWS_HOME)
    assert found == [], "use repository-relative paths or a placeholder; a reviewed harmless path goes in NOT_HOME_PATHS"


def test_home_path_exception_is_bound_to_file_and_whole_path() -> None:
    route = "/home" + "/settings"
    allowed = frozenset({("apps/web/src/routes.ts", route)})
    assert home_path_hits({"apps/web/src/routes.ts": f'route = "{route}"'}, allowed) == []
    assert home_path_hits({"apps/web/src/routes.ts": f'path = "{route}/.ssh/config"'}, allowed) != []
    assert home_path_hits({"docs/notes.md": f'route = "{route}"'}, allowed) != []


def test_bunfig_exception_covers_only_that_file_and_path() -> None:
    config = "~" + "/.bunfig.toml"
    assert home_path_hits({"bunfig.toml": f"a proxy in {config} cannot leak"}, NOT_HOME_PATHS) == []
    assert home_path_hits({"bunfig.toml": f"see {config}.d/private"}, NOT_HOME_PATHS) != []
    assert home_path_hits({"docs/notes.md": f"a proxy in {config}"}, NOT_HOME_PATHS) != []


def is_personal_address(match: re.Match[str]) -> bool:
    local, domain = match.group(1).lower(), match.group(2).lower()
    if domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES):
        return False
    if any(domain.endswith("." + reserved) for reserved in RESERVED_DOMAINS):
        return False
    return not SYNTHETIC_LOCAL_PART.match(local)


def test_mail_addresses_are_synthetic() -> None:
    assert hits(ADDRESS, keep=is_personal_address) == [], (
        "use an address under example.org/.com/.net or *.test, or a local part like me@ or x@"
    )


@pytest.mark.parametrize(
    ("address", "personal"),
    [
        ("mira.beispiel@example.org", False),
        ("news@shop.example", False),
        ("x@yahoo.com", False),
        ("gmail@connector.mistral", False),
        # Split so that this file, which the scan reads too, holds no personal-looking address.
        ("anna.schmidt" + "@gmail.com", True),
        ("info" + "@some-company.de", True),
    ],
)
def test_address_rule(address: str, personal: bool) -> None:
    match = ADDRESS.search(address)
    assert match is not None
    assert is_personal_address(match) is personal


@pytest.mark.parametrize(
    ("text", "flagged"),
    # Split like the addresses above.
    [
        ("/home" + "/someone/project", True),
        ("/home" + "/someone", True),
        ("/home" + "/_build/project", True),
        ("/Users" + "/someone/x", True),
        ("C:" + "\\Users\\Someone\\project", True),
        ('{"path": "C:' + '\\\\Users\\\\Someone\\\\project"}', True),
        ("~" + "/project", True),
        ("~" + "/.config/tool/state", True),
        ("cd " + "~" + "/work", True),
        ("~" + "someone/project", True),
        ("cd " + "~" + "_build/x", True),
        ("(" + "~" + "someone/.ssh)", True),
        ("~/", False),
        ("https://example.org/~/x", False),
        ("a~/b", False),
        ("a~b/c", False),
        ("~5/x", False),
        ("~someone", False),
        ("~~struck~~/", False),
        ("/home/", False),
        ("apps/home/view.tsx", False),
    ],
)
def test_home_path_rule(text: str, flagged: bool) -> None:
    assert bool(HOME_PATH.search(text) or WINDOWS_HOME.search(text)) is flagged


@pytest.mark.parametrize(
    ("text", "flagged"),
    # Split so that this file holds no link the scan would flag.
    [
        ("https://claude" + ".ai/artifact/test-only", True),
        ("https://claude" + ".ai/code/session_test-only", True),
        ("https://claude" + ".ai/chat/00000000-0000-4000-8000-000000000000", True),
        ("https://claude" + ".ai/project/test-only", True),
        ("https://claude" + ".ai/share/test-only", True),
        ("https://CLAUDE" + ".AI/chat/test-only", True),
        ("(see claude" + ".ai/share/test-only)", True),
        ("[chat](//claude" + ".ai/chat/test-only)", True),
        ("//claude" + ".ai/chat/test-only", True),
        ("https://notclaude" + ".ai/artifact/test-only", False),
        ("https://example.org/claude" + ".ai/chat/test-only", False),
        ("https://example.org//claude" + ".ai/chat/test-only", False),
        ("https://sub.claude" + ".ai.example/chat/test-only", False),
        ("https://claude" + ".ai/", False),
    ],
)
def test_private_link_rule(text: str, flagged: bool) -> None:
    assert bool(PRIVATE_LINK.search(text)) is flagged
