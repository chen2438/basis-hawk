from __future__ import annotations

import os
import subprocess
from pathlib import Path

BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_vps.sh"
)


def git(*arguments: str, directory: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_remote_repository(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    git("init", "-b", "main", directory=source)
    git("config", "user.name", "Bootstrap Test", directory=source)
    git(
        "config",
        "user.email",
        "bootstrap@example.test",
        directory=source,
    )
    (source / ".gitignore").write_text(".env\n", encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir()
    deploy = scripts / "deploy_vps.sh"
    deploy.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"$FAKE_DEPLOY_LOG\"\n",
        encoding="utf-8",
    )
    deploy.chmod(0o755)
    git("add", ".", directory=source)
    git("commit", "-m", "initial", directory=source)
    git("clone", "--bare", str(source), str(remote))
    return source, remote


def run_bootstrap(
    repository: Path,
    install_directory: Path,
    deploy_log: Path,
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "FAKE_DEPLOY_LOG": str(deploy_log)}
    return subprocess.run(
        [
            "bash",
            str(BOOTSTRAP),
            "--repository",
            repository.as_uri(),
            "--install-dir",
            str(install_directory),
            "--branch",
            "main",
            "--domain",
            "hawk.example.com",
            "--skip-admin",
            "--yes",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def push_update(source: Path, remote: Path) -> str:
    marker = source / "version.txt"
    marker.write_text("2\n", encoding="utf-8")
    git("add", "version.txt", directory=source)
    git("commit", "-m", "update", directory=source)
    git("push", str(remote), "main", directory=source)
    return git("rev-parse", "HEAD", directory=source)


def test_bootstrap_help_is_remote_command_ready() -> None:
    result = subprocess.run(
        ["bash", str(BOOTSTRAP), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "curl -fsSL" in result.stdout
    assert "fast-forward only" in result.stdout


def test_bootstrap_clones_and_forwards_deploy_arguments(
    tmp_path: Path,
) -> None:
    _, remote = create_remote_repository(tmp_path)
    install_directory = tmp_path / "installed"
    deploy_log = tmp_path / "deploy.log"
    result = run_bootstrap(remote, install_directory, deploy_log)
    assert result.returncode == 0, result.stderr
    arguments = deploy_log.read_text(encoding="utf-8")
    assert f"--project-dir {install_directory}" in arguments
    assert "--domain hawk.example.com --skip-admin --yes" in arguments
    assert git("remote", "get-url", "origin", directory=install_directory) == (
        remote.as_uri()
    )


def test_bootstrap_fast_forwards_existing_clean_checkout(
    tmp_path: Path,
) -> None:
    source, remote = create_remote_repository(tmp_path)
    install_directory = tmp_path / "installed"
    deploy_log = tmp_path / "deploy.log"
    first = run_bootstrap(remote, install_directory, deploy_log)
    assert first.returncode == 0, first.stderr
    expected_head = push_update(source, remote)

    second = run_bootstrap(remote, install_directory, deploy_log)
    assert second.returncode == 0, second.stderr
    assert git("rev-parse", "HEAD", directory=install_directory) == expected_head
    assert (install_directory / "version.txt").read_text(encoding="utf-8") == "2\n"


def test_bootstrap_refuses_dirty_or_wrong_checkout(tmp_path: Path) -> None:
    _, remote = create_remote_repository(tmp_path)
    install_directory = tmp_path / "installed"
    deploy_log = tmp_path / "deploy.log"
    first = run_bootstrap(remote, install_directory, deploy_log)
    assert first.returncode == 0, first.stderr

    (install_directory / "scripts" / "deploy_vps.sh").write_text(
        "#!/usr/bin/env bash\nexit 9\n",
        encoding="utf-8",
    )
    dirty = run_bootstrap(remote, install_directory, deploy_log)
    assert dirty.returncode != 0
    assert "local changes" in dirty.stderr

    wrong_directory = tmp_path / "not-a-repository"
    wrong_directory.mkdir()
    wrong = run_bootstrap(remote, wrong_directory, deploy_log)
    assert wrong.returncode != 0
    assert "not the expected Git checkout" in wrong.stderr
