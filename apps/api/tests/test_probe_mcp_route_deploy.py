"""Offline checks for the probe's deploy helper (#157) against a throwaway repository and a fake CLI.

Each test builds a bare `origin` and a checkout holding the probe directory's tracked files, the
probe's deployment file and two liam-e deployment files outside that checkout, and a fake `mistral`
that records its arguments and answers `apps list` from a file. The fake creates an app the way
CLI v0.18.1 does: without asking, named after `--name` or else after the directory it runs in.
Nothing reaches Mistral or GitHub. The IDs are synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROBE_DIR = "tools/probes/mcp_route_app"
PROBE = "liam-e-mcp-route-probe"
ORG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WS = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NEW = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
LIAM_TEST = "11111111-1111-4111-8111-111111111111"
LIAM_PROD = "22222222-2222-4222-8222-222222222222"
LIST_ARGS = ["apps", "list", "--organization", ORG, "--workspace", WS, "--json"]

FAKE_MISTRAL = """#!/usr/bin/env python3
import json, os, pathlib, sys

log = pathlib.Path(os.environ["FAKE_MISTRAL_LOG"])
log.mkdir(exist_ok=True)
call = log / str(len(list(log.iterdir())))
call.mkdir()
args = sys.argv[1:]
(call / "args").write_text("\\n".join(args) + "\\n")
(call / "cwd").write_text(os.getcwd() + "\\n")
apps_file = pathlib.Path(os.environ["FAKE_APPS_FILE"])


def flag(name):
    return args[args.index(name) + 1] if name in args else None


if args[:2] == ["apps", "list"]:
    sys.stdout.write(apps_file.read_text())
elif args[:1] == ["deploy"]:
    manifest = json.loads(pathlib.Path("mistral.json").read_text())
    (call / "mistral.json").write_text(json.dumps(manifest))
    new_app = os.environ.get("FAKE_NEW_APP")
    if new_app and "deployment" not in manifest:
        # CLI v0.18.1: no confirmation, and the name falls back to the directory name.
        name = os.environ.get("FAKE_CREATED_NAME") or flag("--name") or os.path.basename(os.getcwd())
        manifest["deployment"] = {"organizationId": flag("--organization"), "workspaceId": flag("--workspace"),
                                  "appId": new_app}
        pathlib.Path("mistral.json").write_text(json.dumps(manifest))
        listing = json.loads(apps_file.read_text())
        listing["apps"].append({"id": new_app, "name": name, "label": name})
        apps_file.write_text(json.dumps(listing))
    sys.exit(int(os.environ.get("FAKE_DEPLOY_STATUS", "0")))
"""


@dataclass
class Probe:
    work: Path
    tmpdir: Path
    log: Path
    ids: Path
    apps: Path
    liam_test: Path
    liam_prod: Path
    env: dict[str, str]

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.work, env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.work / PROBE_DIR / "deploy.sh"), *args], cwd=self.work, env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, check=False)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        result = []
        for call in sorted(self.log.iterdir(), key=lambda path: int(path.name)):
            entry: dict[str, Any] = {"args": (call / "args").read_text().splitlines(),
                                     "cwd": Path((call / "cwd").read_text().strip())}
            if (call / "mistral.json").exists():
                entry["manifest"] = json.loads((call / "mistral.json").read_text())
            result.append(entry)
        return result

    def recorded(self) -> dict[str, str]:
        return json.loads(self.ids.read_text())

    def record(self, app_id: str) -> None:
        self.ids.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS, "appId": app_id}))

    def add_app(self, app: dict[str, str]) -> None:
        listing = json.loads(self.apps.read_text())
        listing["apps"].append(app)
        self.apps.write_text(json.dumps(listing))


@pytest.fixture
def probe(tmp_path: Path) -> Probe:
    home, tmpdir, log, fakebin = (tmp_path / name for name in ("home", "tmp", "log", "bin"))
    for path in (home, tmpdir, fakebin):
        path.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.org\n[init]\n\tdefaultBranch = main\n")
    mistral = fakebin / "mistral"
    mistral.write_text(FAKE_MISTRAL)
    mistral.chmod(mistral.stat().st_mode | stat.S_IXUSR)

    ids, apps = tmp_path / "probe.json", tmp_path / "apps.json"
    liam_test, liam_prod = tmp_path / "liam-test.json", tmp_path / "liam-prod.json"
    ids.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS}))
    liam_test.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS, "appId": LIAM_TEST}))
    liam_prod.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS, "appId": LIAM_PROD}))
    apps.write_text(json.dumps({
        "organization": {"id": ORG, "name": "Synthetic organization"},
        "workspace": {"id": WS, "name": "Synthetic workspace"},
        "apps": [{"id": LIAM_TEST, "name": "liam-e-test", "label": "liam-e-test"},
                 {"id": LIAM_PROD, "name": "liam-e", "label": "liam-e"}],
    }))

    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "LIAM_E_", "MCP_ROUTE_PROBE_", "FAKE_"))}
    env.update(
        HOME=str(home),
        TMPDIR=str(tmpdir),
        PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        FAKE_MISTRAL_LOG=str(log),
        FAKE_APPS_FILE=str(apps),
        MCP_ROUTE_PROBE_DEPLOYMENT_FILE=str(ids),
        MCP_ROUTE_PROBE_LIAM_E_FILES=f"{liam_test}{os.pathsep}{liam_prod}",
    )
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], env=env, check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], env=env, check=True, capture_output=True)
    tracked = subprocess.run(["git", "ls-files", PROBE_DIR], cwd=ROOT, check=True, capture_output=True,
                             text=True).stdout.split()
    assert f"{PROBE_DIR}/deploy.sh" in tracked
    for name in tracked:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, work / name)
    probe = Probe(work=work, tmpdir=tmpdir, log=log, ids=ids, apps=apps, liam_test=liam_test, liam_prod=liam_prod,
                  env=env)
    probe.git("add", ".")
    probe.git("commit", "--quiet", "-m", "probe")
    probe.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
    return probe


def assert_no_ids(result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    for value in (ORG, WS, NEW, OTHER, LIAM_TEST, LIAM_PROD):
        assert value not in output


# --- file and usage validation -----------------------------------------------------------------


def test_rejects_the_placeholder_example(probe: Probe) -> None:
    probe.ids.write_text((ROOT / PROBE_DIR / "mistral.deployment.example.json").read_text(encoding="utf-8"))
    result = probe.run(probe.head(), "--create-app", PROBE)
    assert result.returncode == 1 and "lowercase UUID" in result.stderr
    assert "<organization-id>" not in result.stderr + result.stdout
    assert probe.calls() == []


def test_rejects_unknown_keys_and_bad_usage(probe: Probe) -> None:
    probe.ids.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS, "secret": OTHER}))
    result = probe.run(probe.head())
    assert result.returncode == 1 and "must hold organizationId and workspaceId" in result.stderr
    for args in ([], ["--create-app", PROBE], ["--delete", "--create-app", PROBE], [probe.head(), "--yes"],
                 [probe.head(), "--create-app"], [probe.head(), "--create-app", PROBE, "extra"]):
        assert probe.run(*args).returncode == 64, args
    assert probe.calls() == []


# --- the explicit confirmation of a first deploy -----------------------------------------------


@pytest.mark.parametrize("confirmation", [None, "", "liam-e", PROBE.upper(), f"{PROBE} ", "app"])
def test_first_deploy_needs_the_exact_confirmation(probe: Probe, confirmation: str | None) -> None:
    probe.env["FAKE_NEW_APP"] = NEW
    args = [probe.head()] if confirmation is None else [probe.head(), "--create-app", confirmation]
    result = probe.run(*args)
    assert result.returncode == 1
    assert f"--create-app {PROBE}" in result.stderr or f"exactly {PROBE}" in result.stderr
    assert probe.calls() == [], "the CLI must not run without the confirmation"
    assert "appId" not in probe.recorded()


def test_first_deploy_passes_the_name_and_records_the_app(probe: Probe) -> None:
    probe.env["FAKE_NEW_APP"] = NEW
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 0, result.stderr
    before, deploy, after = probe.calls()
    assert before["args"] == LIST_ARGS and after["args"] == LIST_ARGS
    assert deploy["args"] == ["deploy", "--from-disk", "--name", PROBE, "--organization", ORG, "--workspace", WS]
    assert deploy["cwd"].name == PROBE and deploy["cwd"].is_relative_to(probe.tmpdir)
    assert "deployment" not in deploy["manifest"]
    assert probe.recorded() == {"organizationId": ORG, "workspaceId": WS, "appId": NEW}
    assert stat.S_IMODE(probe.ids.stat().st_mode) == 0o600
    assert "the only app named" in result.stdout
    assert list(probe.tmpdir.iterdir()) == [], "the exported copy must be removed"
    assert_no_ids(result)


@pytest.mark.parametrize("field", ["name", "label"])
def test_first_deploy_refuses_an_existing_app_of_the_name(probe: Probe, field: str) -> None:
    probe.env["FAKE_NEW_APP"] = NEW
    probe.add_app({"id": OTHER, "name": "earlier", "label": "earlier", field: PROBE})
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1
    assert "already exists" in result.stderr and "nothing was created" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS], "only the read-only listing may run"
    assert "appId" not in probe.recorded()
    assert_no_ids(result)


def test_first_deploy_refuses_a_listing_of_another_workspace(probe: Probe) -> None:
    probe.env["FAKE_NEW_APP"] = NEW
    listing = json.loads(probe.apps.read_text())
    listing["workspace"]["id"] = OTHER
    probe.apps.write_text(json.dumps(listing))
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "another workspace" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]


def test_first_deploy_refuses_a_commit_outside_main(probe: Probe) -> None:
    (probe.work / "local.txt").write_text("not pushed\n")
    probe.git("add", "local.txt")
    probe.git("commit", "--quiet", "-m", "local only")
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "not on origin/main" in result.stderr
    assert probe.calls() == []


def test_created_app_must_show_up_under_the_probe_name(probe: Probe) -> None:
    probe.env.update(FAKE_NEW_APP=NEW, FAKE_CREATED_NAME="app")
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "does not show it" in result.stderr
    assert probe.recorded()["appId"] == NEW, "the created app stays recorded so it can be found and removed"


def test_a_failed_build_still_records_the_created_app(probe: Probe) -> None:
    probe.env.update(FAKE_NEW_APP=NEW, FAKE_DEPLOY_STATUS="3")
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 3
    assert probe.recorded()["appId"] == NEW
    assert "Recorded the new probe app's ID" in result.stdout


def test_a_failed_deploy_without_an_app_records_nothing(probe: Probe) -> None:
    probe.env["FAKE_DEPLOY_STATUS"] = "3"
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "left no app ID" in result.stderr
    assert "appId" not in probe.recorded()


# --- updates -----------------------------------------------------------------------------------


def test_update_deploys_to_the_recorded_app(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": PROBE, "label": PROBE})
    result = probe.run(probe.head())

    assert result.returncode == 0, result.stderr
    listing, deploy = probe.calls()
    assert listing["args"] == LIST_ARGS
    assert deploy["args"] == ["deploy", "--from-disk"]
    assert deploy["manifest"]["deployment"] == {"organizationId": ORG, "workspaceId": WS, "appId": NEW}
    assert_no_ids(result)


def test_update_accepts_the_probe_name_as_label(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": "liam-e-mcp-route-probe-1a2b", "label": PROBE})
    result = probe.run(probe.head())

    assert result.returncode == 0, result.stderr
    assert [call["args"][:2] for call in probe.calls()] == [LIST_ARGS[:2], ["deploy", "--from-disk"]]


def test_update_refuses_the_create_confirmation(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": PROBE, "label": PROBE})
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "leave out --create-app" in result.stderr
    assert probe.calls() == []


def test_update_refuses_a_second_app_of_the_name(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": PROBE, "label": PROBE})
    probe.add_app({"id": OTHER, "name": PROBE, "label": PROBE})
    result = probe.run(probe.head())

    assert result.returncode == 1 and "also named" in result.stderr and "nothing was deployed" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]


def test_update_refuses_a_recorded_app_under_another_name(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": "app", "label": "app"})
    result = probe.run(probe.head())

    assert result.returncode == 1 and "no app with the recorded ID" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]


# --- liam-e's own apps -------------------------------------------------------------------------


@pytest.mark.parametrize("liam_app", [LIAM_TEST, LIAM_PROD])
def test_liam_e_app_ids_are_refused(probe: Probe, liam_app: str) -> None:
    probe.record(liam_app)
    for args in ([probe.head()], ["--delete"]):
        result = probe.run(*args)
        assert result.returncode == 1 and "liam-e's own app" in result.stderr
        assert_no_ids(result)
    assert probe.calls() == []


def test_liam_e_deployment_file_variable_still_guards(probe: Probe) -> None:
    del probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"]
    probe.env["LIAM_E_DEPLOYMENT_FILE"] = str(probe.liam_test)
    probe.record(LIAM_TEST)
    result = probe.run(probe.head())

    assert result.returncode == 1 and "liam-e's own app" in result.stderr
    assert probe.calls() == []


def test_root_deployment_file_still_guards(probe: Probe) -> None:
    del probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"]
    shutil.copy(probe.liam_prod, probe.work / "mistral.deployment.json")
    probe.record(LIAM_PROD)
    result = probe.run(probe.head())

    assert result.returncode == 1 and "liam-e's own app" in result.stderr
    assert probe.calls() == []


def test_without_a_liam_e_app_id_nothing_runs(probe: Probe) -> None:
    del probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"]
    probe.env["FAKE_NEW_APP"] = NEW
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "no liam-e deployment file names an app ID" in result.stderr
    assert probe.calls() == []


def test_a_listed_liam_e_file_must_exist(probe: Probe) -> None:
    probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"] = f"{probe.work / 'missing.json'}{os.pathsep}{probe.liam_test}"
    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 1 and "missing or unreadable" in result.stderr
    assert probe.calls() == []


# --- delete ------------------------------------------------------------------------------------


def test_delete_needs_a_recorded_app(probe: Probe) -> None:
    result = probe.run("--delete")
    assert result.returncode == 1 and "no appId" in result.stderr
    assert probe.calls() == []


def test_delete_checks_the_listing_and_leaves_the_prompt_to_the_cli(probe: Probe) -> None:
    probe.record(NEW)
    probe.add_app({"id": NEW, "name": PROBE, "label": PROBE})
    result = probe.run("--delete")

    assert result.returncode == 0, result.stderr
    listing, delete = probe.calls()
    assert listing["args"] == LIST_ARGS
    assert delete["args"] == ["apps", "delete", NEW, "--organization", ORG, "--workspace", WS]
    assert probe.recorded() == {"organizationId": ORG, "workspaceId": WS}


def test_delete_refuses_an_unlisted_app(probe: Probe) -> None:
    probe.record(NEW)
    result = probe.run("--delete")

    assert result.returncode == 1 and "nothing was deleted" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]
    assert probe.recorded()["appId"] == NEW
