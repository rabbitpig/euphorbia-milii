#!/usr/bin/env python3
"""Run Codex SDK requests on a dedicated worker thread."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO

from openai_codex_sdk import Codex, ThreadOptions

from .env_config import get_env

LOG = logging.getLogger(__name__)


ReasoningEffort = Literal["minimal", "low", "medium", "high"]


@dataclass(slots=True)
class WorkerConfig:
    model: str
    codex_cwd: str
    reasoning_effort: ReasoningEffort
    developer_instructions: str
    channel_imessage_dir: str
    channel_thread_lock_dir: str
    channel_thread_lock_ttl: timedelta


def resolve_reasoning_effort() -> ReasoningEffort:
    configured = get_env("CODEX_REASONING_EFFORT")
    if configured == "minimal":
        return "minimal"
    if configured == "low":
        return "low"
    if configured == "medium":
        return "medium"
    if configured == "high":
        return "high"
    LOG.warning(
        "unsupported CODEX_REASONING_EFFORT=%r, falling back to 'medium'",
        configured,
    )
    return "medium"


def resolve_config() -> WorkerConfig:
    return WorkerConfig(
        model=get_env("CODEX_MODEL"),
        codex_cwd=get_env("CODEX_CWD"),
        reasoning_effort=resolve_reasoning_effort(),
        developer_instructions=get_env("CODEX_DEVELOPER_INSTRUCTIONS"),
        channel_imessage_dir=get_env("CODEX_CHANNEL_IMESSAGE_DIR"),
        channel_thread_lock_dir=get_env("CODEX_THREAD_LOCK_DIR"),
        channel_thread_lock_ttl=timedelta(minutes=30),
    )


def build_thread_options() -> ThreadOptions:
    config = resolve_config()
    return ThreadOptions(
        model=config.model,
        sandboxMode="read-only",
        workingDirectory=config.codex_cwd,
        skipGitRepoCheck=True,
        modelReasoningEffort=config.reasoning_effort,
        networkAccessEnabled=True,
        webSearchEnabled=True,
        approvalPolicy="never",
        additionalDirectories=None,
    )


def create_codex_client() -> Codex:
    try:
        from openai_codex_sdk import Codex
    except ImportError as exc:  # pragma: no cover - import guard for first-run setup
        raise SystemExit(
            "Missing dependency `openai-codex-sdk`. "
            "Install project dependencies first, "
            "for example with `uv sync`."
        ) from exc
    return Codex()


codex = create_codex_client()


def _conversation_thread_path(conversation_id: str) -> Path:
    config = resolve_config()
    encoded = base64.urlsafe_b64encode(conversation_id.encode("utf-8")).decode("ascii")
    return Path(config.channel_imessage_dir, encoded).resolve()


def _read_thread_id(conversation_id: str) -> str | None:
    path = _conversation_thread_path(conversation_id)
    if not path.exists():
        return None

    thread_id = path.read_text(encoding="utf-8").strip()
    return thread_id or None


def _write_thread_id(conversation_id: str, thread_id: str) -> None:
    path = _conversation_thread_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(thread_id, encoding="utf-8")


def _thread_lock_path(biz_id: str) -> Path:
    config = resolve_config()
    encoded = base64.urlsafe_b64encode(biz_id.encode("utf-8")).decode("ascii")
    return Path(config.channel_thread_lock_dir, f"{encoded}.lock")


def _acquire_thread_lock(biz_id: str) -> TextIO | None:
    lock_path = _thread_lock_path(biz_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(now.timestamp()))
    lock_file.flush()
    return lock_file


def _release_thread_lock(lock_file: TextIO | None) -> None:
    if lock_file is None:
        return
    with contextlib.suppress(OSError):
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    lock_file.close()


def _format_compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(value)


def _format_usage_suffix(
    input_tokens: int, cached_input_tokens: int, output_tokens: int
) -> str:
    return (
        f"\n\n({_format_compact_number(input_tokens)}"
        f"/{_format_compact_number(cached_input_tokens)}"
        f"/{_format_compact_number(output_tokens)})"
    )


async def generate_reply(
    biz_id: str,
    message: str,
) -> str | None:
    lock_path = _acquire_thread_lock(biz_id)
    if lock_path is None:
        return "try later"

    thread_options = build_thread_options()

    thread_id = None
    if biz_id is not None:
        thread_id = _read_thread_id(biz_id)

    if thread_id is None:
        thread = codex.start_thread(thread_options)
    else:
        thread = codex.resume_thread(thread_id, thread_options)

    try:
        result = await thread.run(message)
        if result.usage is not None:
            LOG.info(
                "codex usage biz_id=%s input_tokens=%s cached_input_tokens=%s output_tokens=%s",
                biz_id,
                result.usage.input_tokens,
                result.usage.cached_input_tokens,
                result.usage.output_tokens,
            )

        if thread_id is None and biz_id is not None:
            if thread.id is None:
                raise RuntimeError("Codex thread id is missing after thread creation.")
            _write_thread_id(biz_id, thread.id)
    finally:
        _release_thread_lock(lock_path)

    usage_suffix = ""
    if result.usage is not None:
        usage_suffix = _format_usage_suffix(
            result.usage.input_tokens,
            result.usage.cached_input_tokens,
            result.usage.output_tokens,
        )

    if result.final_response:
        return result.final_response.strip() + usage_suffix
    if usage_suffix:
        return usage_suffix.strip()
    return None
