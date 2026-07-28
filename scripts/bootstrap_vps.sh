#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPOSITORY="https://github.com/chen2438/basis-hawk.git"
DEFAULT_BRANCH="main"
DEFAULT_INSTALL_DIRECTORY="/opt/basis-hawk"
REPOSITORY="${DEFAULT_REPOSITORY}"
BRANCH="${DEFAULT_BRANCH}"
INSTALL_DIRECTORY="${DEFAULT_INSTALL_DIRECTORY}"
declare -a DEPLOY_ARGUMENTS=()

usage() {
    cat <<'EOF'
Usage:
  curl -fsSL \
    https://raw.githubusercontent.com/chen2438/basis-hawk/main/scripts/bootstrap_vps.sh \
    | sudo bash -s -- --domain hawk.example.com --install-docker

Bootstrap options:
  --repository URL        Git repository (default: official Basis Hawk repo).
  --branch BRANCH         Branch to clone or fast-forward (default: main).
  --install-dir PATH      Checkout path (default: /opt/basis-hawk).
  -h, --help              Show this help.

All other options are passed unchanged to scripts/deploy_vps.sh. On repeat
runs, the checkout must have the expected origin, branch, and a clean tracked
worktree. Updates are fast-forward only; existing files are never deleted.
EOF
}

log() {
    printf '[basis-hawk-bootstrap] %s\n' "$*"
}

die() {
    printf '[basis-hawk-bootstrap] ERROR: %s\n' "$*" >&2
    exit 1
}

require_argument() {
    local option="$1"
    local value="${2:-}"
    [[ -n "${value}" && "${value}" != --* ]] \
        || die "${option} requires a value"
}

while (($#)); do
    case "$1" in
        --repository)
            require_argument "$1" "${2:-}"
            REPOSITORY="$2"
            shift 2
            ;;
        --branch)
            require_argument "$1" "${2:-}"
            BRANCH="$2"
            shift 2
            ;;
        --install-dir)
            require_argument "$1" "${2:-}"
            INSTALL_DIRECTORY="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            DEPLOY_ARGUMENTS+=("$@")
            break
            ;;
        *)
            DEPLOY_ARGUMENTS+=("$1")
            shift
            ;;
    esac
done

[[ "${REPOSITORY}" == https://* || "${REPOSITORY}" == file://* ]] \
    || die "repository must use HTTPS"
[[ "${BRANCH}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
    && [[ "${BRANCH}" != *..* && "${BRANCH}" != *//* ]] \
    || die "branch contains unsupported characters"
[[ "${INSTALL_DIRECTORY}" == /* && "${INSTALL_DIRECTORY}" != "/" ]] \
    || die "install directory must be an absolute path other than /"

run_as_root() {
    if ((EUID == 0)); then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 \
            || die "sudo is required to install prerequisites"
        sudo "$@"
    fi
}

install_git() {
    [[ -r /etc/os-release ]] || die "cannot identify the Linux distribution"
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] \
        || die "install Git manually on this distribution"
    log "installing Git and CA certificates"
    run_as_root apt-get update
    run_as_root apt-get install -y git ca-certificates
}

if ! command -v git >/dev/null 2>&1; then
    install_git
fi

if [[ -e "${INSTALL_DIRECTORY}" && ! -d "${INSTALL_DIRECTORY}/.git" ]]; then
    die "install directory exists but is not the expected Git checkout"
fi

if [[ ! -d "${INSTALL_DIRECTORY}/.git" ]]; then
    parent_directory="$(dirname "${INSTALL_DIRECTORY}")"
    if [[ ! -d "${parent_directory}" ]]; then
        run_as_root install -d -m 0755 "${parent_directory}"
    fi
    if [[ ! -w "${parent_directory}" ]] && ((EUID != 0)); then
        die "install directory parent is not writable; run the bootstrap with sudo"
    fi
    log "cloning ${REPOSITORY} (${BRANCH}) into ${INSTALL_DIRECTORY}"
    git clone --branch "${BRANCH}" --single-branch \
        "${REPOSITORY}" "${INSTALL_DIRECTORY}"
else
    log "validating existing checkout"
    configured_origin="$(
        git -C "${INSTALL_DIRECTORY}" remote get-url origin 2>/dev/null || true
    )"
    [[ "${configured_origin}" == "${REPOSITORY}" ]] \
        || die "existing checkout origin does not match --repository"
    current_branch="$(
        git -C "${INSTALL_DIRECTORY}" symbolic-ref --quiet --short HEAD \
            2>/dev/null || true
    )"
    [[ "${current_branch}" == "${BRANCH}" ]] \
        || die "existing checkout is not on branch ${BRANCH}"
    [[ -z "$(git -C "${INSTALL_DIRECTORY}" status --porcelain)" ]] \
        || die "existing checkout has local changes; refusing to overwrite them"

    log "fetching and applying a fast-forward-only update"
    git -C "${INSTALL_DIRECTORY}" fetch --prune origin "${BRANCH}"
    git -C "${INSTALL_DIRECTORY}" merge --ff-only FETCH_HEAD
fi

deploy_script="${INSTALL_DIRECTORY}/scripts/deploy_vps.sh"
[[ -x "${deploy_script}" ]] \
    || die "deployment script is missing or not executable after checkout"

log "starting VPS deployment from ${INSTALL_DIRECTORY}"
if [[ ! -t 0 && -t 1 ]] \
    && { : </dev/tty; } 2>/dev/null; then
    log "reconnecting deployment input to the controlling terminal"
    exec "${deploy_script}" \
        --project-dir "${INSTALL_DIRECTORY}" \
        "${DEPLOY_ARGUMENTS[@]}" </dev/tty
fi

exec "${deploy_script}" \
    --project-dir "${INSTALL_DIRECTORY}" \
    "${DEPLOY_ARGUMENTS[@]}"
