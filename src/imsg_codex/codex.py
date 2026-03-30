#!/usr/bin/env python3
"""Run Codex SDK requests on a dedicated worker thread."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from openai_codex_sdk import Codex, TextInput, ThreadOptions

from .env_config import get_env


LOG = logging.getLogger(__name__)
STOP = object()
RUNTIME_DIR = Path("runtime")
IMESSAGE_CHANNEL_DIR = RUNTIME_DIR / "channels" / "imessage"
THREAD_LOCK_DIR = RUNTIME_DIR / "threads"
THREAD_LOCK_TTL = timedelta(minutes=30)


class CodexWorkerConfig(Protocol):
    model: str
    codex_cwd: str
    reasoning_effort: str
    developer_instructions: str


@dataclass(slots=True)
class WorkerConfig:
    model: str
    codex_cwd: str
    reasoning_effort: str
    developer_instructions: str


def resolve_config() -> WorkerConfig:
    return WorkerConfig(
        model=get_env("IMSG_CODEX_MODEL", default="gpt-5.4-mini") or "gpt-5.4-mini",
        codex_cwd=get_env("IMSG_CODEX_CWD", default=".") or ".",
        reasoning_effort=get_env("IMSG_CODEX_REASONING_EFFORT", default="medium")
        or "medium",
        developer_instructions=(
            get_env(
                "IMSG_CODEX_DEVELOPER_INSTRUCTIONS",
                default=(
                    "You are replying to an iMessage sender. Keep responses concise, natural, "
                    "and directly answer the user's latest message."
                ),
            )
            or ""
        ),
    )


def build_thread_options() -> ThreadOptions:
    config=resolve_config()
    return ThreadOptions(
            model = config.model,
            sandboxMode= "read-only",
            workingDirectory= config.codex_cwd,
            skipGitRepoCheck = True,
            modelReasoningEffort = config.reasoning_effort,
            networkAccessEnabled = True,
            webSearchEnabled = True,
            approvalPolicy = "never",
            additionalDirectories = None,
    )


def create_codex_client() -> Codex:
    try:
        from openai_codex_sdk import Codex
    except ImportError as exc:  # pragma: no cover - import guard for first-run setup
        raise SystemExit(
            "Missing dependency `openai-codex-sdk`. Install project dependencies first, "
            "for example with `uv sync`."
        ) from exc
    return Codex()

codex=create_codex_client()


def _conversation_thread_path(conversation_id: str) -> Path:
    encoded = base64.urlsafe_b64encode(conversation_id.encode("utf-8")).decode("ascii")
    return IMESSAGE_CHANNEL_DIR / encoded


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
    encoded = base64.urlsafe_b64encode(biz_id.encode("utf-8")).decode("ascii")
    return THREAD_LOCK_DIR / f"{encoded}.lock"


def _acquire_thread_lock(biz_id: str) -> Path | None:
    lock_path = _thread_lock_path(biz_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(UTC)
    if lock_path.exists():
        raw_value = lock_path.read_text(encoding="utf-8").strip()
        if raw_value:
            with contextlib.suppress(ValueError):
                locked_at = datetime.fromtimestamp(float(raw_value), tz=UTC)
                if now - locked_at < THREAD_LOCK_TTL:
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
            _write_thread_id(biz_id, thread.id)
    finally:
        _release_thread_lock(lock_path)
    
    return result.final_response.strip() if result.final_response else None
