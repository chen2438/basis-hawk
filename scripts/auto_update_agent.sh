#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

CONFIG_FILE="${BASIS_HAWK_UPDATER_CONFIG:-/etc/basis-hawk-updater.conf}"
[[ -f "${CONFIG_FILE}" && ! -L "${CONFIG_FILE}" ]] || exit 0
# The installer creates this root-owned file from validated values.
# shellcheck disable=SC1090
. "${CONFIG_FILE}"

[[ "${AUTO_UPDATE_ENABLED:-false}" == "true" ]] || exit 0
[[ "${REPOSITORY}" =~ ^https://github\.com/[^/[:space:]]+/[^/[:space:]]+(\.git)?$ ]] \
    || exit 0

REQUEST_DIRECTORY="${STATE_DIRECTORY}/request"
REQUEST_FILE="${REQUEST_DIRECTORY}/request"
LOCK_FILE="${BASIS_HAWK_UPDATER_LOCK_FILE:-/run/lock/basis-hawk-update.lock}"

exec 9>"${LOCK_FILE}"
flock -n 9 || exit 0
[[ ! -e "${REQUEST_FILE}" && ! -L "${REQUEST_FILE}" ]] || exit 0
[[ -d "${PROJECT_DIRECTORY}/.git" ]] || exit 0
[[ "$(git -C "${PROJECT_DIRECTORY}" remote get-url origin 2>/dev/null || true)" == "${REPOSITORY}" ]] \
    || exit 0
[[ "$(git -C "${PROJECT_DIRECTORY}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" == "${BRANCH}" ]] \
    || exit 0
[[ -z "$(git -C "${PROJECT_DIRECTORY}" status --porcelain)" ]] || exit 0

current_commit="$(git -C "${PROJECT_DIRECTORY}" rev-parse HEAD 2>/dev/null)" \
    || exit 0
git -C "${PROJECT_DIRECTORY}" fetch --prune origin "${BRANCH}" || exit 0
available_commit="$(
    git -C "${PROJECT_DIRECTORY}" rev-parse "origin/${BRANCH}" 2>/dev/null
)" || exit 0
[[ "${current_commit}" != "${available_commit}" ]] || exit 0
git -C "${PROJECT_DIRECTORY}" merge-base --is-ancestor \
    "${current_commit}" "${available_commit}" || exit 0

repository_path="${REPOSITORY#https://github.com/}"
repository_path="${repository_path%.git}"
python3 - "${repository_path}" "${BRANCH}" "${available_commit}" <<'PY' \
    || exit 0
import json
import sys
import urllib.parse
import urllib.request

repository, branch, commit = sys.argv[1:]
query = urllib.parse.urlencode(
    {
        "branch": branch,
        "event": "push",
        "status": "completed",
        "per_page": 20,
    }
)
url = (
    f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs"
    f"?{query}"
)
request = urllib.request.Request(
    url,
    headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "basis-hawk-auto-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    },
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
except (OSError, ValueError):
    raise SystemExit(1)
runs = payload.get("workflow_runs")
if not isinstance(runs, list):
    raise SystemExit(1)
approved = any(
    isinstance(run, dict)
    and run.get("head_sha") == commit
    and run.get("event") == "push"
    and run.get("status") == "completed"
    and run.get("conclusion") == "success"
    for run in runs
)
raise SystemExit(0 if approved else 1)
PY

request_id="${BASIS_HAWK_AUTO_UPDATE_REQUEST_ID:-}"
if [[ -z "${request_id}" && -r /proc/sys/kernel/random/uuid ]]; then
    request_id="$(tr 'A-F' 'a-f' </proc/sys/kernel/random/uuid)"
fi
[[ "${request_id}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
    || exit 0
temporary="$(mktemp "${REQUEST_DIRECTORY}/.auto-request.XXXXXX")"
cleanup() {
    rm -f -- "${temporary}"
}
trap cleanup EXIT
printf '%s\n' \
    "version=1" \
    "request_id=${request_id}" \
    "action=update" \
    "target_commit=${available_commit}" >"${temporary}"
chmod 0600 "${temporary}"
ln "${temporary}" "${REQUEST_FILE}" || exit 0

if ! docker compose \
    --project-directory "${PROJECT_DIRECTORY}" \
    --env-file "${PROJECT_DIRECTORY}/.env" \
    run --rm --no-deps api \
    basis-hawk update-prepare --target "${available_commit}"; then
    rm -f -- "${REQUEST_FILE}"
    exit 0
fi

flock -u 9
exec 9>&-
systemctl start --no-block basis-hawk-update.service
