from __future__ import annotations

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
