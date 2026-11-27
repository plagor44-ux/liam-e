"""Offline checks for tools/deploy.sh against a throwaway repository and a fake `mistral` CLI.

Each test builds a bare `origin`, a checkout with the real script committed under `tools/`, the
git-ignored deployment file and `.env`, and a fake CLI that records its arguments, working
directory and the files it would upload. Nothing reaches Mistral or GitHub. The IDs are synthetic.
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

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "deploy.sh"
ORGANIZATION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
APP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
IDS = {"organizationId": ORGANIZATION, "workspaceId": WORKSPACE, "appId": APP}
MANIFEST = {"schemaVersion": 2, "kind": "app", "name": "liam-e", "modules": []}

FAKE_MISTRAL = """#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$FAKE_MISTRAL_LOG"
call="$FAKE_MISTRAL_LOG/$(find "$FAKE_MISTRAL_LOG" -mindepth 1 -maxdepth 1 | wc -l)"
mkdir -p "$call"
printf '%s\\n' "$@" >"$call/args"
pwd >"$call/cwd"
printf '%s\\n' "${TMPDIR:-/tmp}" >"$call/tmpdir"
python3 -c 'import os, stat; print(oct(stat.S_IMODE(os.stat(os.environ.get("TMPDIR", "/tmp")).st_mode)))' >"$call/tmpmode"
if [ "$1 ${2:-}" = "apps list" ]; then
  printf '%s' "$FAKE_APPS_JSON"
elif [ "$1" = deploy ]; then
  cp mistral.json "$call/mistral.json"
  find . -path ./.git -prune -o -type f -print | sort >"$call/files"
  if git push --quiet origin HEAD:refs/heads/pushed-by-cli 2>/dev/null; then
    echo pushed >"$call/push"
  else
    echo refused >"$call/push"
  fi
fi
"""


@dataclass
class Repo:
    work: Path
    origin: Path
    tmpdir: Path
    log: Path
    env: dict[str, str]

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.work, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def deploy(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.work / "tools" / "deploy.sh"), *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        result = []
        for call in sorted(self.log.iterdir(), key=lambda path: int(path.name)):
            entry: dict[str, Any] = {
                "args": (call / "args").read_text().splitlines(),
                "cwd": (call / "cwd").read_text().strip(),
                "tmpdir": Path((call / "tmpdir").read_text().strip()),
                "tmpmode": (call / "tmpmode").read_text().strip(),
            }
            for name in ("files", "push"):
                if (call / name).exists():
                    entry[name] = (call / name).read_text().split()
            if (call / "mistral.json").exists():
                entry["manifest"] = json.loads((call / "mistral.json").read_text())
            result.append(entry)
        return result


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    home, tmpdir, log, fakebin = (tmp_path / name for name in ("home", "tmp", "log", "bin"))
    for path in (home, tmpdir, fakebin):
        path.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.org\n[init]\n\tdefaultBranch = main\n")
    mistral = fakebin / "mistral"
    mistral.write_text(FAKE_MISTRAL)
    mistral.chmod(mistral.stat().st_mode | stat.S_IXUSR)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "LIAM_E_")) and key != "XDG_CACHE_HOME"}
    env.update(
        HOME=str(home),
        TMPDIR=str(tmpdir),
        PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        FAKE_MISTRAL_LOG=str(log),
        FAKE_APPS_JSON=json.dumps({"apps": [{"id": APP, "name": "liam-e"}]}),
    )
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], env=env, check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], env=env, check=True, capture_output=True)
    repo = Repo(work=work, origin=origin, tmpdir=tmpdir, log=log, env=env)
    (work / "tools").mkdir()
    shutil.copy(SCRIPT, work / "tools" / "deploy.sh")
    shutil.copy(ROOT / "tools" / "deploy_temp.py", work / "tools" / "deploy_temp.py")
    (work / "mistral.json").write_text(json.dumps(MANIFEST, indent=2) + "\n")
    (work / ".gitignore").write_text(".env\n/mistral.deployment.json\n")
    repo.git("add", ".")
    repo.git("commit", "--quiet", "-m", "app")
    repo.git("push", "--quiet", "origin", "HEAD:refs/heads/main")
    (work / ".env").write_text("TOKEN=test-only\n")
    (work / "mistral.deployment.json").write_text(json.dumps(IDS) + "\n")
    return repo


def test_deploys_a_clean_copy_of_the_commit_without_yes(repo: Repo) -> None:
    sha = repo.git("rev-parse", "HEAD")
    manifest_before = (repo.work / "mistral.json").read_text()

    result = repo.deploy(sha)

    assert result.returncode == 0, result.stderr
    assert f"Deployed commit {sha}." in result.stdout
    listing, deploy = repo.calls()
    assert listing["args"] == ["apps", "list", "--organization", ORGANIZATION, "--workspace", WORKSPACE, "--json"]
    assert deploy["args"] == ["deploy", "--from-disk"]
    copy = Path(str(deploy["cwd"]))
    assert copy.is_relative_to(repo.tmpdir)
    assert not copy.exists() and list(repo.tmpdir.iterdir()) == []
    assert deploy["manifest"] == {**MANIFEST, "deployment": IDS}
    assert list(deploy["manifest"]) == ["schemaVersion", "kind", "name", "deployment", "modules"]
    assert deploy["files"] == ["./.gitignore", "./mistral.json", "./tools/deploy.sh", "./tools/deploy_temp.py"]
    assert deploy["push"] == ["refused"]
    assert repo.git("ls-remote", str(repo.origin)).count("refs/heads/") == 1
    assert (repo.work / "mistral.json").read_text() == manifest_before
    assert repo.git("status", "--porcelain") == ""
    for value in IDS.values():
        assert value not in result.stdout + result.stderr


def test_unset_tmpdir_uses_private_cache_removed_after_deploy(repo: Repo) -> None:
    del repo.env["TMPDIR"]

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode == 0, result.stderr
    listing, deploy = repo.calls()
    staging = deploy["tmpdir"]
    assert staging.parent == Path(repo.env["HOME"]) / ".cache" / "liam-e"
    assert listing["tmpdir"] == staging and listing["tmpmode"] == deploy["tmpmode"] == "0o700"
    assert Path(deploy["cwd"]).parent == staging
    assert not staging.exists()


def test_git_above_explicit_tmpdir_stops_before_the_cli(repo: Repo) -> None:
    outer = repo.tmpdir / "outer"
    requested = outer / "nested"
    requested.mkdir(parents=True)
    marker = outer / ".git"
    marker.mkdir()
    repo.env["TMPDIR"] = str(requested)

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode == 1
    assert "a .git sits in a parent directory" in result.stderr and "TMPDIR" in result.stderr
    assert str(requested) not in result.stderr
    assert repo.calls() == []
    assert list(requested.iterdir()) == []
    assert marker.is_dir()


def test_missing_deployment_file_stops_before_the_cli(repo: Repo) -> None:
    (repo.work / "mistral.deployment.json").unlink()

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode != 0
    assert "no deployment file" in result.stderr
    assert repo.calls() == []


def test_deployment_file_can_live_outside_the_checkout(repo: Repo) -> None:
    elsewhere = repo.tmpdir.parent / "ids.json"
    (repo.work / "mistral.deployment.json").rename(elsewhere)
    repo.env["LIAM_E_DEPLOYMENT_FILE"] = str(elsewhere)

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode == 0, result.stderr
    assert [call["args"][0] for call in repo.calls()] == ["apps", "deploy"]


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"organizationId": "<organization-id>", "workspaceId": "<workspace-id>", "appId": "<app-id>"}),
        json.dumps({**IDS, "extra": "x"}),
        json.dumps({key: value for key, value in IDS.items() if key != "appId"}),
        "not json",
    ],
)
def test_invalid_deployment_file_stops_before_the_cli(repo: Repo, content: str) -> None:
    (repo.work / "mistral.deployment.json").write_text(content)

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode != 0
    assert "deployment file" in result.stderr
    assert "<app-id>" not in result.stderr
    assert repo.calls() == []


def test_commit_not_on_origin_main_stops_before_the_cli(repo: Repo) -> None:
    (repo.work / "local.txt").write_text("not pushed\n")
    repo.git("add", "local.txt")
    repo.git("commit", "--quiet", "-m", "local only")

    result = repo.deploy("HEAD")

    assert result.returncode != 0
    assert "is not on origin/main" in result.stderr
    assert repo.calls() == []
    assert list(repo.tmpdir.iterdir()) == []


def test_unknown_target_app_stops_before_deploy(repo: Repo) -> None:
    repo.env["FAKE_APPS_JSON"] = json.dumps({"apps": [{"id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}]})

    result = repo.deploy(repo.git("rev-parse", "HEAD"))

    assert result.returncode != 0
    assert "target app was not found" in result.stderr
    assert [call["args"][:2] for call in repo.calls()] == [["apps", "list"]]
    assert list(repo.tmpdir.iterdir()) == []


def test_usage_without_a_commit(repo: Repo) -> None:
    result = repo.deploy()

    assert result.returncode == 64
    assert "usage:" in result.stderr
    assert repo.calls() == []
