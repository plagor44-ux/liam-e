"""Create and check GitHub issues that follow the forms in .github/ISSUE_TEMPLATE/.

GitHub enforces the forms only in the browser (`blank_issues_enabled: false`). The REST API and
`gh issue create` accept any title, body and labels, and gh cannot fill in an issue form at all:
its `--template` flag only works interactively, and its template list comes from the GraphQL
field `issueTemplates`, which lists Markdown templates but no YAML forms. This script builds and
checks a body in the shape the browser produces for a form and creates the issue with the form's
labels.

  bash tools/uv.sh run --no-sync python tools/issue_form.py forms
  bash tools/uv.sh run --no-sync python tools/issue_form.py skeleton 02-change > body.md
  bash tools/uv.sh run --no-sync python tools/issue_form.py check 02-change body.md
  bash tools/uv.sh run --no-sync python tools/issue_form.py create 02-change --title "..." --body-file body.md
  bash tools/uv.sh run --no-sync python tools/issue_form.py verify 97

A body holds one `### <label>` section per form field, in the form's order, and nothing else.
An empty optional field reads `_No response_`. Checkboxes list every option as `- [ ]` or `- [X]`.
A dropdown holds exactly one of its options and an input holds a single line. Headings inside a
section use `####` or lower, and `###` lines inside fenced code blocks are ignored.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

FORMS = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"
NO_RESPONSE = "_No response_"
HINT = "<!-- form:"
HINT_LINE = re.compile(r"^<!-- form:.*-->$\n?", re.MULTILINE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
CHECKBOX = re.compile(r"^- \[([ xX])\] (.+)$")


def load_forms() -> dict[str, dict[str, Any]]:
    """All issue forms by file stem, for example `02-change`."""
    return {path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(FORMS.glob("[0-9]*.yml"))}


def resolve(name: str) -> tuple[str, dict[str, Any]]:
    forms = load_forms()
    stem = name.removesuffix(".yml")
    if stem in forms:
        return stem, forms[stem]
    sys.exit(f"issue_form: unknown form {name!r}; choose one of {', '.join(forms)}")


def fields(form: dict[str, Any]) -> list[dict[str, Any]]:
    return [field for field in form["body"] if field["type"] != "markdown"]


def required(field: dict[str, Any]) -> bool:
    return bool(field.get("validations", {}).get("required", False))


def split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Text before the first `###` heading and the (label, value) sections, ignoring code fences."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence = ""
    for line in body.replace("\r\n", "\n").split("\n"):
        match = FENCE.match(line)
        if fence:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = ""
        elif match:
            fence = match.group(1)
        elif line.startswith("### "):
            sections.append((line[4:].strip(), []))
            continue
        (sections[-1][1] if sections else preamble).append(line)
    return "\n".join(preamble).strip(), [(label, "\n".join(lines).strip()) for label, lines in sections]


def check(form: dict[str, Any], body: str) -> list[str]:
    """Problems that keep `body` from matching `form`; an empty list means it matches."""
    problems: list[str] = []
    if HINT in body:
        problems.append("remove the hint comments from the skeleton")
    preamble, sections = split_sections(HINT_LINE.sub("", body))
    if preamble:
        problems.append("text before the first section; every line belongs to a form section")
    expected = [field["attributes"]["label"] for field in fields(form)]
    found = [label for label, _ in sections]
    if found != expected:
        problems.append(f"sections must be exactly, in this order: {expected}; found {found}")
        return problems
    for field, (label, value) in zip(fields(form), sections):
        empty = value in ("", NO_RESPONSE)
        kind = field["type"]
        if kind == "checkboxes":
            options = [option["label"] for option in field["attributes"]["options"]]
            listed = [CHECKBOX.match(line) for line in value.split("\n")] if value else []
            if not all(listed) or [match.group(2) for match in listed if match] != options:
                problems.append(f"{label}: list every option once as '- [ ] <option>' or '- [X] <option>', in order")
            continue
        if not value:
            problems.append(f"{label}: write {NO_RESPONSE} for an empty field")
        elif empty and required(field):
            problems.append(f"{label}: required")
        elif empty:
            continue
        elif kind == "input" and "\n" in value:
            problems.append(f"{label}: a single line")
        elif kind == "dropdown" and value not in field["attributes"]["options"]:
            problems.append(f"{label}: one of {field['attributes']['options']}")
    return problems


def skeleton(form: dict[str, Any]) -> str:
    parts = []
    for field in fields(form):
        attributes = field["attributes"]
        hint = "required" if required(field) else "optional"
        if attributes.get("description"):
            hint += f"; {attributes['description']}"
        if field["type"] == "dropdown":
            hint += f"; one of: {' | '.join(attributes['options'])}"
        if field["type"] == "checkboxes":
            value = "\n".join(f"- [ ] {option['label']}" for option in attributes["options"])
            hint += "; mark what applies with [X]"
        else:
            value = NO_RESPONSE
        parts.append(f"### {attributes['label']}\n\n{HINT} {hint} -->\n{value}")
    return "\n\n".join(parts) + "\n"


def matching_form(body: str) -> tuple[str, dict[str, Any]] | None:
    labels = [label for label, _ in split_sections(body)[1]]
    for stem, form in load_forms().items():
        if labels == [field["attributes"]["label"] for field in fields(form)]:
            return stem, form
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("forms", help="list the forms, their labels and sections")
    commands.add_parser("skeleton", help="print a body to fill in").add_argument("form")
    check_parser = commands.add_parser("check", help="check a body file against a form")
    check_parser.add_argument("form")
    check_parser.add_argument("body_file", type=Path)
    create_parser = commands.add_parser("create", help="check the body, then run gh issue create with the form's labels")
    create_parser.add_argument("form")
    create_parser.add_argument("--title", required=True)
    create_parser.add_argument("--body-file", required=True, type=Path)
    create_parser.add_argument("--dry-run", action="store_true", help="print the gh command instead of running it")
    commands.add_parser("verify", help="check an existing issue's sections and labels").add_argument("number", type=int)
    args = parser.parse_args(argv)

    if args.command == "forms":
        for stem, form in load_forms().items():
            print(f"{stem}: {form['name']} (labels: {', '.join(form.get('labels', []))})")
            for field in fields(form):
                print(f"  ### {field['attributes']['label']}{'' if required(field) else '  (optional)'}")
        return 0
    if args.command == "skeleton":
        print(skeleton(resolve(args.form)[1]), end="")
        return 0
    if args.command == "verify":
        view = subprocess.run(
            ["gh", "issue", "view", str(args.number), "--json", "body,labels"], capture_output=True, text=True, check=True
        )
        issue = json.loads(view.stdout)
        found = matching_form(issue["body"] or "")
        if not found:
            print(f"#{args.number}: its sections match no form", file=sys.stderr)
            return 1
        stem, form = found
        problems = check(form, issue["body"])
        missing = sorted(set(form.get("labels", [])) - {label["name"] for label in issue["labels"]})
        if missing:
            problems.append(f"missing label(s): {', '.join(missing)}")
        for problem in problems:
            print(f"#{args.number} ({stem}): {problem}", file=sys.stderr)
        if not problems:
            print(f"#{args.number} follows {stem} ({form['name']})")
        return 1 if problems else 0

    stem, form = resolve(args.form)
    problems = check(form, args.body_file.read_text(encoding="utf-8"))
    for problem in problems:
        print(f"{stem}: {problem}", file=sys.stderr)
    if problems:
        return 1
    if args.command == "check":
        print(f"{args.body_file} follows {stem} ({form['name']})")
        return 0
    if not args.title.strip():
        print("issue_form: the title is empty", file=sys.stderr)
        return 1
    command = ["gh", "issue", "create", "--title", args.title, "--body-file", str(args.body_file)]
    for label in form.get("labels", []):
        command += ["--label", label]
    if args.dry_run:
        print(shlex.join(command))
        return 0
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
