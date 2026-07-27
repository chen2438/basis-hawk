from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

from basis_hawk.updates import enqueue_update

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "scripts" / "update_agent.sh"
AUTO_AGENT = ROOT / "scripts" / "auto_update_agent.sh"


def git(*arguments: str, directory: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_host_agent_checks_and_applies_only_remote_fast_forward(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    source.mkdir()
    git("init", "-b", "main", directory=source)
    git("config", "user.name", "Update Test", directory=source)
    git("config", "user.email", "update@example.test", directory=source)
    scripts = source / "scripts"
    scripts.mkdir()
    deploy = scripts / "deploy_vps.sh"
    deploy.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"$FAKE_DEPLOY_LOG\"\n",
        encoding="utf-8",
    )
    deploy.chmod(0o755)
    (source / "version.txt").write_text("1\n", encoding="utf-8")
    git("add", ".", directory=source)
    git("commit", "-m", "initial", directory=source)
    git("clone", "--bare", str(source), str(remote))
    git("clone", "--branch", "main", remote.as_uri(), str(checkout))

    (source / "version.txt").write_text("2\n", encoding="utf-8")
    git("add", "version.txt", directory=source)
    git("commit", "-m", "update", directory=source)
    git("push", str(remote), "main", directory=source)
    available = git("rev-parse", "HEAD", directory=source)

    state = tmp_path / "state"
    request_directory = state / "request"
    status_directory = state / "status"
    request_directory.mkdir(parents=True)
    status_directory.mkdir()
    config = tmp_path / "updater.conf"
    config.write_text(
        "\n".join(
            [
                f"PROJECT_DIRECTORY={checkout}",
                f"REPOSITORY={remote.as_uri()}",
                "BRANCH=main",
                f"STATE_DIRECTORY={state}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("chown", "flock"):
        command = fake_bin / name
        command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BASIS_HAWK_UPDATER_CONFIG": str(config),
        "BASIS_HAWK_UPDATER_LOCK_FILE": str(tmp_path / "update.lock"),
        "FAKE_DEPLOY_LOG": str(tmp_path / "deploy.log"),
    }

    check_id = uuid4()
    enqueue_update(request_directory, action="check", request_id=check_id)
    checked = subprocess.run(
        ["bash", str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert checked.returncode == 0, checked.stderr
    status = (status_directory / "status").read_text(encoding="ascii")
    assert "state=update_available" in status
    assert f"available_commit={available}" in status

    update_id = uuid4()
    enqueue_update(
        request_directory,
        action="update",
        request_id=update_id,
        target_commit=available,
    )
    updated = subprocess.run(
        ["bash", str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert updated.returncode == 0, updated.stderr
    assert git("rev-parse", "HEAD", directory=checkout) == available
    deploy_arguments = (tmp_path / "deploy.log").read_text(encoding="utf-8")
    assert "--reconcile-after-update" in deploy_arguments
    assert "--skip-admin" in deploy_arguments
    assert "state=succeeded" in (
        status_directory / "status"
    ).read_text(encoding="ascii")


def test_auto_agent_queues_only_ci_approved_update_after_safety_pause(
    tmp_path: Path,
) -> None:
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".env").write_text("TEST=1\n", encoding="ascii")
    state = tmp_path / "state"
    request_directory = state / "request"
    request_directory.mkdir(parents=True)
    (state / "status").mkdir()
    repository = "https://github.com/example/basis-hawk.git"
    current = "1" * 40
    available = "2" * 40
    config = tmp_path / "updater.conf"
    config.write_text(
        "\n".join(
            [
                f"PROJECT_DIRECTORY={shlex.quote(str(project))}",
                f"REPOSITORY={repository}",
                "BRANCH=main",
                f"STATE_DIRECTORY={shlex.quote(str(state))}",
                "AUTO_UPDATE_ENABLED=true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    flock_command = fake_bin / "flock"
    flock_command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    flock_command.chmod(0o755)
    git_command = fake_bin / "git"
    git_command.write_text(
        f"""#!/usr/bin/env bash
case "$*" in
  *"remote get-url origin"*) printf '%s\\n' '{repository}' ;;
  *"symbolic-ref --quiet --short HEAD"*) printf 'main\\n' ;;
  *"status --porcelain"*) : ;;
  *"rev-parse HEAD"*) printf '%s\\n' '{current}' ;;
  *"rev-parse origin/main"*) printf '%s\\n' '{available}' ;;
  *"fetch --prune origin main"*) : ;;
  *"merge-base --is-ancestor"*) : ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    git_command.chmod(0o755)
    python_command = fake_bin / "python3"
    python_command.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null\nexit \"${FAKE_CI_RESULT:-0}\"\n",
        encoding="utf-8",
    )
    python_command.chmod(0o755)
    docker_log = tmp_path / "docker.log"
    docker_command = fake_bin / "docker"
    docker_command.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >\"$FAKE_DOCKER_LOG\"\n"
        "exit \"${FAKE_PREPARE_RESULT:-0}\"\n",
        encoding="utf-8",
    )
    docker_command.chmod(0o755)
    systemctl_command = fake_bin / "systemctl"
    systemctl_command.write_text(
        "#!/usr/bin/env bash\nexit 0\n",
        encoding="utf-8",
    )
    systemctl_command.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BASIS_HAWK_UPDATER_CONFIG": str(config),
        "BASIS_HAWK_UPDATER_LOCK_FILE": str(tmp_path / "update.lock"),
        "BASIS_HAWK_AUTO_UPDATE_REQUEST_ID": str(uuid4()),
        "FAKE_DOCKER_LOG": str(docker_log),
    }

    approved = subprocess.run(
        ["bash", str(AUTO_AGENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert approved.returncode == 0, approved.stderr
    request = (request_directory / "request").read_text(encoding="ascii")
    assert "action=update" in request
    assert f"target_commit={available}" in request
    prepare = docker_log.read_text(encoding="utf-8")
    assert "basis-hawk update-prepare" in prepare
    assert available in prepare

    (request_directory / "request").unlink()
    environment["FAKE_CI_RESULT"] = "1"
    rejected = subprocess.run(
        ["bash", str(AUTO_AGENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode == 0, rejected.stderr
    assert not (request_directory / "request").exists()

    environment["FAKE_CI_RESULT"] = "0"
    environment["FAKE_PREPARE_RESULT"] = "1"
    unsafe = subprocess.run(
        ["bash", str(AUTO_AGENT)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert unsafe.returncode == 0, unsafe.stderr
    assert not (request_directory / "request").exists()
