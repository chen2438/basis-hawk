from pathlib import Path

from basis_hawk.api import _safe_frontend_file


def test_spa_file_lookup_stays_inside_frontend_root(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    nested = frontend / "docs"
    nested.mkdir(parents=True)
    allowed = nested / "guide.txt"
    allowed.write_text("public", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")

    assert _safe_frontend_file(frontend, "docs/guide.txt") == allowed
    assert _safe_frontend_file(frontend, "../secret.txt") is None
    assert _safe_frontend_file(frontend, "/etc/passwd") is None


def test_spa_file_lookup_rejects_outbound_symlink(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    (frontend / "linked.txt").symlink_to(secret)

    assert _safe_frontend_file(frontend, "linked.txt") is None
