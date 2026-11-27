#!/usr/bin/env bash
# Deploy a commit from origin/main to liam-e's existing Mistral Studio app.
#
#   bash tools/deploy.sh <commit>
#
# The Mistral organization, workspace and app IDs never enter the repository. They live in the
# git-ignored file mistral.deployment.json at the checkout root, or in the file that
# LIAM_E_DEPLOYMENT_FILE names (for example from a linked worktree). It holds exactly the keys of
# the `deployment` block that `mistral deploy` reads from mistral.json:
#
#   {"organizationId": "<organization-id>", "workspaceId": "<workspace-id>", "appId": "<app-id>"}
#
# Without that file the script stops: a deploy without the block would create a new app.
#
# Steps: fetch origin/main and require the commit to be on it; clone the commit into a fresh
# private staging directory outside any Git tree (tracked files only, so no .env and no ignored
# files); write the deployment block into that copy's mistral.json only; confirm with
# `mistral apps list` that the target app exists; run `mistral deploy --from-disk` in the copy;
# print the deployed commit; remove the copy. The script never passes --yes, so the CLI asks before
# it creates, commits or pushes anything, and the copy has no push URL. `--from-disk` is
# experimental in the CLI; docs/publishing.md describes the way back to a plain Git deploy.
#
# The script never prints the IDs. The CLI's own output may show the target.
set -euo pipefail

fail() {
  echo "deploy: $*" >&2
  exit 1
}

[ "$#" -eq 1 ] || {
  echo "usage: bash tools/deploy.sh <commit>" >&2
  exit 64
}

root="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
ids_file="${LIAM_E_DEPLOYMENT_FILE:-$root/mistral.deployment.json}"
[ -f "$ids_file" ] || fail "no deployment file at $ids_file. Create it as described in docs/providers-and-operations.md; without it a deploy would create a new app."
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v mistral >/dev/null 2>&1 || fail "the mistral CLI is not on PATH"

# Validate the deployment file and read the organization and workspace for the app check. Errors
# name the rule, never a value from the file.
ids="$(python3 -I - "$ids_file" <<'PY'
import json
import re
import sys

KEYS = ("organizationId", "workspaceId", "appId")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    sys.exit("deploy: the deployment file is not readable JSON")
if not isinstance(data, dict) or sorted(data) != sorted(KEYS):
    sys.exit("deploy: the deployment file must hold exactly organizationId, workspaceId and appId")
if not all(isinstance(data[key], str) and UUID.fullmatch(data[key]) for key in KEYS):
    sys.exit("deploy: each value in the deployment file must be a lowercase UUID")
print(data["organizationId"])
print(data["workspaceId"])
PY
)"
organization="${ids%%$'\n'*}"
workspace="${ids#*$'\n'}"

work="$(python3 -I "$root/tools/deploy_temp.py" liam-e-deploy)"
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export TMPDIR="$work"

git -C "$root" fetch --quiet origin main || fail "could not fetch main from origin"
sha="$(git -C "$root" rev-parse --verify --quiet "$1^{commit}")" || fail "unknown commit: $1"
git -C "$root" merge-base --is-ancestor "$sha" refs/remotes/origin/main ||
  fail "commit $sha is not on origin/main; push and merge it first"

copy="$work/liam-e"

# A separate clone, not a worktree: nothing is registered in the source repository. Fetching the
# source's origin/main makes the commit available even when no local branch contains it. Hooks are
# off in the copy, including global ones.
git clone --quiet --no-checkout --origin source --config core.hooksPath=/dev/null -- "$root" "$copy"
git -C "$copy" fetch --quiet source "+refs/remotes/origin/main:refs/remotes/source/origin-main"
git -C "$copy" checkout --quiet --detach "$sha"
git -C "$copy" remote remove source
# The copy looks like a normal checkout of origin but cannot push: if the CLI ever offers to
# commit and push mistral.json, the push fails instead of publishing the IDs.
git -C "$copy" remote add origin "$(git -C "$root" remote get-url origin)"
git -C "$copy" remote set-url --push origin no-push-from-deploy-copy
[ -z "$(git -C "$copy" status --porcelain --ignored)" ] || fail "the fresh copy is not clean"

python3 -I - "$copy/mistral.json" "$ids_file" <<'PY'
import json
import sys

manifest_path, ids_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
with open(ids_path, encoding="utf-8") as handle:
    deployment = json.load(handle)
# Same position as the block the CLI writes: right after "name".
result = {}
for key, value in manifest.items():
    if key == "deployment":
        continue
    result[key] = value
    if key == "name":
        result["deployment"] = deployment
result.setdefault("deployment", deployment)
with open(manifest_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY

listing="$work/apps.json"
(cd "$copy" && mistral apps list --organization "$organization" --workspace "$workspace" --json >"$listing") ||
  fail "could not list the apps of the target workspace; run 'mistral login' and retry"
python3 -I - "$listing" "$ids_file" <<'PY' || fail "the target app was not found in the target workspace; nothing was deployed"
import json
import sys

with open(sys.argv[2], encoding="utf-8") as handle:
    app_id = json.load(handle)["appId"]
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        listing = json.load(handle)
except ValueError:
    sys.exit("deploy: 'mistral apps list --json' did not return JSON")


def contains(value):
    if isinstance(value, dict):
        return any(contains(item) for item in value.values())
    if isinstance(value, list):
        return any(contains(item) for item in value)
    return value == app_id


sys.exit(0 if contains(listing) else 1)
PY

echo "Deploying commit $sha from a clean copy of origin/main."
status=0
(cd "$copy" && mistral deploy --from-disk) || status=$?
if [ "$status" -ne 0 ]; then
  echo "deploy: 'mistral deploy --from-disk' exited with $status for commit $sha" >&2
  exit "$status"
fi
echo "Deployed commit $sha."
