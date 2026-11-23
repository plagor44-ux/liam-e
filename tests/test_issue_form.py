"""Offline checks for tools/issue_form.py against the real issue forms and a fake `gh` CLI."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "issue_form.py"
FORMS = sorted(path.stem for path in (ROOT / ".github" / "ISSUE_TEMPLATE").glob("[0-9]*.yml"))

FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" >"$FAKE_GH_LOG"
if [ "$1 $2" = "issue view" ]; then
  printf '%s' "$FAKE_GH_VIEW"
else
  echo https://github.com/example/example/issues/1
fi
"""


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60
    )


def form(stem: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / f"{stem}.yml").read_text())


def filled(stem: str) -> str:
    """A body in the browser's shape with every field answered with synthetic text."""
    parts = []
    for field in form(stem)["body"]:
        if field["type"] == "markdown":
            continue
        attributes = field["attributes"]
        if field["type"] == "checkboxes":
            value = "\n".join(f"- [{'X' if i == 0 else ' '}] {o['label']}" for i, o in enumerate(attributes["options"]))
        elif field["type"] == "dropdown":
            value = attributes["options"][0]
        elif field["type"] == "input":
            value = "#10"
        else:
            value = "Synthetic text."
        parts.append(f"### {attributes['label']}\n\n{value}")
    return "\n\n".join(parts) + "\n"


@pytest.fixture
def gh(tmp_path: Path) -> dict[str, str]:
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
        "FAKE_GH_VIEW": "{}",
    }


@pytest.mark.parametrize("stem", FORMS)
def test_a_filled_body_passes_and_the_skeleton_does_not(stem: str, tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(filled(stem))
    assert run("check", stem, str(body)).returncode == 0

    body.write_text(run("skeleton", stem).stdout)
    result = run("check", stem, str(body))
    assert result.returncode == 1
    assert "remove the hint comments" in result.stderr
    required = [f for f in form(stem)["body"] if f.get("validations", {}).get("required")]
    assert all(f"{f['attributes']['label']}: required" in result.stderr for f in required)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda body: "Intro line.\n\n" + body, "text before the first section"),
        (lambda body: body.replace("### Proposal", "### Suggestion"), "sections must be exactly"),
        (lambda body: body + "\n### Notes\n\nExtra.\n", "sections must be exactly"),
        (lambda body: body.replace("### Acceptance criteria\n\nSynthetic text.", "### Acceptance criteria\n\n_No response_"), "Acceptance criteria: required"),
        (lambda body: body.replace("- [ ] Tables (generated migration)\n", ""), "Affected contracts: list every option"),
    ],
)
def test_change_form_rejects_bodies_that_drift_from_the_form(change, message: str, tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(change(filled("02-change")))

    result = run("check", "02-change", str(body))

    assert result.returncode == 1
    assert message in result.stderr


def test_headings_inside_code_fences_are_not_sections(tmp_path: Path) -> None:
    fenced = "Synthetic text.\n\n```markdown\n### Proposal\n```\n\n~~~~\n### Out of scope\n~~~~"
    body = tmp_path / "body.md"
    body.write_text(filled("02-change").replace("### Problem\n\nSynthetic text.", "### Problem\n\n" + fenced, 1))

    result = run("check", "02-change", str(body))

    assert result.returncode == 0, result.stderr


def test_bug_form_rejects_an_unknown_area_and_a_multi_line_version(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(filled("01-bug").replace("UI (apps/web)", "Somewhere").replace("### Version\n\n#10", "### Version\n\n#10\nsecond line"))

    result = run("check", "01-bug", str(body))

    assert result.returncode == 1
    assert "Area: one of" in result.stderr
    assert "Version: a single line" in result.stderr


def test_create_passes_title_body_and_the_form_labels_to_gh(gh: dict[str, str], tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text(filled("01-bug"))

    result = run("create", "01-bug", "--title", "Inbox: example", "--body-file", str(body), env=gh)

    assert result.returncode == 0, result.stderr
    args = Path(gh["FAKE_GH_LOG"]).read_text().splitlines()
    assert args == ["issue", "create", "--title", "Inbox: example", "--body-file", str(body), "--label", "bug"]


def test_create_does_not_call_gh_when_the_body_fails(gh: dict[str, str], tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("Free text without the form's sections.\n")

    result = run("create", "02-change", "--title", "Example", "--body-file", str(body), env=gh)

    assert result.returncode == 1
    assert not Path(gh["FAKE_GH_LOG"]).exists()


def test_verify_finds_the_form_and_requires_its_label(gh: dict[str, str]) -> None:
    gh["FAKE_GH_VIEW"] = json.dumps({"body": filled("04-subtask"), "labels": [{"name": "enhancement"}]})
    ok = run("verify", "7", env=gh)
    assert ok.returncode == 0, ok.stderr
    assert "#7 follows 04-subtask" in ok.stdout

    gh["FAKE_GH_VIEW"] = json.dumps({"body": filled("04-subtask"), "labels": []})
    unlabeled = run("verify", "7", env=gh)
    assert unlabeled.returncode == 1
    assert "missing label(s): enhancement" in unlabeled.stderr

    gh["FAKE_GH_VIEW"] = json.dumps({"body": "## Goal\n\nFree text.\n", "labels": [{"name": "enhancement"}]})
    freeform = run("verify", "7", env=gh)
    assert freeform.returncode == 1
    assert "match no form" in freeform.stderr
