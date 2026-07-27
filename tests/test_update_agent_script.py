from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

from basis_hawk.updates import enqueue_update

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "scripts" / "update_agent.sh"


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
