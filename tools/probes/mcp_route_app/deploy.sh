#!/usr/bin/env bash
# Deploy or delete the synthetic MCP-route probe app (#157). Never liam-e itself.
#
#   bash tools/probes/mcp_route_app/deploy.sh <commit> --create-app liam-e-mcp-route-probe   # first deploy
#   bash tools/probes/mcp_route_app/deploy.sh <commit>                                       # update the app
#   bash tools/probes/mcp_route_app/deploy.sh --delete                                       # dry run
#   bash tools/probes/mcp_route_app/deploy.sh --delete --apply                               # delete it
#
# Each run needs the owner's approval for the live probe (docs/superpowers/probes/
# 2026-11-25-mcp-route-sign-in.md). The Mistral IDs never enter the repository. They live in the
# git-ignored file tools/probes/mcp_route_app/mistral.deployment.json, or in the file that
# MCP_ROUTE_PROBE_DEPLOYMENT_FILE names. Before the first deploy it holds the target organization
# and workspace (see mistral.deployment.example.json):
#
#   {"organizationId": "<organization-id>", "workspaceId": "<workspace-id>"}
#
# The CLI (checked with v0.18.1) creates a new app without asking, and without --name it names the
# app after the directory it runs in. So the confirmation happens here, before the CLI runs: a first
# deploy needs `--create-app liam-e-mcp-route-probe`, spelled exactly, and the script passes that
# name to the CLI as --name. Before it creates anything, it lists the target workspace read-only and
# refuses if an app of that name already exists. Afterwards it adds the new "appId" to the file and
# lists the workspace again, so later runs update the same app and --delete can remove it. An
# update refuses --create-app and needs the recorded app to be the only app of that name.
#
# liam-e's own apps are never a target. The script refuses an app ID that any liam-e deployment file
# names: mistral.deployment.json at the checkout root, the file LIAM_E_DEPLOYMENT_FILE names, every
# file in the colon-separated list MCP_ROUTE_PROBE_LIAM_E_FILES, and every *.json file in liam-e's
# config directory, ${XDG_CONFIG_HOME:-$HOME/.config}/liam-e (for example the deployment files of
# liam-e's test and production apps). It stops if none of them names an app ID, because then it
# could not tell liam-e's apps apart.
#
# Steps: refuse a temporary directory with a .git entry in it or in any parent (#190); require the
# commit to be on origin/main; export the probe directory's tracked files at that commit into a
# fresh temporary directory (no .env, no ignored files, no Git metadata); write
# the deployment block into that copy's mistral.json only; run `mistral deploy --from-disk` there;
# remove the copy. The script never prints the IDs. The CLI's own output may show the target and the
# app URL; do not copy those into the probe log.
#
# --delete is the emergency stop of the L9 kill-switch test, so it guards harder than a deploy:
#   - it reads the app ID only from tools/probes/mcp_route_app/mistral.deployment.json and refuses
#     MCP_ROUTE_PROBE_DEPLOYMENT_FILE;
#   - liam-e's config directory must name at least one app ID, and the recorded ID must be none of
#     the protected IDs;
#   - the workspace listing must show the recorded app under the exact name liam-e-mcp-route-probe,
#     and no other app may carry that name or label.
# Without --apply it stops there (dry run). With --apply it runs `mistral apps delete --yes` (the
# script has asked already, and the CLI needs --yes off a terminal), lists the workspace again, and
# removes the appId from the deployment file only once the listing shows no app of the probe's ID
# or name. Last it checks that the liam-e apps listed before the delete are still listed with the
# same name, label and updatedAt. It prints counts, never IDs, and keeps the CLI's own output to
# itself.
set -euo pipefail

PROBE_DIR="tools/probes/mcp_route_app"
PROBE_NAME="liam-e-mcp-route-probe"

fail() {
  echo "probe deploy: $*" >&2
  exit 1
}

usage() {
  echo "usage: bash $PROBE_DIR/deploy.sh <commit> [--create-app $PROBE_NAME] | --delete [--apply]" >&2
  exit 64
}

mode=deploy
commit=""
create_given=0
create_name=""
apply=0
case "${1:-}" in
  "") usage ;;
  --delete)
    mode=delete
    case "$#:${2:-}" in
      1:) ;;
      2:--apply) apply=1 ;;
      *) usage ;;
    esac
    ;;
  -*) usage ;;
  *)
    commit="$1"
    shift
    if [ "$#" -gt 0 ]; then
      { [ "$#" -eq 2 ] && [ "$1" = "--create-app" ]; } || usage
      create_given=1
      create_name="$2"
    fi
    ;;
esac

root="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
if [ "$mode" = delete ] && [ -n "${MCP_ROUTE_PROBE_DEPLOYMENT_FILE:-}" ]; then
  fail "--delete reads the app ID only from $PROBE_DIR/mistral.deployment.json; unset MCP_ROUTE_PROBE_DEPLOYMENT_FILE"
fi
ids_file="${MCP_ROUTE_PROBE_DEPLOYMENT_FILE:-$root/$PROBE_DIR/mistral.deployment.json}"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/liam-e"
[ -f "$ids_file" ] || fail "no deployment file at $ids_file; copy mistral.deployment.example.json there and fill in the IDs"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v mistral >/dev/null 2>&1 || fail "the mistral CLI is not on PATH"

# Validate the file, collect liam-e's app IDs and print organization, workspace, app ID ("none"
# before the first deploy) and the protected app IDs. Errors name the rule, never a value.
ids="$(python3 -I - "$mode" "$ids_file" "$root/mistral.deployment.json" "${LIAM_E_DEPLOYMENT_FILE:-}" "${MCP_ROUTE_PROBE_LIAM_E_FILES:-}" "$config_dir" <<'PY'
import json
import pathlib
import re
import sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQUIRED = ("organizationId", "workspaceId")
mode, ids_path, root_file, named_file, listed_files, config_dir = sys.argv[1:]
try:
    with open(ids_path, encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    sys.exit("probe deploy: the deployment file is not readable JSON")
if not isinstance(data, dict) or not set(REQUIRED) <= set(data) <= {*REQUIRED, "appId"}:
    sys.exit("probe deploy: the deployment file must hold organizationId and workspaceId, and appId after the first deploy")
if not all(isinstance(value, str) and UUID.fullmatch(value) for value in data.values()):
    sys.exit("probe deploy: each value in the deployment file must be a lowercase UUID")


def liam_app_id(path: str, required: bool) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            liam = json.load(handle)
    except OSError:
        if required:
            sys.exit("probe deploy: a liam-e deployment file named in the environment or kept in the liam-e config "
                     "directory is missing or unreadable")
        return None
    except ValueError:
        sys.exit("probe deploy: a liam-e deployment file is not readable JSON")
    value = liam.get("appId") if isinstance(liam, dict) else None
    return value.lower() if isinstance(value, str) and value else None


sources = [(root_file, False)]
if named_file:
    sources.append((named_file, True))
sources += [(path, True) for path in listed_files.split(":") if path]
config = pathlib.Path(config_dir)
config_files = sorted(str(path) for path in config.glob("*.json")) if config.is_dir() else []
from_config = {value for path in config_files if (value := liam_app_id(path, True))}
protected = from_config | {value for path, required in sources if (value := liam_app_id(path, required))}
if not protected:
    sys.exit("probe deploy: no liam-e deployment file names an app ID, so liam-e's apps cannot be ruled out; "
             "list liam-e's deployment files in MCP_ROUTE_PROBE_LIAM_E_FILES or keep them in the liam-e "
             "config directory")
if mode == "delete" and not from_config:
    sys.exit("probe deploy: the liam-e config directory (XDG_CONFIG_HOME/liam-e, by default $HOME/.config/liam-e) "
             "names no app ID; --delete compares the probe app with liam-e's test and production apps there")
if data.get("appId") in protected:
    sys.exit("probe deploy: the app ID is liam-e's own app; the probe never deploys to it")
print(data["organizationId"])
print(data["workspaceId"])
print(data.get("appId", "none"))
print(",".join(sorted(protected)))
PY
)"
{ read -r organization; read -r workspace; read -r app_id; read -r protected_ids; } <<<"$ids"
[ "$app_id" != none ] || app_id=""

# The explicit confirmation, checked before anything reaches the network or the CLI.
if [ "$mode" = deploy ]; then
  if [ -z "$app_id" ]; then
    [ "$create_given" -eq 1 ] ||
      fail "no appId is recorded, so this deploy would create the probe app; confirm with --create-app $PROBE_NAME"
    [ "$create_name" = "$PROBE_NAME" ] || fail "--create-app must name exactly $PROBE_NAME; nothing was created"
  else
    [ "$create_given" -eq 0 ] || fail "an appId is recorded, so this deploy updates that app; leave out --create-app"
  fi
elif [ -z "$app_id" ]; then
  fail "the deployment file names no appId, so there is no probe app to delete"
fi

if [ "$mode" = deploy ]; then
  work="$(python3 -I "$root/tools/deploy_temp.py" liam-e-probe-deploy)"
  export TMPDIR="$work"
else
  work="$(mktemp -d "${TMPDIR:-/tmp}/liam-e-probe-deploy.XXXXXX")"
fi
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Lists the target workspace read-only and checks it for the probe's name, as an app's name or
# label. "absent": no app may carry it. "recorded": the app with the given ID must carry it, and no
# other app may. "deletable": as "recorded", and the app's name must be exactly the probe's name.
# "gone": no app may have the given ID or carry the name.
check_listing() {
  local expectation="$1" expected_app="$2"
  (cd "$work" && mistral apps list --organization "$organization" --workspace "$workspace" --json >"$work/apps.json") ||
    fail "could not list the apps of the target workspace; check the CLI login and retry"
  python3 -I - "$work/apps.json" "$expectation" "$expected_app" "$PROBE_NAME" "$organization" "$workspace" <<'PY'
import json
import sys

path, expectation, app_id, name, organization, workspace = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as handle:
        listing = json.load(handle)
except ValueError:
    sys.exit("probe deploy: 'mistral apps list --json' did not return JSON")
apps = listing.get("apps") if isinstance(listing, dict) else None
if not isinstance(apps, list) or not all(isinstance(app, dict) for app in apps):
    sys.exit("probe deploy: 'mistral apps list --json' did not return an app list")
for key, expected in (("organization", organization), ("workspace", workspace)):
    scope = listing.get(key)
    if isinstance(scope, dict) and scope.get("id") not in (None, expected):
        sys.exit(f"probe deploy: the app list is for another {key} than the deployment file names")
named = [app for app in apps if name in (app.get("name"), app.get("label"))]
if expectation == "absent":
    if named:
        sys.exit(f"probe deploy: an app named {name} already exists in the target workspace; if it is the "
                 "probe app from an earlier run, record its appId in the deployment file and deploy without --create-app")
    sys.exit(0)
recorded = [app for app in apps if app.get("id") == app_id]
if expectation == "gone":
    if recorded or named:
        sys.exit(f"probe deploy: the app list still shows an app with the recorded ID or the name {name}")
    sys.exit(0)
if not recorded or recorded[0] not in named:
    sys.exit(f"probe deploy: no app with the recorded ID and the name {name} in the target workspace")
if any(app.get("id") != app_id for app in named):
    sys.exit(f"probe deploy: another app in the target workspace is also named {name}; check the workspace by hand")
if expectation == "deletable" and recorded[0].get("name") != name:
    sys.exit(f"probe deploy: the app with the recorded ID is not named exactly {name}")
PY
}

# Removes the appId from the deployment file, keeping its mode at 0600.
forget_app_id() {
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
}

if [ "$mode" = delete ]; then
  check_listing deletable "$app_id" || fail "nothing was deleted"
  cp "$work/apps.json" "$work/apps-before.json"
  if [ "$apply" -eq 0 ]; then
    echo "Dry run: the recorded app is the only app named $PROBE_NAME in the target workspace and none of" \
      "liam-e's apps. Nothing was deleted; run again with --apply to delete it."
    exit 0
  fi
  status=0
  (cd "$work" && mistral apps delete "$app_id" --organization "$organization" --workspace "$workspace" --yes \
    >"$work/delete.out" 2>&1) || status=$?
  [ "$status" -eq 0 ] ||
    fail "'mistral apps delete' exited with $status; the appId stays recorded; check the workspace by hand"
  check_listing gone "$app_id" ||
    fail "the CLI reported the delete, but the app list still shows the probe app; the appId stays recorded"
  forget_app_id
  echo "Deleted the probe app: no app named $PROBE_NAME remains, and its appId is gone from the deployment file."
  kept="$(python3 -I - "$work/apps-before.json" "$work/apps.json" "$protected_ids" <<'PY'
import json
import sys

before_path, after_path, protected = sys.argv[1:]
protected_ids = set(protected.split(","))


def listed(path: str) -> dict[str, tuple[object, ...]]:
    with open(path, encoding="utf-8") as handle:
        apps = json.load(handle)["apps"]
    return {app.get("id"): (app.get("name"), app.get("label"), app.get("updatedAt"))
            for app in apps if app.get("id") in protected_ids}


before, after = listed(before_path), listed(after_path)
changed = [app_id for app_id, record in before.items() if after.get(app_id) != record]
if changed:
    sys.exit(f"probe deploy: {len(changed)} liam-e app(s) listed before the delete are missing or changed now; "
             "check the workspace by hand")
print(len(before))
PY
)" || fail "a liam-e app listed before the delete is missing or changed now"
  echo "Checked the listing: the $kept liam-e apps listed before are still listed, unchanged."
  exit 0
fi

git -C "$root" fetch --quiet origin main || fail "could not fetch main from origin"
sha="$(git -C "$root" rev-parse --verify --quiet "$commit^{commit}")" || fail "unknown commit: $commit"
git -C "$root" merge-base --is-ancestor "$sha" refs/remotes/origin/main ||
  fail "commit $sha is not on origin/main; push and merge it first"
git -C "$root" cat-file -e "$sha:$PROBE_DIR/mistral.json" 2>/dev/null || fail "commit $sha has no probe app"

# Tracked files of the probe directory at that commit, nothing else. The copy carries the probe's
# name, so even a deploy without --name could not name the app after a generic directory.
mkdir "$work/export"
git -C "$root" archive --format=tar "$sha" "$PROBE_DIR" | tar -x -C "$work/export"
copy="$work/$PROBE_NAME"
mv "$work/export/$PROBE_DIR" "$copy"
[ ! -e "$copy/mistral.deployment.json" ] || fail "the exported copy holds a deployment file"

if [ -n "$app_id" ]; then
  check_listing recorded "$app_id" || fail "nothing was deployed"
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
  check_listing absent "" || fail "nothing was created"
  echo "No appId recorded yet: this deploy creates the probe app $PROBE_NAME, as confirmed with --create-app."
  deploy_args=(--name "$PROBE_NAME" --organization "$organization" --workspace "$workspace")
fi

# Keeps the new app's ID for later runs. The CLI writes the deployment block into the copy as soon
# as it has created the app, even if the build fails afterwards. Prints the ID on success.
record_new_app() {
  python3 -I - "$copy/mistral.json" "$ids_file" "$protected_ids" <<'PY'
import json
import os
import re
import sys

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
manifest_path, ids_path, protected = sys.argv[1:]
with open(manifest_path, encoding="utf-8") as handle:
    block = json.load(handle).get("deployment") or {}
with open(ids_path, encoding="utf-8") as handle:
    data = json.load(handle)
app_id = block.get("appId")
if not (isinstance(app_id, str) and UUID.fullmatch(app_id)):
    sys.exit("probe deploy: the CLI recorded no app ID in the copy")
if app_id in protected.split(","):
    sys.exit("probe deploy: the CLI reports one of liam-e's app IDs for the new app; check the workspace by hand")
if block.get("organizationId") != data["organizationId"] or block.get("workspaceId", data["workspaceId"]) != data["workspaceId"]:
    sys.exit("probe deploy: the CLI deployed to another organization or workspace than the file names")
data["appId"] = app_id
temporary = f"{ids_path}.tmp"
with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
os.replace(temporary, ids_path)
print(app_id)
PY
}

echo "Deploying the probe app from commit $sha."
status=0
(cd "$copy" && mistral deploy --from-disk "${deploy_args[@]}") || status=$?

if [ -z "$app_id" ]; then
  if new_app="$(record_new_app)"; then
    echo "Recorded the new probe app's ID in the deployment file."
  elif [ "$status" -ne 0 ]; then
    fail "'mistral deploy --from-disk' exited with $status and left no app ID in the copy; check 'mistral apps list' for an app named $PROBE_NAME"
  else
    fail "deployed, but the new appId could not be recorded; add it to the deployment file by hand from 'mistral apps list'"
  fi
fi
if [ "$status" -ne 0 ]; then
  echo "probe deploy: 'mistral deploy --from-disk' exited with $status for commit $sha" >&2
  exit "$status"
fi
if [ -z "$app_id" ]; then
  check_listing recorded "$new_app" ||
    fail "created the app, but the listing does not show it as the only app named $PROBE_NAME; check the workspace by hand"
  echo "Checked the listing: the new app is the only app named $PROBE_NAME."
fi
echo "Deployed the probe app from commit $sha."
