#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

CONFIG_FILE="${BASIS_HAWK_UPDATER_CONFIG:-/etc/basis-hawk-updater.conf}"
[[ -f "${CONFIG_FILE}" && ! -L "${CONFIG_FILE}" ]] || exit 1
# The installer creates this root-owned file from validated values.
# shellcheck disable=SC1090
. "${CONFIG_FILE}"

REQUEST_FILE="${STATE_DIRECTORY}/request/request"
STATUS_DIRECTORY="${STATE_DIRECTORY}/status"
STATUS_FILE="${STATUS_DIRECTORY}/status"
LOCK_FILE="${BASIS_HAWK_UPDATER_LOCK_FILE:-/run/lock/basis-hawk-update.lock}"

write_status() {
    local state="$1"
    local current_commit="${2:-}"
    local available_commit="${3:-}"
    local request_id="${4:-}"
    local checked_at="${5:-}"
    local completed_at="${6:-}"
    local error_code="${7:-}"
    local temporary
    temporary="$(mktemp "${STATUS_DIRECTORY}/.status.XXXXXX")"
    printf '%s\n' \
        "version=1" \
        "state=${state}" \
        "current_commit=${current_commit}" \
        "available_commit=${available_commit}" \
        "request_id=${request_id}" \
        "checked_at=${checked_at}" \
        "completed_at=${completed_at}" \
        "error_code=${error_code}" >"${temporary}"
    chmod 0640 "${temporary}"
    chown root:10001 "${temporary}"
    mv -f -- "${temporary}" "${STATUS_FILE}"
}

fail() {
    local code="$1"
    local current="${2:-}"
    local available="${3:-}"
    local request="${4:-}"
    write_status \
        "failed" "${current}" "${available}" "${request}" "" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${code}"
    exit 1
}

exec 9>"${LOCK_FILE}"
flock -n 9 || exit 0
[[ -f "${REQUEST_FILE}" && ! -L "${REQUEST_FILE}" ]] || exit 0
trap 'rm -f -- "${REQUEST_FILE}"' EXIT

[[ "$(wc -l <"${REQUEST_FILE}")" -eq 4 ]] || fail "invalid_request"
version_line="$(sed -n '1p' "${REQUEST_FILE}")"
request_line="$(sed -n '2p' "${REQUEST_FILE}")"
action_line="$(sed -n '3p' "${REQUEST_FILE}")"
target_line="$(sed -n '4p' "${REQUEST_FILE}")"
[[ "${version_line}" == "version=1" ]] || fail "invalid_request"
request_id="${request_line#request_id=}"
action="${action_line#action=}"
target_commit="${target_line#target_commit=}"
[[ "${request_line}" == "request_id=${request_id}" ]] \
    && [[ "${request_id}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
    || fail "invalid_request"
[[ "${action_line}" == "action=${action}" ]] || fail "invalid_request"
[[ "${target_line}" == "target_commit=${target_commit}" ]] \
    || fail "invalid_request"
[[ "${action}" == "check" || "${action}" == "update" ]] \
    || fail "invalid_request"
if [[ "${action}" == "check" ]]; then
    [[ -z "${target_commit}" ]] || fail "invalid_request"
else
    [[ "${target_commit}" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] \
        || fail "invalid_request"
fi

[[ -d "${PROJECT_DIRECTORY}/.git" ]] || fail "checkout_missing" "" "" "${request_id}"
[[ "$(git -C "${PROJECT_DIRECTORY}" remote get-url origin 2>/dev/null || true)" == "${REPOSITORY}" ]] \
    || fail "origin_mismatch" "" "" "${request_id}"
[[ "$(git -C "${PROJECT_DIRECTORY}" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" == "${BRANCH}" ]] \
    || fail "branch_mismatch" "" "" "${request_id}"
[[ -z "$(git -C "${PROJECT_DIRECTORY}" status --porcelain)" ]] \
    || fail "local_changes" "" "" "${request_id}"

current_commit="$(git -C "${PROJECT_DIRECTORY}" rev-parse HEAD 2>/dev/null)" \
    || fail "checkout_invalid" "" "" "${request_id}"
write_status \
    "$([[ "${action}" == "check" ]] && printf checking || printf updating)" \
    "${current_commit}" "" "${request_id}" "" "" ""

git -C "${PROJECT_DIRECTORY}" fetch --prune origin "${BRANCH}" \
    || fail "fetch_failed" "${current_commit}" "" "${request_id}"
available_commit="$(
    git -C "${PROJECT_DIRECTORY}" rev-parse "origin/${BRANCH}" 2>/dev/null
)" || fail "remote_invalid" "${current_commit}" "" "${request_id}"
git -C "${PROJECT_DIRECTORY}" merge-base --is-ancestor \
    "${current_commit}" "${available_commit}" \
    || fail "not_fast_forward" "${current_commit}" "${available_commit}" "${request_id}"

checked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "${action}" == "check" ]]; then
    if [[ "${current_commit}" == "${available_commit}" ]]; then
        write_status \
            "up_to_date" "${current_commit}" "${available_commit}" \
            "${request_id}" "${checked_at}" "${checked_at}" ""
    else
        write_status \
            "update_available" "${current_commit}" "${available_commit}" \
            "${request_id}" "${checked_at}" "${checked_at}" ""
    fi
    exit 0
fi

[[ "${target_commit}" == "${available_commit}" ]] \
    || fail "remote_changed" "${current_commit}" "${available_commit}" "${request_id}"
write_status \
    "updating" "${current_commit}" "${available_commit}" \
    "${request_id}" "${checked_at}" "" ""
git -C "${PROJECT_DIRECTORY}" merge --ff-only "${available_commit}" \
    || fail "merge_failed" "${current_commit}" "${available_commit}" "${request_id}"
"${PROJECT_DIRECTORY}/scripts/deploy_vps.sh" \
    --project-dir "${PROJECT_DIRECTORY}" \
    --skip-admin \
    --yes \
    || fail "deployment_failed" "${available_commit}" "${available_commit}" "${request_id}"

write_status \
    "succeeded" "${available_commit}" "${available_commit}" \
    "${request_id}" "${checked_at}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" ""
