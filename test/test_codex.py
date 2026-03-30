from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from imsg_codex import codex


def _channel_path(channel_id: str) -> Path:
    encoded = base64.urlsafe_b64encode(channel_id.encode("utf-8")).decode("ascii")
    return Path(
        "/Users/jia/Documents/git/imsg-codex/runtime/channels/imessage", encoded
    )


def _thread_path(thread_id: str) -> Path:
    encoded = base64.urlsafe_b64encode(thread_id.encode("utf-8")).decode("ascii")
    return Path(
        "/Users/jia/Documents/git/imsg-codex/runtime/threads",
        f"{encoded}.lock",
    )


def _unique_channel_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def test_generate_reply_persists_thread_mapping_with_real_codex() -> None:
    channel_id = _unique_channel_id("persist")
    token = uuid.uuid4().hex[:12]

    result = asyncio.run(
        codex.generate_reply(
            channel_id,
            f"Reply with exactly this token and nothing else: {token}",
        )
    )

    channel_file = _channel_path(channel_id)
    assert result is not None
    assert token in result
    assert channel_file.exists()


def test_generate_reply_reuses_real_codex_thread_context() -> None:
    channel_id = _unique_channel_id("resume")
    token = uuid.uuid4().hex[:12]

    first_result = asyncio.run(
        codex.generate_reply(
            channel_id,
            f"Remember this token for later: {token}. Reply with exactly: stored",
        )
    )

    second_result = asyncio.run(
        codex.generate_reply(
            channel_id,
            "What token did I ask you to remember earlier? Reply with the token only.",
        )
    )

    channel_file = _channel_path(channel_id)
    first_thread_id = channel_file.read_text(encoding="utf-8").strip()

    assert first_result == "stored"
    assert second_result is not None
    assert token in second_result
    assert channel_file.read_text(encoding="utf-8").strip() == first_thread_id


def test_generate_reply_returns_try_later_for_active_lock_without_running_turn(
) -> None:
    channel_id = _unique_channel_id("locked")
    thread = codex.codex.start_thread(codex.build_thread_options())
    asyncio.run(thread.run("Initial message to get thread id"))
    assert thread.id is not None

    channel_file = _channel_path(channel_id)
    channel_file.parent.mkdir(parents=True, exist_ok=True)
    channel_file.write_text(thread.id, encoding="utf-8")

    lock_path = _thread_path(channel_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(datetime.now(UTC).timestamp()), encoding="utf-8")

    result = asyncio.run(
        codex.generate_reply(channel_id, "Reply with exactly: impossible")
    )

    assert result == "try later"
    assert lock_path.exists()


def test_generate_reply_clears_stale_lock_after_real_codex_run() -> None:
    channel_id = _unique_channel_id("stale")
    thread = codex.codex.start_thread(codex.build_thread_options())
    asyncio.run(thread.run("Initial message to get thread id"))
    assert thread.id is not None

    channel_file = _channel_path(channel_id)
    channel_file.parent.mkdir(parents=True, exist_ok=True)
    channel_file.write_text(thread.id, encoding="utf-8")

    lock_path = _thread_path(channel_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stale_time = datetime.now(UTC) - codex.THREAD_LOCK_TTL - timedelta(seconds=1)
    lock_path.write_text(str(stale_time.timestamp()), encoding="utf-8")

    result = asyncio.run(
        codex.generate_reply(
            channel_id,
            "Reply with exactly: stale-lock-cleared",
        )
    )

    assert result is not None
    assert "stale-lock-cleared" in result
    assert not lock_path.exists()
