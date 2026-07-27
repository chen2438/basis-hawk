from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from basis_hawk import cli


def test_admin_password_prompt_explains_policy_and_retries(
    monkeypatch,
    capsys,
) -> None:
    answers = iter(
        [
            "too-short",
            "long-enough-password",
            "different-password",
            "long-enough-password",
            "long-enough-password",
        ]
    )
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)

    password = cli.prompt_admin_password()

    assert password == "long-enough-password"
    assert prompts == [
        "Administrator password: ",
        "Administrator password: ",
        "Confirm password: ",
        "Administrator password: ",
        "Confirm password: ",
    ]
    output = capsys.readouterr().out
    assert "must contain at least 12 characters." in output
    assert "at least 12 characters; try again" in output
    assert "passwords do not match; try again" in output


async def test_rotate_totp_cli_verifies_password_and_prints_uri(
    monkeypatch,
    capsys,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            assert url == "sqlite://"

        async def initialize(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeAuthService:
        def __init__(self, database, cipher, *, session_hours: int) -> None:
            assert session_hours == 12

        async def rotate_admin_totp(
            self,
            username: str,
            password: str,
        ) -> str:
            calls.append((username, password))
            return "otpauth://totp/test?secret=TESTONLY"

    monkeypatch.setattr(
        cli,
        "get_config",
        lambda: SimpleNamespace(
            credential_master_key=SecretStr(
                "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
            ),
            database_url="sqlite://",
            session_hours=12,
        ),
    )
    monkeypatch.setattr(cli, "Database", FakeDatabase)
    monkeypatch.setattr(cli, "AuthService", FakeAuthService)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda prompt: "correct horse battery staple",
    )

    result = await cli.rotate_admin_totp("admin")

    assert result == 0
    assert calls == [("admin", "correct horse battery staple")]
    output = capsys.readouterr().out
    assert "signs out every active session" in output
    assert output.rstrip().endswith(
        "This URI will not be displayed again; store it securely."
    )
