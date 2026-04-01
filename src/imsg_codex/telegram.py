#!/usr/bin/env python3
"""Listen for messages from configured Telegram chats using python-telegram."""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from concurrent_log_handler import ConcurrentRotatingFileHandler
from telegram.client import Telegram

from .env_config import get_env, get_env_bool, get_env_int

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool
    api_id: int
    api_hash: str
    channel_chat_ids: tuple[int, ...]
    phone_number: str
    database_encryption_key: str
    tdlib_library_path: str
    telegram_files_directory: str
    news_telegram_directory: str


def parse_chat_ids(raw_value: str | None) -> tuple[int, ...]:
    if raw_value in (None, ""):
        return ()

    chat_ids: list[int] = []
    for raw_part in raw_value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            chat_ids.append(int(part))
        except ValueError as exc:
            raise SystemExit(f"Invalid TG_CHANNEL_CHAT_IDS entry: {part!r}") from exc
    return tuple(chat_ids)


def resolve_config() -> TelegramConfig:
    return TelegramConfig(
        enabled=get_env_bool("TG_LISTENER_ENABLED"),
        api_id=get_env_int("TG_API_ID"),
        api_hash=get_env("TG_API_HASH"),
        channel_chat_ids=parse_chat_ids(get_env("TG_CHANNEL_CHAT_IDS")),
        phone_number=get_env("TG_PHONE_NUMBER"),
        database_encryption_key=get_env("TG_DATABASE_ENCRYPTION_KEY"),
        tdlib_library_path=get_env("TDLIB_LIBRARY_PATH"),
        telegram_files_directory=get_env("TELEGRAM_FILES_DIR"),
        news_telegram_directory=get_env("NEWS_TELEGRAM_DIR"),
    )


@dataclass(slots=True)
class TelegramListenerRuntime:
    tg: Telegram
    message_logs_directory: Path
    target_chat_ids: set[int]
    stop_event: threading.Event | None = None
    shutdown_requested: threading.Event = field(init=False, repr=False)
    message_loggers: dict[Path, logging.Logger] = field(init=False, repr=False)
    message_logger_lock: threading.Lock = field(init=False, repr=False)
    chat_cache: dict[int, dict[str, Any] | None] = field(init=False, repr=False)
    chat_cache_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.shutdown_requested = threading.Event()
        self.message_loggers = {}
        self.message_logger_lock = threading.Lock()
        self.chat_cache = {}
        self.chat_cache_lock = threading.Lock()

    def request_shutdown(self, reason: str) -> None:
        if self.shutdown_requested.is_set():
            return
        self.shutdown_requested.set()
        LOG.info("%s", reason)
        if self.stop_event is not None:
            self.stop_event.set()
        self.tg.stop()

    def get_message_log_path(self, chat_id: int) -> Path:
        now = datetime.now()
        log_directory = (
            self.message_logs_directory
            / f"{now:%Y}"
            / f"{now:%m}"
            / f"{now:%d}"
            / f"{now:%H}"
        )
        log_directory.mkdir(parents=True, exist_ok=True)
        return log_directory / f"{chat_id}.md"

    def get_message_logger(self, chat_id: int) -> logging.Logger:
        log_path = self.get_message_log_path(chat_id)
        logger = self.message_loggers.get(log_path)
        if logger is not None:
            return logger

        with self.message_logger_lock:
            logger = self.message_loggers.get(log_path)
            if logger is not None:
                return logger

            logger = logging.Logger(
                f"{__name__}.chat.{chat_id}.{log_path.parent.name}",
                level=logging.INFO,
            )
            logger.propagate = False
            handler = ConcurrentRotatingFileHandler(
                filename=log_path,
                maxBytes=0,
                backupCount=0,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(fmt="%(message)s\n\n---\n\n"))
            logger.addHandler(handler)
            self.message_loggers[log_path] = logger
            return logger

    def get_chat_info(self, chat_id: int) -> dict[str, Any] | None:
        chat_info = self.chat_cache.get(chat_id)
        if chat_info is not None:
            return chat_info

        response = self.tg.get_chat(chat_id)
        response.wait()
        if response.error:
            LOG.warning("Failed to fetch Telegram chat info for chat_id %d", chat_id)
            return None

        chat_info = response.update
        with self.chat_cache_lock:
            existing_chat_info = self.chat_cache.get(chat_id)
            if existing_chat_info is not None:
                return existing_chat_info
            self.chat_cache[chat_id] = chat_info
        return chat_info

    def handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return

        content = msg.get("content", {})
        if content.get("@type") != "messageText":
            return

        chat_id = msg.get("chat_id")
        if chat_id is None:
            return

        chat_info = self.get_chat_info(chat_id)
        if chat_info is None:
            return

        if chat_id in self.target_chat_ids:
            text = content.get("text", {}).get("text", "")
            try:
                self.get_message_logger(chat_id).info("%s", text)
            except Exception:
                LOG.exception(
                    "Failed to write Telegram message log for chat_id %d",
                    chat_id,
                )
            return

        LOG.info(
            "Received message from chat_id %d (%s) and ignoring",
            chat_id,
            chat_info.get("title"),
        )


def run(
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    config = resolve_config()
    if not config.enabled:
        LOG.info("Telegram listener disabled via TG_LISTENER_ENABLED=false")
        return 0

    files_directory = Path(config.telegram_files_directory).resolve()
    files_directory.mkdir(parents=True, exist_ok=True)
    message_logs_directory = Path(config.news_telegram_directory).resolve()
    message_logs_directory.mkdir(parents=True, exist_ok=True)
    target_chat_ids = set(config.channel_chat_ids)

    tg = Telegram(
        api_id=config.api_id,
        api_hash=config.api_hash,
        phone=config.phone_number,
        library_path=config.tdlib_library_path,
        database_encryption_key=config.database_encryption_key,
        files_directory=files_directory,
    )

    state = tg.login()
    print("Telegram login state:", state)

    result = tg.get_me()
    result.wait()
    print(result.update)
    listener_runtime = TelegramListenerRuntime(
        tg=tg,
        message_logs_directory=message_logs_directory,
        target_chat_ids=target_chat_ids,
        stop_event=stop_event,
    )
    tg.add_message_handler(listener_runtime.handle_update)

    if stop_event is not None:

        def watch_stop_event() -> None:
            stop_event.wait()
            listener_runtime.request_shutdown(
                "received shared stop request, shutting down Telegram listener"
            )

        threading.Thread(target=watch_stop_event, daemon=True).start()

    if install_signal_handlers:

        def shutdown(signum: int, _frame: Any) -> None:
            listener_runtime.request_shutdown(
                f"received signal {signum}, shutting down Telegram listener"
            )

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

    try:
        tg.idle(stop_signals=())
    finally:
        tg.stop()

    return 0
