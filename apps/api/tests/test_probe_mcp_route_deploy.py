"""Offline checks for the probe's deploy helper (#157) against a throwaway repository and a fake CLI.

Each test builds a bare `origin` and a checkout holding the probe directory's tracked files, the
probe's deployment file and two liam-e deployment files outside that checkout, and a fake `mistral`
that records its arguments and answers `apps list` from a file. The fake creates an app the way
CLI v0.18.1 does: without asking, named after `--name` or else after the directory it runs in, and
`apps delete` removes the app from that file. A delete reads the probe's own deployment file and
liam-e's test and production apps from a liam-e config directory under the test's own HOME.
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
if args[:1] == ["deploy"]:
    staging = pathlib.Path(os.environ.get("TMPDIR") or "/tmp")
    (call / "tmpdir").write_text(str(staging))
    (call / "tmpmode").write_text(oct(staging.stat().st_mode & 0o777))
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
elif args[:2] == ["apps", "delete"]:
    status = int(os.environ.get("FAKE_DELETE_STATUS", "0"))
    gone = set() if status or os.environ.get("FAKE_DELETE_KEEPS") else {args[2]}
    gone |= {value for value in os.environ.get("FAKE_DELETE_ALSO", "").split(",") if value}
    listing = json.loads(apps_file.read_text())
    listing["apps"] = [app for app in listing["apps"] if app.get("id") not in gone]
    for app in listing["apps"]:
        if app.get("id") == os.environ.get("FAKE_DELETE_TOUCH"):
            app["updatedAt"] = "2026-11-27T12:00:00Z"
    apps_file.write_text(json.dumps(listing))
    # The real CLI may name the app it deleted; the script must not pass that on.
    print(f"Deleted app {args[2]}")
    sys.exit(status)
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
    home: Path
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
            if (call / "tmpdir").exists():
                entry["tmpdir"] = Path((call / "tmpdir").read_text())
                entry["tmpmode"] = (call / "tmpmode").read_text()
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

    def listed_ids(self) -> list[str]:
        return [app["id"] for app in json.loads(self.apps.read_text())["apps"]]

    def config_dir(self) -> Path:
        return self.home / ".config" / "liam-e"

    def prepare_delete(self, app_id: str = NEW, *, name: str | None = PROBE) -> None:
        """The setup of a delete: the app ID only in the probe's own deployment file, and liam-e's
        test and production apps in the config directory instead of the environment."""
        del self.env["MCP_ROUTE_PROBE_DEPLOYMENT_FILE"]
        del self.env["MCP_ROUTE_PROBE_LIAM_E_FILES"]
        self.ids = self.work / PROBE_DIR / "mistral.deployment.json"
        self.record(app_id)
        self.ids.chmod(0o600)
        self.config_dir().mkdir(parents=True)
        shutil.copy(self.liam_test, self.config_dir() / "studio-test.json")
        shutil.copy(self.liam_prod, self.config_dir() / "studio-production.json")
        if name is not None:
            self.add_app({"id": app_id, "name": name, "label": name})


@pytest.fixture
def probe(deploy_tmp_path: Path) -> Probe:
    home, log, fakebin = (deploy_tmp_path / name for name in ("home", "log", "bin"))
    tmpdir = deploy_tmp_path / "tmp"
    for path in (home, tmpdir, fakebin):
        path.mkdir()
    gitconfig = deploy_tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.org\n[init]\n\tdefaultBranch = main\n")
    mistral = fakebin / "mistral"
    mistral.write_text(FAKE_MISTRAL)
    mistral.chmod(mistral.stat().st_mode | stat.S_IXUSR)

    ids, apps = deploy_tmp_path / "probe.json", deploy_tmp_path / "apps.json"
    liam_test, liam_prod = deploy_tmp_path / "liam-test.json", deploy_tmp_path / "liam-prod.json"
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
           if not key.startswith(("GIT_", "LIAM_E_", "MCP_ROUTE_PROBE_", "FAKE_")) and key != "XDG_CONFIG_HOME"}
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
    origin, work = deploy_tmp_path / "origin.git", deploy_tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], env=env, check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], env=env, check=True, capture_output=True)
    tracked = subprocess.run(["git", "ls-files", PROBE_DIR], cwd=ROOT, check=True, capture_output=True,
                             text=True).stdout.split()
    assert f"{PROBE_DIR}/deploy.sh" in tracked
    for name in [*tracked, "tools/deploy_temp.py"]:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, work / name)
    probe = Probe(work=work, tmpdir=tmpdir, log=log, ids=ids, apps=apps, liam_test=liam_test, liam_prod=liam_prod,
                  home=home, env=env)
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


# --- a .git above the temporary directory (#190) -----------------------------------------------


def put_git_above_tmpdir(probe: Probe, kind: str) -> Path:
    """Points TMPDIR into a directory whose parent holds a `.git` entry, as on 26 November."""
    outer = probe.tmpdir.parent / "outer"
    tmpdir = outer / "level" / "tmp"
    tmpdir.mkdir(parents=True)
    if kind == "file":
        (outer / ".git").write_text("gitdir: elsewhere\n")
    else:
        (outer / ".git").mkdir()
        if kind == "full":
            (outer / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    probe.env["TMPDIR"] = str(tmpdir)
    return tmpdir


@pytest.mark.parametrize("kind", ["empty", "full", "file"])
@pytest.mark.parametrize("update", [False, True])
def test_a_git_entry_above_the_temporary_directory_stops_a_deploy(probe: Probe, kind: str, update: bool) -> None:
    probe.env["FAKE_NEW_APP"] = NEW
    if update:
        probe.record(NEW)
        probe.add_app({"id": NEW, "name": PROBE, "label": PROBE})
    tmpdir = put_git_above_tmpdir(probe, kind)
    result = probe.run(*([probe.head()] if update else [probe.head(), "--create-app", PROBE]))

    assert result.returncode == 1
    assert "a .git sits in a parent directory of the temporary directory" in result.stderr
    assert "TMPDIR" in result.stderr and str(tmpdir) not in result.stderr
    assert probe.calls() == [], "the CLI must not run, not even the read-only listing"
    assert list(tmpdir.iterdir()) == [], "the temporary directory must be removed"
    assert probe.recorded().get("appId") == (NEW if update else None)
    assert_no_ids(result)


def test_unset_tmpdir_uses_private_xdg_cache_for_the_gitless_export(probe: Probe) -> None:
    del probe.env["TMPDIR"]
    cache = probe.home / "cache"
    probe.env.update(XDG_CACHE_HOME=str(cache), FAKE_NEW_APP=NEW)

    result = probe.run(probe.head(), "--create-app", PROBE)

    assert result.returncode == 0, result.stderr
    _, deploy, _ = probe.calls()
    assert deploy["tmpdir"] == deploy["cwd"].parent
    assert deploy["tmpdir"].parent == cache / "liam-e"
    assert deploy["tmpmode"] == "0o700"
    assert not deploy["tmpdir"].exists()


def test_a_git_entry_above_the_temporary_directory_does_not_stop_a_delete(probe: Probe) -> None:
    probe.prepare_delete()
    tmpdir = put_git_above_tmpdir(probe, "empty")
    result = probe.run("--delete")

    assert result.returncode == 0, result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]
    assert list(tmpdir.iterdir()) == []


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
    result = probe.run(probe.head())
    assert result.returncode == 1 and "liam-e's own app" in result.stderr
    assert_no_ids(result)
    assert probe.calls() == []


def test_the_config_directory_also_guards_deploys(probe: Probe) -> None:
    del probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"]
    probe.config_dir().mkdir(parents=True)
    shutil.copy(probe.liam_prod, probe.config_dir() / "studio-production.json")
    probe.record(LIAM_PROD)
    result = probe.run(probe.head())

    assert result.returncode == 1 and "liam-e's own app" in result.stderr
    assert probe.calls() == []


def test_an_unreadable_config_file_stops_every_mode(probe: Probe) -> None:
    probe.config_dir().mkdir(parents=True)
    (probe.config_dir() / "broken.json").write_text("not json\n")
    result = probe.run(probe.head(), "--create-app", PROBE)
    assert result.returncode == 1 and "not readable JSON" in result.stderr

    del probe.env["MCP_ROUTE_PROBE_DEPLOYMENT_FILE"]
    probe.ids = probe.work / PROBE_DIR / "mistral.deployment.json"
    probe.record(NEW)
    result = probe.run("--delete")
    assert result.returncode == 1 and "not readable JSON" in result.stderr
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


DELETE_ARGS = ["apps", "delete", NEW, "--organization", ORG, "--workspace", WS, "--yes"]


def test_delete_needs_a_recorded_app(probe: Probe) -> None:
    probe.prepare_delete(name=None)
    probe.ids.write_text(json.dumps({"organizationId": ORG, "workspaceId": WS}))
    result = probe.run("--delete", "--apply")
    assert result.returncode == 1 and "no appId" in result.stderr
    assert probe.calls() == []


def test_delete_is_a_dry_run_by_default(probe: Probe) -> None:
    probe.prepare_delete()
    result = probe.run("--delete")

    assert result.returncode == 0, result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS], "a dry run only lists"
    assert probe.recorded()["appId"] == NEW and NEW in probe.listed_ids()
    assert "Dry run" in result.stdout and "--apply" in result.stdout and "Nothing was deleted" in result.stdout
    assert_no_ids(result)


def test_delete_with_apply_deletes_the_app_and_checks_the_listing(probe: Probe) -> None:
    probe.prepare_delete()
    result = probe.run("--delete", "--apply")

    assert result.returncode == 0, result.stderr
    before, delete, after = probe.calls()
    assert before["args"] == LIST_ARGS and after["args"] == LIST_ARGS
    # --yes because the script has asked already (--apply) and runs off a terminal.
    assert delete["args"] == DELETE_ARGS
    assert probe.recorded() == {"organizationId": ORG, "workspaceId": WS}
    assert stat.S_IMODE(probe.ids.stat().st_mode) == 0o600
    assert probe.listed_ids() == [LIAM_TEST, LIAM_PROD]
    assert f"no app named {PROBE} remains" in result.stdout
    assert "the 2 liam-e apps listed before are still listed, unchanged" in result.stdout
    assert list(probe.tmpdir.iterdir()) == [], "the work directory must be removed"
    assert_no_ids(result)


def test_delete_reads_only_the_probe_deployment_file(probe: Probe) -> None:
    probe.prepare_delete()
    probe.env["MCP_ROUTE_PROBE_DEPLOYMENT_FILE"] = str(probe.ids)
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "MCP_ROUTE_PROBE_DEPLOYMENT_FILE" in result.stderr
    assert probe.calls() == []


def test_delete_needs_liam_e_app_ids_from_the_config_directory(probe: Probe) -> None:
    probe.prepare_delete()
    shutil.rmtree(probe.config_dir())
    # Another protected source is not enough: the delete compares with liam-e's own config files.
    probe.env["MCP_ROUTE_PROBE_LIAM_E_FILES"] = f"{probe.liam_test}{os.pathsep}{probe.liam_prod}"
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "liam-e config directory" in result.stderr
    assert probe.calls() == [] and probe.recorded()["appId"] == NEW


def test_delete_honours_xdg_config_home(probe: Probe) -> None:
    probe.prepare_delete()
    elsewhere = probe.home / "xdg"
    elsewhere.mkdir()
    probe.config_dir().rename(elsewhere / "liam-e")
    assert probe.run("--delete").returncode == 1

    probe.env["XDG_CONFIG_HOME"] = str(elsewhere)
    result = probe.run("--delete")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("liam_app", [LIAM_TEST, LIAM_PROD])
@pytest.mark.parametrize("renamed", [False, True])
def test_delete_refuses_liam_e_app_ids(probe: Probe, liam_app: str, renamed: bool) -> None:
    probe.prepare_delete(liam_app, name=None)
    if renamed:  # even a liam-e app that carries the probe's name
        listing = json.loads(probe.apps.read_text())
        for app in listing["apps"]:
            if app["id"] == liam_app:
                app.update(name=PROBE, label=PROBE)
        probe.apps.write_text(json.dumps(listing))
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "liam-e's own app" in result.stderr
    assert probe.calls() == [], "the CLI must not run"
    assert probe.recorded()["appId"] == liam_app and liam_app in probe.listed_ids()
    assert_no_ids(result)


@pytest.mark.parametrize(("name", "label"), [
    ("app", "app"),
    (f"{PROBE}-1a2b", PROBE),  # only the label matches
    (PROBE.upper(), PROBE.upper()),
    ("liam-e", "liam-e"),
])
def test_delete_refuses_an_app_of_another_name(probe: Probe, name: str, label: str) -> None:
    probe.prepare_delete(name=None)
    probe.add_app({"id": NEW, "name": name, "label": label})
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "nothing was deleted" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS], "only the read-only listing may run"
    assert probe.recorded()["appId"] == NEW
    assert_no_ids(result)


def test_delete_refuses_when_no_app_matches(probe: Probe) -> None:
    probe.prepare_delete(name=None)
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "nothing was deleted" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]
    assert probe.recorded()["appId"] == NEW


@pytest.mark.parametrize("field", ["name", "label"])
def test_delete_refuses_two_apps_of_the_name(probe: Probe, field: str) -> None:
    probe.prepare_delete()
    probe.add_app({"id": OTHER, "name": "earlier", "label": "earlier", field: PROBE})
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "also named" in result.stderr and "nothing was deleted" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS]
    assert OTHER in probe.listed_ids() and NEW in probe.listed_ids()


def test_a_failed_cli_delete_keeps_the_app_id(probe: Probe) -> None:
    probe.prepare_delete()
    probe.env["FAKE_DELETE_STATUS"] = "3"
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "exited with 3" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS, DELETE_ARGS]
    assert probe.recorded()["appId"] == NEW
    assert_no_ids(result)


def test_an_app_still_listed_after_the_delete_keeps_the_app_id(probe: Probe) -> None:
    probe.prepare_delete()
    probe.env["FAKE_DELETE_KEEPS"] = "1"
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "still shows" in result.stderr
    assert [call["args"] for call in probe.calls()] == [LIST_ARGS, DELETE_ARGS, LIST_ARGS]
    assert probe.recorded()["appId"] == NEW
    assert_no_ids(result)


@pytest.mark.parametrize("variable", ["FAKE_DELETE_ALSO", "FAKE_DELETE_TOUCH"])
def test_a_liam_e_app_missing_or_changed_after_the_delete_is_reported(probe: Probe, variable: str) -> None:
    probe.prepare_delete()
    probe.env[variable] = LIAM_TEST
    result = probe.run("--delete", "--apply")

    assert result.returncode == 1 and "liam-e app" in result.stderr and "missing or changed" in result.stderr
    assert probe.recorded() == {"organizationId": ORG, "workspaceId": WS}, "the probe app is gone either way"
    assert_no_ids(result)


@pytest.mark.parametrize("args", [["--apply"], ["--apply", "--delete"], ["--delete", "--apply", "--apply"],
                                  ["--delete", "--yes"], ["--delete", "apply"], ["HEAD", "--apply"]])
def test_delete_usage(probe: Probe, args: list[str]) -> None:
    probe.prepare_delete()
    assert probe.run(*args).returncode == 64, args
    assert probe.calls() == []
