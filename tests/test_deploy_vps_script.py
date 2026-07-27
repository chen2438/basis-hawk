from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_vps.sh"


def deployment_fixture(tmp_path: Path) -> Path:
    for name in (".env.example", "compose.yaml", "Caddyfile"):
        shutil.copy2(ROOT / name, tmp_path / name)
    return tmp_path


def run_script(
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def environment_values(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for key, separator, value in [line.partition("=")]
        if separator
    }


def fake_vps_commands(tmp_path: Path) -> tuple[dict[str, str], Path]:
    binary_directory = tmp_path / "fake-bin"
    binary_directory.mkdir()
    log_path = tmp_path / "docker.log"
    docker = binary_directory / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_DOCKER_LOG"
command_line="$*"
if [[ "$command_line" == *"to_regclass"* ]] \
    && [[ "${FAKE_EXISTING_SCHEMA:-0}" == "1" ]]; then
    printf 'alembic_version\\n'
elif [[ "$command_line" == *"SELECT count(*) FROM admin_users"* ]]; then
    printf '%s\\n' "${FAKE_ADMIN_COUNT:-0}"
fi
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    for name, contents in {
        "uname": "#!/usr/bin/env bash\nprintf 'Linux\\n'\n",
        "getent": "#!/usr/bin/env bash\nexit 0\n",
        "timedatectl": "#!/usr/bin/env bash\nprintf 'yes\\n'\n",
    }.items():
        path = binary_directory / name
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary_directory}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
    }
    return environment, log_path


def test_deploy_script_help_does_not_require_a_vps() -> None:
    result = run_script("--help")
    assert result.returncode == 0
    assert "--prepare-env-only" in result.stdout
    assert "never overwrites an existing .env" in result.stdout


def test_prepare_environment_generates_independent_secrets_without_leaking(
    tmp_path: Path,
) -> None:
    project = deployment_fixture(tmp_path)
    result = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--prepare-env-only",
    )
    assert result.returncode == 0, result.stderr

    environment_file = project / ".env"
    values = environment_values(environment_file)
    assert values["BASIS_HAWK_DOMAIN"] == "hawk.example.com"
    assert values["POSTGRES_PASSWORD"] in values["BASIS_HAWK_DATABASE_URL"]
    assert len(values["POSTGRES_PASSWORD"]) == 64
    assert len(values["BASIS_HAWK_CREDENTIAL_MASTER_KEY"]) == 44
    assert len(values["BASIS_HAWK_BACKUP_KEY"]) == 44
    assert (
        values["BASIS_HAWK_CREDENTIAL_MASTER_KEY"]
        != values["BASIS_HAWK_BACKUP_KEY"]
    )
    assert values["POSTGRES_PASSWORD"] not in result.stdout
    assert values["BASIS_HAWK_CREDENTIAL_MASTER_KEY"] not in result.stdout
    assert values["BASIS_HAWK_BACKUP_KEY"] not in result.stdout
    assert oct(environment_file.stat().st_mode & 0o777) == "0o600"


def test_prepare_environment_is_idempotent_and_refuses_domain_rewrite(
    tmp_path: Path,
) -> None:
    project = deployment_fixture(tmp_path)
    first = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--prepare-env-only",
    )
    assert first.returncode == 0, first.stderr
    original = (project / ".env").read_bytes()

    second = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--prepare-env-only",
    )
    assert second.returncode == 0, second.stderr
    assert (project / ".env").read_bytes() == original

    mismatch = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "other.example.com",
        "--prepare-env-only",
    )
    assert mismatch.returncode != 0
    assert "refusing to rewrite" in mismatch.stderr
    assert (project / ".env").read_bytes() == original


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "-hawk.example.com",
        "hawk-.example.com",
        "hawk..example.com",
        "https://hawk.example.com",
    ],
)
def test_prepare_environment_rejects_invalid_domains(
    tmp_path: Path,
    domain: str,
) -> None:
    project = deployment_fixture(tmp_path)
    result = run_script(
        "--project-dir",
        str(project),
        "--domain",
        domain,
        "--prepare-env-only",
    )
    assert result.returncode != 0
    assert not (project / ".env").exists()


def test_prepare_environment_rejects_unsafe_permissions(tmp_path: Path) -> None:
    project = deployment_fixture(tmp_path)
    environment_file = project / ".env"
    prepared = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--prepare-env-only",
    )
    assert prepared.returncode == 0, prepared.stderr
    os.chmod(environment_file, 0o644)

    result = run_script(
        "--project-dir",
        str(project),
        "--prepare-env-only",
    )
    assert result.returncode != 0
    assert "permissions must be 600" in result.stderr


def test_full_first_deployment_runs_migrations_and_health_checks(
    tmp_path: Path,
) -> None:
    project = deployment_fixture(tmp_path)
    environment, log_path = fake_vps_commands(tmp_path)
    result = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--skip-admin",
        "--yes",
        environment=environment,
    )
    assert result.returncode == 0, result.stderr
    commands = log_path.read_text(encoding="utf-8")
    assert "run --rm api alembic upgrade head" in commands
    assert "up -d --remove-orphans" in commands
    assert "/api/health/live" in commands
    assert "/api/health/ready" in commands
    assert "basis_hawk.backup verify" in commands
    assert "stop worker api backup" not in commands


def test_existing_deployment_stops_worker_and_backs_up_before_migration(
    tmp_path: Path,
) -> None:
    project = deployment_fixture(tmp_path)
    prepared = run_script(
        "--project-dir",
        str(project),
        "--domain",
        "hawk.example.com",
        "--prepare-env-only",
    )
    assert prepared.returncode == 0, prepared.stderr
    environment, log_path = fake_vps_commands(tmp_path)
    environment["FAKE_EXISTING_SCHEMA"] = "1"
    environment["FAKE_ADMIN_COUNT"] = "1"

    result = run_script(
        "--project-dir",
        str(project),
        "--yes",
        environment=environment,
    )
    assert result.returncode == 0, result.stderr
    commands = log_path.read_text(encoding="utf-8")
    stop_index = commands.index("stop worker api backup")
    backup_index = commands.index("run --rm backup create")
    pull_index = commands.index("pull postgres caddy")
    migration_index = commands.index("run --rm api alembic upgrade head")
    assert stop_index < backup_index < pull_index < migration_index
    assert "admin-create" not in commands
