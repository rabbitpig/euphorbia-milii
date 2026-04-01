#!/usr/bin/env python3
"""Run Codex SDK requests on a dedicated worker thread."""

from __future__ import annotations

import base64
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

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


def _acquire_thread_lock(biz_id: str) -> Path | None:
    config = resolve_config()

    lock_path = _thread_lock_path(biz_id)
    # TODO 这里锁的实现是否存在问题
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    if lock_path.exists():
        raw_value = lock_path.read_text(encoding="utf-8").strip()
        if raw_value:
            with contextlib.suppress(ValueError):
                locked_at = datetime.fromtimestamp(float(raw_value), tz=UTC)
                if now - locked_at < config.channel_thread_lock_ttl:
                    return None
        lock_path.unlink(missing_ok=True)

    lock_path.write_text(str(now.timestamp()), encoding="utf-8")
    return lock_path


def _release_thread_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    lock_path.unlink(missing_ok=True)


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

        if thread_id is None and biz_id is not None:
            if thread.id is None:
                raise RuntimeError("Codex thread id is missing after thread creation.")
            _write_thread_id(biz_id, thread.id)
    finally:
        _release_thread_lock(lock_path)

    return result.final_response.strip() if result.final_response else None
