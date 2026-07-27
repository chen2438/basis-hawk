#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

[[ ${EUID} -eq 0 ]] || {
    printf 'install_update_agent.sh must run as root\n' >&2
    exit 1
}

PROJECT_DIRECTORY="${1:-}"
REPOSITORY="${2:-}"
BRANCH="${3:-}"
AUTO_UPDATE_MODE="${4:-preserve}"
STATE_DIRECTORY="/var/lib/basis-hawk-updater"
CONFIG_FILE="/etc/basis-hawk-updater.conf"

[[ "${PROJECT_DIRECTORY}" == /* && "${PROJECT_DIRECTORY}" != "/" ]] || exit 1
[[ "${REPOSITORY}" == https://* ]] || exit 1
[[ "${BRANCH}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
    && [[ "${BRANCH}" != *..* && "${BRANCH}" != *//* ]] || exit 1
[[ "${AUTO_UPDATE_MODE}" == "true" \
    || "${AUTO_UPDATE_MODE}" == "false" \
    || "${AUTO_UPDATE_MODE}" == "preserve" ]] || exit 1
[[ -x "${PROJECT_DIRECTORY}/scripts/update_agent.sh" ]] || exit 1
[[ -x "${PROJECT_DIRECTORY}/scripts/auto_update_agent.sh" ]] || exit 1
command -v systemctl >/dev/null 2>&1 || {
    printf 'systemd is required for web updates\n' >&2
    exit 1
}

AUTO_UPDATE_ENABLED=false
if [[ "${AUTO_UPDATE_MODE}" == "preserve" \
    && -f "${CONFIG_FILE}" && ! -L "${CONFIG_FILE}" ]]; then
    previous_auto_update="$(
        awk -F= '$1 == "AUTO_UPDATE_ENABLED" { print $2 }' \
            "${CONFIG_FILE}" | tail -n 1
    )"
    [[ "${previous_auto_update}" == "true" ]] \
        && AUTO_UPDATE_ENABLED=true
elif [[ "${AUTO_UPDATE_MODE}" != "preserve" ]]; then
    AUTO_UPDATE_ENABLED="${AUTO_UPDATE_MODE}"
fi
if [[ "${AUTO_UPDATE_ENABLED}" == "true" ]]; then
    [[ "${REPOSITORY}" =~ ^https://github\.com/[^/[:space:]]+/[^/[:space:]]+(\.git)?$ ]] \
        || {
            printf 'automatic updates require a GitHub HTTPS repository\n' >&2
            exit 1
        }
fi

install -d -m 0755 /usr/local/libexec
install -m 0755 \
    "${PROJECT_DIRECTORY}/scripts/update_agent.sh" \
    /usr/local/libexec/basis-hawk-update-agent
install -m 0755 \
    "${PROJECT_DIRECTORY}/scripts/auto_update_agent.sh" \
    /usr/local/libexec/basis-hawk-auto-update-agent
install -d -m 0750 -o 10001 -g 0 "${STATE_DIRECTORY}/request"
install -d -m 0750 -o root -g 10001 "${STATE_DIRECTORY}/status"

{
    printf 'PROJECT_DIRECTORY=%q\n' "${PROJECT_DIRECTORY}"
    printf 'REPOSITORY=%q\n' "${REPOSITORY}"
    printf 'BRANCH=%q\n' "${BRANCH}"
    printf 'STATE_DIRECTORY=%q\n' "${STATE_DIRECTORY}"
    printf 'AUTO_UPDATE_ENABLED=%q\n' "${AUTO_UPDATE_ENABLED}"
} >"${CONFIG_FILE}"
chmod 0600 "${CONFIG_FILE}"
chown root:root "${CONFIG_FILE}"

install -m 0644 /dev/stdin /etc/systemd/system/basis-hawk-update.service <<'EOF'
[Unit]
Description=Basis Hawk controlled update agent
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/local/libexec/basis-hawk-update-agent
EOF

install -m 0644 /dev/stdin /etc/systemd/system/basis-hawk-update.path <<'EOF'
[Unit]
Description=Watch for Basis Hawk controlled update requests

[Path]
PathExists=/var/lib/basis-hawk-updater/request/request
Unit=basis-hawk-update.service

[Install]
WantedBy=multi-user.target
EOF

install -m 0644 /dev/stdin /etc/systemd/system/basis-hawk-auto-update.service <<'EOF'
[Unit]
Description=Check for a CI-approved Basis Hawk update
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
UMask=0077
ExecStart=/usr/local/libexec/basis-hawk-auto-update-agent
EOF

install -m 0644 /dev/stdin /etc/systemd/system/basis-hawk-auto-update.timer <<'EOF'
[Unit]
Description=Periodically check for CI-approved Basis Hawk updates

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30
Persistent=true
Unit=basis-hawk-auto-update.service

[Install]
WantedBy=timers.target
EOF

status_file="${STATE_DIRECTORY}/status/status"
if [[ ! -f "${status_file}" ]]; then
    current_commit="$(git -C "${PROJECT_DIRECTORY}" rev-parse HEAD)"
    temporary="$(mktemp "${STATE_DIRECTORY}/status/.status.XXXXXX")"
    printf '%s\n' \
        "version=1" \
        "state=idle" \
        "current_commit=${current_commit}" \
        "available_commit=${current_commit}" \
        "request_id=" \
        "checked_at=" \
        "completed_at=" \
        "error_code=" >"${temporary}"
    chmod 0640 "${temporary}"
    chown root:10001 "${temporary}"
    mv -f -- "${temporary}" "${status_file}"
fi

systemctl daemon-reload
systemctl enable --now basis-hawk-update.path
if [[ "${AUTO_UPDATE_ENABLED}" == "true" ]]; then
    systemctl enable --now basis-hawk-auto-update.timer
else
    systemctl disable --now basis-hawk-auto-update.timer >/dev/null 2>&1 || true
fi
