from __future__ import annotations

from _pytest.monkeypatch import MonkeyPatch

from imsg_codex import imessage_imsg


def test_prepare_text_argument_preserves_normal_text() -> None:
    assert imessage_imsg._prepare_text_argument("hello") == "hello"


def test_prepare_text_argument_prefixes_leading_dash() -> None:
    assert imessage_imsg._prepare_text_argument("- bullet") == "\n- bullet"


def test_build_send_command_for_target_uses_safe_text_argument(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        imessage_imsg,
        "resolve_config",
        lambda: imessage_imsg.IMessageConfig(
            enabled=True,
            binary="imsg",
            log_level="info",
        ),
    )

    command = imessage_imsg.build_send_command_for_target(
        imessage_imsg.IMessageSendTarget(recipient="zjj@rabbitpig.org"),
        "- bullet",
    )

    assert command == [
        "imsg",
        "send",
        "--json",
        "--to",
        "zjj@rabbitpig.org",
        "--text",
        "\n- bullet",
    ]
