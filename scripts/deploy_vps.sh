#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_PROJECT_DIRECTORY="$(cd "${SCRIPT_DIRECTORY}/.." && pwd -P)"
PROJECT_DIRECTORY="${DEFAULT_PROJECT_DIRECTORY}"
ENVIRONMENT_FILE=""
DOMAIN=""
ADMIN_USERNAME="admin"
SSH_PORT=""
INSTALL_DOCKER=false
ENABLE_UFW=false
SKIP_ADMIN=false
ASSUME_YES=false
PREPARE_ENV_ONLY=false
TEMPORARY_ENVIRONMENT_FILE=""
ADMIN_TOTP_URI=""

cleanup() {
    if [[ -n "${TEMPORARY_ENVIRONMENT_FILE}" ]]; then
        rm -f -- "${TEMPORARY_ENVIRONMENT_FILE}"
    fi
}

finish() {
    local status="$?"
    cleanup
    if [[ -n "${ADMIN_TOTP_URI}" ]]; then
        printf '\n'
        log "IMPORTANT: add this one-time TOTP URI to your authenticator now"
        printf '%s\n' "${ADMIN_TOTP_URI}"
        log "IMPORTANT: this TOTP URI will not be displayed again; store it securely"
    fi
    return "${status}"
}
trap finish EXIT

usage() {
    cat <<'EOF'
Usage:
  scripts/deploy_vps.sh --domain hawk.example.com [options]

First deployment:
  sudo scripts/deploy_vps.sh \
    --domain hawk.example.com \
    --install-docker \
    --enable-ufw

Options:
  --domain DOMAIN          Public HTTPS domain. Required when creating .env.
  --admin USERNAME         Initial administrator username (default: admin).
  --project-dir PATH       Deployment checkout (default: repository root).
  --install-docker         Install Docker Engine and Compose from Docker's
                           official apt repository when Docker is absent.
  --enable-ufw             Allow SSH/80/443 and enable UFW.
  --ssh-port PORT          SSH port opened with --enable-ufw. Defaults to the
                           current SSH connection's server port, then 22.
  --skip-admin             Do not create an administrator when none exists.
  --yes                    Skip the final deployment confirmation.
  --prepare-env-only       Only create or validate .env; do not use Docker.
  -h, --help               Show this help.

The script never overwrites an existing .env and never prints generated
secrets. Administrator passwords are accepted only through the interactive,
hidden prompt provided by `basis-hawk admin-create`.
EOF
}

log() {
    printf '[basis-hawk] %s\n' "$*"
}

warn() {
    printf '[basis-hawk] WARNING: %s\n' "$*" >&2
}

die() {
    printf '[basis-hawk] ERROR: %s\n' "$*" >&2
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
        --domain)
            require_argument "$1" "${2:-}"
            DOMAIN="$2"
            shift 2
            ;;
        --admin)
            require_argument "$1" "${2:-}"
            ADMIN_USERNAME="$2"
            shift 2
            ;;
        --project-dir)
            require_argument "$1" "${2:-}"
            PROJECT_DIRECTORY="$2"
            shift 2
            ;;
        --install-docker)
            INSTALL_DOCKER=true
            shift
            ;;
        --enable-ufw)
            ENABLE_UFW=true
            shift
            ;;
        --ssh-port)
            require_argument "$1" "${2:-}"
            SSH_PORT="$2"
            shift 2
            ;;
        --skip-admin)
            SKIP_ADMIN=true
            shift
            ;;
        --yes)
            ASSUME_YES=true
            shift
            ;;
        --prepare-env-only)
            PREPARE_ENV_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ -d "${PROJECT_DIRECTORY}" ]] \
    || die "project directory does not exist: ${PROJECT_DIRECTORY}"
PROJECT_DIRECTORY="$(cd "${PROJECT_DIRECTORY}" && pwd -P)"
ENVIRONMENT_FILE="${PROJECT_DIRECTORY}/.env"

for required_file in .env.example compose.yaml Caddyfile; do
    [[ -f "${PROJECT_DIRECTORY}/${required_file}" ]] \
        || die "missing ${required_file} in ${PROJECT_DIRECTORY}"
done

validate_domain() {
    local value="$1"
    local label
    local -a labels
    [[ ${#value} -le 253 ]] || return 1
    [[ "${value}" == *.* ]] || return 1
    [[ "${value}" =~ ^[A-Za-z0-9.-]+$ ]] || return 1
    [[ "${value}" != .* && "${value}" != *. && "${value}" != *..* ]] || return 1
    IFS='.' read -r -a labels <<<"${value}"
    for label in "${labels[@]}"; do
        [[ -n "${label}" && ${#label} -le 63 ]] || return 1
        [[ "${label}" != -* && "${label}" != *- ]] || return 1
    done
}

environment_value() {
    local key="$1"
    awk -v key="${key}" '
        index($0, key "=") == 1 {
            sub(/^[^=]*=/, "")
            sub(/\r$/, "")
            print
            exit
        }
    ' "${ENVIRONMENT_FILE}"
}

generate_urlsafe_key() {
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
}

prepare_environment() {
    local database_password
    local credential_key
    local backup_key

    command -v openssl >/dev/null 2>&1 \
        || die "openssl is required to generate deployment secrets"

    if [[ -f "${ENVIRONMENT_FILE}" ]]; then
        log "using existing .env without modifying it"
        return
    fi

    if [[ -z "${DOMAIN}" ]]; then
        if [[ -t 0 ]]; then
            read -r -p "Public HTTPS domain: " DOMAIN
        else
            die "--domain is required when creating .env"
        fi
    fi
    validate_domain "${DOMAIN}" \
        || die "domain must be a valid fully qualified DNS name"

    database_password="$(openssl rand -hex 32)"
    credential_key="$(generate_urlsafe_key)"
    backup_key="$(generate_urlsafe_key)"
    TEMPORARY_ENVIRONMENT_FILE="$(mktemp "${PROJECT_DIRECTORY}/.env.tmp.XXXXXX")"

    awk \
        -v database_password="${database_password}" \
        -v credential_key="${credential_key}" \
        -v backup_key="${backup_key}" \
        -v domain="${DOMAIN}" '
        /^BASIS_HAWK_DATABASE_URL=/ {
            print "BASIS_HAWK_DATABASE_URL=postgresql+asyncpg://basis_hawk:" \
                database_password "@postgres/basis_hawk"
            next
        }
        /^BASIS_HAWK_CREDENTIAL_MASTER_KEY=/ {
            print "BASIS_HAWK_CREDENTIAL_MASTER_KEY=" credential_key
            next
        }
        /^BASIS_HAWK_BACKUP_KEY=/ {
            print "BASIS_HAWK_BACKUP_KEY=" backup_key
            next
        }
        /^BASIS_HAWK_DOMAIN=/ {
            print "BASIS_HAWK_DOMAIN=" domain
            next
        }
        /^POSTGRES_PASSWORD=/ {
            print "POSTGRES_PASSWORD=" database_password
            next
        }
        { print }
    ' "${PROJECT_DIRECTORY}/.env.example" >"${TEMPORARY_ENVIRONMENT_FILE}"

    chmod 600 "${TEMPORARY_ENVIRONMENT_FILE}"
    mv -- "${TEMPORARY_ENVIRONMENT_FILE}" "${ENVIRONMENT_FILE}"
    TEMPORARY_ENVIRONMENT_FILE=""
    log "created .env with generated secrets (values were not printed)"
}

validate_environment() {
    local configured_domain
    local database_name
    local database_user
    local database_password
    local database_url
    local credential_key
    local backup_key

    configured_domain="$(environment_value BASIS_HAWK_DOMAIN)"
    database_name="$(environment_value POSTGRES_DB)"
    database_user="$(environment_value POSTGRES_USER)"
    database_password="$(environment_value POSTGRES_PASSWORD)"
    database_url="$(environment_value BASIS_HAWK_DATABASE_URL)"
    credential_key="$(environment_value BASIS_HAWK_CREDENTIAL_MASTER_KEY)"
    backup_key="$(environment_value BASIS_HAWK_BACKUP_KEY)"

    validate_domain "${configured_domain}" \
        || die "BASIS_HAWK_DOMAIN in .env is not a valid DNS name"
    if [[ -n "${DOMAIN}" && "${DOMAIN}" != "${configured_domain}" ]]; then
        die "--domain does not match the existing .env; refusing to rewrite it"
    fi
    DOMAIN="${configured_domain}"

    [[ "${database_name}" =~ ^[A-Za-z0-9_]+$ ]] \
        || die "POSTGRES_DB must contain only letters, numbers, and underscores"
    [[ "${database_user}" =~ ^[A-Za-z0-9_]+$ ]] \
        || die "POSTGRES_USER must contain only letters, numbers, and underscores"
    [[ "${database_password}" =~ ^[A-Za-z0-9_-]{24,}$ ]] \
        || die "POSTGRES_PASSWORD must be at least 24 URL-safe characters"
    [[ "${credential_key}" =~ ^[A-Za-z0-9_-]{43}=$ ]] \
        || die "BASIS_HAWK_CREDENTIAL_MASTER_KEY must encode exactly 32 bytes"
    [[ "${backup_key}" =~ ^[A-Za-z0-9_-]{43}=$ ]] \
        || die "BASIS_HAWK_BACKUP_KEY must encode exactly 32 bytes"
    [[ "${credential_key}" != "${backup_key}" ]] \
        || die "credential and backup keys must be different"
    [[ "${database_url}" == \
        "postgresql+asyncpg://${database_user}:${database_password}@postgres/${database_name}" ]] \
        || die "BASIS_HAWK_DATABASE_URL does not match the PostgreSQL settings"
    if grep -Eq '(^|=).*replace-(me|with-a-generated-key)' "${ENVIRONMENT_FILE}"; then
        die ".env still contains a required placeholder"
    fi
    [[ "$(stat -c '%a' "${ENVIRONMENT_FILE}" 2>/dev/null \
        || stat -f '%Lp' "${ENVIRONMENT_FILE}")" == "600" ]] \
        || die ".env permissions must be 600"
}

prepare_environment
validate_environment

if ${PREPARE_ENV_ONLY}; then
    log ".env is ready at ${ENVIRONMENT_FILE}"
    exit 0
fi

[[ "$(uname -s)" == "Linux" ]] \
    || die "full VPS deployment is supported only on Linux"
[[ "${ADMIN_USERNAME}" =~ ^[^[:space:]]{1,80}$ ]] \
    || die "administrator username must be 1-80 non-whitespace characters"

run_as_root() {
    if ((EUID == 0)); then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 \
            || die "sudo is required for this operation"
        sudo "$@"
    fi
}

install_docker_engine() {
    local distribution_id
    local distribution_codename
    local architecture
    local repository_file

    [[ -r /etc/os-release ]] || die "cannot identify the Linux distribution"
    # shellcheck disable=SC1091
    . /etc/os-release
    distribution_id="${ID:-}"
    distribution_codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [[ "${distribution_id}" == "ubuntu" || "${distribution_id}" == "debian" ]] \
        || die "--install-docker supports only official Ubuntu and Debian releases"
    [[ -n "${distribution_codename}" ]] \
        || die "cannot determine the distribution codename"
    architecture="$(dpkg --print-architecture)"

    log "installing Docker Engine from Docker's official apt repository"
    run_as_root apt-get update
    run_as_root apt-get install -y ca-certificates curl
    run_as_root install -m 0755 -d /etc/apt/keyrings
    run_as_root curl -fsSL \
        "https://download.docker.com/linux/${distribution_id}/gpg" \
        -o /etc/apt/keyrings/docker.asc
    run_as_root chmod a+r /etc/apt/keyrings/docker.asc

    repository_file="$(mktemp)"
    printf '%s\n' \
        "Types: deb" \
        "URIs: https://download.docker.com/linux/${distribution_id}" \
        "Suites: ${distribution_codename}" \
        "Components: stable" \
        "Architectures: ${architecture}" \
        "Signed-By: /etc/apt/keyrings/docker.asc" >"${repository_file}"
    run_as_root install -m 0644 "${repository_file}" \
        /etc/apt/sources.list.d/docker.sources
    rm -f -- "${repository_file}"

    run_as_root apt-get update
    run_as_root apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
    run_as_root systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1; then
    ${INSTALL_DOCKER} \
        || die "Docker is missing; rerun with --install-docker"
    install_docker_engine
fi

declare -a DOCKER_COMMAND=(docker)
if ! docker info >/dev/null 2>&1; then
    if ((EUID == 0)); then
        die "Docker daemon is unavailable"
    elif sudo docker info >/dev/null 2>&1; then
        DOCKER_COMMAND=(sudo docker)
    else
        die "Docker daemon is unavailable or the current user lacks access"
    fi
fi
"${DOCKER_COMMAND[@]}" compose version >/dev/null \
    || die "Docker Compose v2 plugin is required"
declare -a COMPOSE_COMMAND=("${DOCKER_COMMAND[@]}" compose)

if ${ENABLE_UFW}; then
    if [[ -z "${SSH_PORT}" ]]; then
        SSH_PORT="${SSH_CONNECTION:-}"
        SSH_PORT="${SSH_PORT##* }"
        [[ "${SSH_PORT}" =~ ^[0-9]+$ ]] || SSH_PORT="22"
    fi
    if [[ ! "${SSH_PORT}" =~ ^[0-9]+$ ]] \
        || ((SSH_PORT < 1 || SSH_PORT > 65535)); then
        die "--ssh-port must be between 1 and 65535"
    fi
    if ! command -v ufw >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 \
            || die "install UFW manually on this distribution"
        run_as_root apt-get update
        run_as_root apt-get install -y ufw
    fi
    log "allowing SSH ${SSH_PORT}/tcp and HTTPS 80/443 through UFW"
    run_as_root ufw allow "${SSH_PORT}/tcp"
    run_as_root ufw allow 80/tcp
    run_as_root ufw allow 443/tcp
    run_as_root ufw allow 443/udp
    run_as_root ufw --force enable
fi

if command -v timedatectl >/dev/null 2>&1; then
    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" != "yes" ]]; then
        warn "system time is not reported as NTP-synchronized"
    fi
fi
if command -v getent >/dev/null 2>&1 \
    && ! getent ahosts "${DOMAIN}" >/dev/null 2>&1; then
    warn "${DOMAIN} does not resolve yet; Caddy cannot issue TLS until DNS is ready"
fi

cd "${PROJECT_DIRECTORY}"
"${COMPOSE_COMMAND[@]}" --env-file "${ENVIRONMENT_FILE}" config --quiet

if ! ${ASSUME_YES}; then
    [[ -t 0 ]] || die "interactive confirmation is unavailable; use --yes"
    printf 'Deploy Basis Hawk at https://%s from %s? [y/N] ' \
        "${DOMAIN}" "${PROJECT_DIRECTORY}"
    read -r confirmation
    [[ "${confirmation}" == "y" || "${confirmation}" == "Y" ]] \
        || die "deployment canceled"
fi

if [[ -d "${PROJECT_DIRECTORY}/.git" ]]; then
    updater_origin="$(
        git -C "${PROJECT_DIRECTORY}" remote get-url origin 2>/dev/null || true
    )"
    updater_branch="$(
        git -C "${PROJECT_DIRECTORY}" symbolic-ref --quiet --short HEAD \
            2>/dev/null || true
    )"
    if [[ "${updater_origin}" == https://* && -n "${updater_branch}" ]]; then
        log "installing the constrained host update agent"
        run_as_root "${PROJECT_DIRECTORY}/scripts/install_update_agent.sh" \
            "${PROJECT_DIRECTORY}" "${updater_origin}" "${updater_branch}"
    else
        warn "web updates are unavailable because the checkout origin or branch is unsupported"
    fi
else
    warn "web updates are unavailable because this deployment is not a Git checkout"
fi

wait_for_command() {
    local timeout_seconds="$1"
    local description="$2"
    shift 2
    local started_at="${SECONDS}"
    until "$@" >/dev/null 2>&1; do
        if ((SECONDS - started_at >= timeout_seconds)); then
            die "timed out waiting for ${description}"
        fi
        sleep 2
    done
}

configure_journal_limits() {
    local drop_in_directory="/etc/systemd/journald.conf.d"
    local drop_in_file="${drop_in_directory}/basis-hawk.conf"
    local candidate

    [[ -d /run/systemd/system ]] || return 0
    candidate="$(mktemp)"
    printf '%s\n' \
        '[Journal]' \
        'SystemMaxUse=200M' \
        'SystemKeepFree=1G' \
        'MaxRetentionSec=7day' >"${candidate}"
    if [[ -f "${drop_in_file}" ]] && cmp -s "${candidate}" "${drop_in_file}"; then
        rm -f -- "${candidate}"
    else
        run_as_root install -d -m 0755 "${drop_in_directory}" \
            || { rm -f -- "${candidate}"; return 1; }
        run_as_root install -m 0644 "${candidate}" "${drop_in_file}" \
            || { rm -f -- "${candidate}"; return 1; }
        rm -f -- "${candidate}"
        run_as_root systemctl restart systemd-journald || return 1
    fi
    run_as_root journalctl --vacuum-size=200M >/dev/null
}

log "building API, worker, and backup images"
"${COMPOSE_COMMAND[@]}" build --pull api worker backup

log "ensuring PostgreSQL is running"
"${COMPOSE_COMMAND[@]}" up -d postgres
# Variables intentionally expand inside the PostgreSQL container.
# shellcheck disable=SC2016
wait_for_command 120 "PostgreSQL" \
    "${COMPOSE_COMMAND[@]}" exec -T postgres \
    sh -ec 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

existing_schema="$(
    # Variables intentionally expand inside the PostgreSQL container.
    # shellcheck disable=SC2016
    "${COMPOSE_COMMAND[@]}" exec -T postgres sh -ec \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        "SELECT COALESCE(to_regclass('\''public.alembic_version'\'')::text, '\'''\'');"'
)"
if [[ -n "${existing_schema//[[:space:]]/}" ]]; then
    log "existing database detected; entering maintenance mode and creating a pre-upgrade backup"
    "${COMPOSE_COMMAND[@]}" stop worker api backup >/dev/null 2>&1 || true
    "${COMPOSE_COMMAND[@]}" run --rm backup create
fi

log "pulling PostgreSQL and Caddy service images after the safety backup"
"${COMPOSE_COMMAND[@]}" pull postgres caddy
"${COMPOSE_COMMAND[@]}" up -d postgres
# Variables intentionally expand inside the PostgreSQL container.
# shellcheck disable=SC2016
wait_for_command 120 "PostgreSQL after image refresh" \
    "${COMPOSE_COMMAND[@]}" exec -T postgres \
    sh -ec 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

log "applying database migrations"
"${COMPOSE_COMMAND[@]}" run --rm api alembic upgrade head

admin_count="$(
    # Variables intentionally expand inside the PostgreSQL container.
    # shellcheck disable=SC2016
    "${COMPOSE_COMMAND[@]}" exec -T postgres sh -ec \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        "SELECT count(*) FROM admin_users;"'
)"
admin_count="${admin_count//[[:space:]]/}"
[[ "${admin_count}" =~ ^[0-9]+$ ]] \
    || die "could not determine administrator state"
if [[ "${admin_count}" == "0" ]]; then
    if ${SKIP_ADMIN}; then
        warn "no administrator exists; login will remain unavailable"
    else
        [[ -t 0 && -t 1 ]] \
            || die "administrator creation requires a terminal; use --skip-admin only if intentional"
        log "creating the initial administrator; the TOTP URI will be displayed when deployment finishes"
        admin_output=""
        if ! admin_output="$(
            "${COMPOSE_COMMAND[@]}" run --rm api \
                basis-hawk admin-create --username "${ADMIN_USERNAME}"
        )"; then
            printf '%s\n' "${admin_output}" >&2
            die "administrator creation failed"
        fi
        while IFS= read -r output_line; do
            if [[ "${output_line}" == otpauth://totp/* ]]; then
                ADMIN_TOTP_URI="${output_line}"
                break
            fi
        done <<<"${admin_output}"
        [[ -n "${ADMIN_TOTP_URI}" ]] \
            || die "administrator was created but no TOTP URI was returned"
    fi
else
    log "administrator already exists; bootstrap step skipped"
fi

log "starting API, worker, backup, and Caddy"
"${COMPOSE_COMMAND[@]}" up -d --remove-orphans
wait_for_command 180 "API liveness" \
    "${COMPOSE_COMMAND[@]}" exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live')"
wait_for_command 300 "market catalog readiness" \
    "${COMPOSE_COMMAND[@]}" exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready')"
wait_for_command 120 "first encrypted backup" \
    "${COMPOSE_COMMAND[@]}" exec -T backup sh -ec \
    'find /backups -maxdepth 1 -name "basis-hawk-*.bhbk" -print -quit | grep -q .'
# Variables intentionally expand inside the backup container.
# shellcheck disable=SC2016
"${COMPOSE_COMMAND[@]}" exec -T backup sh -ec \
    'latest="$(ls -1t /backups/basis-hawk-*.bhbk | head -n 1)"; \
    python3 -m basis_hawk.backup verify "$latest"'

log "pruning unused Docker build cache older than 24 hours"
if ! "${DOCKER_COMMAND[@]}" builder prune --all --force --filter "until=24h"; then
    warn "Docker build cache cleanup failed; deployment remains healthy"
fi
log "applying a 200 MB system journal limit"
if ! configure_journal_limits; then
    warn "system journal limit could not be applied; deployment remains healthy"
fi

"${COMPOSE_COMMAND[@]}" ps
log "deployment complete: https://${DOMAIN}"
log "next: sign in, configure exchange credentials, and keep automatic trading disabled until account reconciliation is reviewed"
