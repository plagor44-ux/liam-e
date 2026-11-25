#!/usr/bin/env bash
# Deploy or delete the synthetic MCP-route probe app (#157). Never liam-e itself.
#
#   bash tools/probes/mcp_route_app/deploy.sh <commit>   # create or update the probe app
#   bash tools/probes/mcp_route_app/deploy.sh --delete   # delete it after the probe
#
# Each run needs the owner's approval for the live probe (docs/superpowers/probes/
# 2026-11-25-mcp-route-sign-in.md). The Mistral IDs never enter the repository. They live in the
# git-ignored file tools/probes/mcp_route_app/mistral.deployment.json, or in the file that
# MCP_ROUTE_PROBE_DEPLOYMENT_FILE names. Before the first deploy it holds the target organization
# and workspace (see mistral.deployment.example.json):
#
#   {"organizationId": "<organization-id>", "workspaceId": "<workspace-id>"}
#
# The first deploy creates the probe app; the CLI asks before it creates anything, because this
# script never passes --yes. Afterwards the script adds the new "appId" to the file, so later runs
# update the same app and --delete can remove it. It refuses an app ID that equals the one in
# liam-e's own deployment file (mistral.deployment.json at the checkout root, or
# LIAM_E_DEPLOYMENT_FILE).
#
# Steps: require the commit to be on origin/main; export the probe directory's tracked files at
# that commit into a fresh temporary directory (no .env, no ignored files, no Git metadata); write
# the deployment block into that copy's mistral.json only; run `mistral deploy --from-disk` there;
# remove the copy. The script never prints the IDs. The CLI's own output may show the target and the
# app URL; do not copy those into the probe log.
set -euo pipefail

PROBE_DIR="tools/probes/mcp_route_app"
PROBE_NAME="liam-e-mcp-route-probe"

fail() {
  echo "probe deploy: $*" >&2
  exit 1
}

usage() {
  echo "usage: bash $PROBE_DIR/deploy.sh <commit> | --delete" >&2
  exit 64
}

[ "$#" -eq 1 ] || usage
root="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
ids_file="${MCP_ROUTE_PROBE_DEPLOYMENT_FILE:-$root/$PROBE_DIR/mistral.deployment.json}"
liam_file="${LIAM_E_DEPLOYMENT_FILE:-$root/mistral.deployment.json}"
[ -f "$ids_file" ] || fail "no deployment file at $ids_file; copy mistral.deployment.example.json there and fill in the IDs"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v mistral >/dev/null 2>&1 || fail "the mistral CLI is not on PATH"

# Validate the file and print organization, workspace and app ID (empty before the first deploy).
# Errors name the rule, never a value.
ids="$(python3 -I - "$ids_file" "$liam_file" <<'PY'
import json
import re
import sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQUIRED = ("organizationId", "workspaceId")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    sys.exit("probe deploy: the deployment file is not readable JSON")
if not isinstance(data, dict) or not set(REQUIRED) <= set(data) <= {*REQUIRED, "appId"}:
    sys.exit("probe deploy: the deployment file must hold organizationId and workspaceId, and appId after the first deploy")
if not all(isinstance(value, str) and UUID.fullmatch(value) for value in data.values()):
    sys.exit("probe deploy: each value in the deployment file must be a lowercase UUID")
try:
    with open(sys.argv[2], encoding="utf-8") as handle:
        liam = json.load(handle)
except (OSError, ValueError):
    liam = {}
if isinstance(liam, dict) and data.get("appId") and data.get("appId") == liam.get("appId"):
    sys.exit("probe deploy: the app ID is liam-e's own app; the probe never deploys to it")
print(data["organizationId"])
print(data["workspaceId"])
print(data.get("appId", "none"))
PY
)"
{ read -r organization; read -r workspace; read -r app_id; } <<<"$ids"
[ "$app_id" != none ] || app_id=""

work="$(mktemp -d "${TMPDIR:-/tmp}/liam-e-probe-deploy.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# True if `mistral apps list --json` shows an object that carries both the app ID and the probe name.
probe_listed() {
  (cd "$work" && mistral apps list --organization "$organization" --workspace "$workspace" --json >"$work/apps.json") ||
    fail "could not list the apps of the target workspace; run 'mistral login' and retry"
  python3 -I - "$work/apps.json" "$app_id" "$PROBE_NAME" <<'PY'
import json
import sys

path, app_id, name = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        listing = json.load(handle)
except ValueError:
    sys.exit("probe deploy: 'mistral apps list --json' did not return JSON")


def match(node):
    if isinstance(node, dict):
        own = [v for v in node.values() if not isinstance(v, (dict, list))]
        if app_id in own and name in own:
            return True
        return any(match(v) for v in node.values())
    if isinstance(node, list):
        return any(match(v) for v in node)
    return False


sys.exit(0 if match(listing) else 1)
PY
}

if [ "$1" = "--delete" ]; then
  [ -n "$app_id" ] || fail "the deployment file names no appId, so there is no probe app to delete"
  probe_listed || fail "no app with the recorded ID and the name $PROBE_NAME in the target workspace; nothing was deleted"
  # Without --yes the CLI asks to type the app's name before it deletes anything.
  (cd "$work" && mistral apps delete "$app_id" --organization "$organization" --workspace "$workspace") ||
    fail "'mistral apps delete' did not finish; check the workspace by hand"
  python3 -I - "$ids_file" <<'PY'
import json
import os
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
data.pop("appId", None)
temporary = f"{path}.tmp"
with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(temporary, path)
PY
  echo "Deleted the probe app and removed its appId from the deployment file."
  exit 0
fi

case "$1" in -*) usage ;; esac
git -C "$root" fetch --quiet origin main || fail "could not fetch main from origin"
sha="$(git -C "$root" rev-parse --verify --quiet "$1^{commit}")" || fail "unknown commit: $1"
git -C "$root" merge-base --is-ancestor "$sha" refs/remotes/origin/main ||
  fail "commit $sha is not on origin/main; push and merge it first"
git -C "$root" cat-file -e "$sha:$PROBE_DIR/mistral.json" 2>/dev/null || fail "commit $sha has no probe app"

# Tracked files of the probe directory at that commit, nothing else.
mkdir "$work/export"
git -C "$root" archive --format=tar "$sha" "$PROBE_DIR" | tar -x -C "$work/export"
copy="$work/app"
mv "$work/export/$PROBE_DIR" "$copy"
[ ! -e "$copy/mistral.deployment.json" ] || fail "the exported copy holds a deployment file"

if [ -n "$app_id" ]; then
  probe_listed || fail "no app with the recorded ID and the name $PROBE_NAME in the target workspace; nothing was deployed"
  python3 -I - "$copy/mistral.json" "$ids_file" <<'PY'
import json
import sys

manifest_path, ids_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
with open(ids_path, encoding="utf-8") as handle:
    deployment = json.load(handle)
result = {}
for key, value in manifest.items():
    if key == "deployment":
        continue
    result[key] = value
    if key == "name":
        result["deployment"] = deployment
with open(manifest_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
PY
  deploy_args=()
else
  echo "No appId recorded yet: this deploy creates the probe app. The CLI asks before it creates it."
  deploy_args=(--organization "$organization" --workspace "$workspace")
fi

echo "Deploying the probe app from commit $sha."
status=0
(cd "$copy" && mistral deploy --from-disk "${deploy_args[@]}") || status=$?
if [ "$status" -ne 0 ]; then
  echo "probe deploy: 'mistral deploy --from-disk' exited with $status for commit $sha" >&2
  exit "$status"
fi

if [ -z "$app_id" ]; then
  # Keep the new app's ID for later runs. The CLI writes the deployment block into the copy.
  python3 -I - "$copy/mistral.json" "$ids_file" <<'PY' || fail "deployed, but the new appId could not be recorded; add it to the deployment file by hand from 'mistral apps list'"
import json
import os
import re
import sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
manifest_path, ids_path = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    block = json.load(handle).get("deployment") or {}
with open(ids_path, encoding="utf-8") as handle:
    data = json.load(handle)
app_id = block.get("appId")
if not (isinstance(app_id, str) and UUID.fullmatch(app_id)):
    sys.exit("probe deploy: the CLI recorded no app ID in the copy")
if block.get("organizationId") != data["organizationId"] or block.get("workspaceId", data["workspaceId"]) != data["workspaceId"]:
    sys.exit("probe deploy: the CLI deployed to another organization or workspace than the file names")
data["appId"] = app_id
temporary = f"{ids_path}.tmp"
with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(temporary, ids_path)
PY
  echo "Recorded the new probe app's ID in the deployment file."
fi
echo "Deployed the probe app from commit $sha."
