#!/usr/bin/env python3
"""Application scheduler setup."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .codex import generate_reply
from .env_config import get_env, get_env_bool_optional, get_env_optional
from .imessage_imsg import IMessageSendTarget, send_message_to_target

LOG = logging.getLogger(__name__)
TELEGRAM_NEWS_DIR = Path("runtime/workspace/news/telegram")


def _resolve_imessage_target() -> IMessageSendTarget:
    if chat_id := get_env_optional("SCHEDULER_IMESSAGE_CHAT_ID"):
        return IMessageSendTarget(chat_id=chat_id)
    if chat_guid := get_env_optional("SCHEDULER_IMESSAGE_CHAT_GUID"):
        return IMessageSendTarget(chat_guid=chat_guid)
    if chat_identifier := get_env_optional("SCHEDULER_IMESSAGE_CHAT_IDENTIFIER"):
        return IMessageSendTarget(chat_identifier=chat_identifier)
    if recipient := get_env_optional("SCHEDULER_IMESSAGE_TO"):
        return IMessageSendTarget(recipient=recipient)
    raise ValueError(
        "Missing scheduler iMessage target. Set one of "
        "SCHEDULER_IMESSAGE_CHAT_ID, SCHEDULER_IMESSAGE_CHAT_GUID, "
        "SCHEDULER_IMESSAGE_CHAT_IDENTIFIER, or SCHEDULER_IMESSAGE_TO."
    )


def _previous_hour_directory(now: datetime | None = None) -> Path:
    previous_hour = (now or datetime.now()) - timedelta(hours=1)
    return (
        TELEGRAM_NEWS_DIR
        / f"{previous_hour:%Y}"
        / f"{previous_hour:%m}"
        / f"{previous_hour:%d}"
        / f"{previous_hour:%H}"
    )


def _build_summary_prompt(news_directory: Path) -> str:
    return (
        "请读取下面目录中的所有 Markdown 文件并给出中文摘要。\n"
        f"目录: {news_directory.resolve()}\n"
        "要求:\n"
        "1. 只基于该目录下的 Markdown 文件内容总结。\n"
        "2. 如果目录不存在，或者目录下没有 Markdown 文件，请明确说明。\n"
        "3. 输出适合直接发给 iMessage 的简洁总结。\n"
        "4. 不要编造未在文件中出现的信息。"
    )


def _send_imessage(text: str) -> None:
    target = _resolve_imessage_target()
    LOG.info("sending scheduled summary via iMessage")
    send_message_to_target(target, text)


def resolve_enabled() -> bool:
    return get_env_bool_optional("SCHEDULER_ENABLED", default=False)


def run_hourly_example_task() -> None:
    LOG.info("Running scheduled example task at minute 10 of the hour")
    news_directory = _previous_hour_directory()
    prompt = _build_summary_prompt(news_directory)
    biz_id = get_env("SCHEDULER_CODEX_BIZ_ID")
    summary = asyncio.run(generate_reply(biz_id, prompt))
    if summary is None:
        raise RuntimeError("Codex returned no summary for scheduled hourly task.")
    _send_imessage(summary)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_hourly_example_task,
        trigger=CronTrigger(minute=10),
        id="hourly-example-task",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
