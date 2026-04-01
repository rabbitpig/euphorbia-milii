#!/usr/bin/env python3
"""Run the iMessage and Telegram listeners together in one process."""

from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler

from . import imessage_imsg, scheduler, telegram
from .env_config import get_env_bool, load_dotenv
from .logging_config import configure_logging

LOG = logging.getLogger("imsg-codex-main")
Listener = Callable[..., int]


@dataclass(slots=True)
class ListenerResult:
    name: str
    rc: int | None = None
    error: BaseException | None = None
    traceback_text: str | None = None


def resolve_verbose() -> bool:
    load_dotenv()
    return get_env_bool("CODEX_VERBOSE")


def run_listener(
    name: str,
    runner: Listener,
    stop_event: threading.Event,
    results: queue.Queue[ListenerResult],
) -> None:
    try:
        rc = runner(stop_event=stop_event, install_signal_handlers=False)
    except BaseException as exc:
        results.put(
            ListenerResult(
                name=name,
                error=exc,
                traceback_text=traceback.format_exc(),
            )
        )
    else:
        results.put(ListenerResult(name=name, rc=rc))
    finally:
        stop_event.set()


def build_listener_threads(
    stop_event: threading.Event,
    results: queue.Queue[ListenerResult],
) -> list[threading.Thread]:
    threads: list[threading.Thread] = []
    imessage_config = imessage_imsg.resolve_config()
    telegram_config = telegram.resolve_config()

    if imessage_config.enabled:
        threads.append(
            threading.Thread(
                target=run_listener,
                name="imessage-listener",
                args=("imessage", imessage_imsg.run, stop_event, results),
            )
        )

    if telegram_config.enabled:
        threads.append(
            threading.Thread(
                target=run_listener,
                name="telegram-listener",
                args=("telegram", telegram.run, stop_event, results),
            )
        )

    return threads


def shutdown_listeners(
    stop_event: threading.Event,
    threads: Sequence[threading.Thread],
) -> None:
    stop_event.set()
    for thread in threads:
        thread.join()


def maybe_start_scheduler() -> BackgroundScheduler | None:
    if not scheduler.resolve_enabled():
        return None

    background_scheduler = scheduler.create_scheduler()
    background_scheduler.start()
    LOG.info("started APScheduler with %s job(s)", len(background_scheduler.get_jobs()))
    return background_scheduler


def main() -> int:
    if len(sys.argv) > 1:
        raise SystemExit(
            "imsg-codex no longer accepts CLI arguments; configure it via .env."
        )

    configure_logging(resolve_verbose())

    stop_event = threading.Event()
    results: queue.Queue[ListenerResult] = queue.Queue()
    threads = build_listener_threads(stop_event, results)
    background_scheduler = maybe_start_scheduler()
    if not threads and background_scheduler is None:
        LOG.error(
            "no listeners or scheduler enabled; set IMESSAGE_ENABLED=true, "
            "TG_LISTENER_ENABLED=true, or SCHEDULER_ENABLED=true"
        )
        return 2

    def request_stop(signum: int, _frame: Any) -> None:
        if stop_event.is_set():
            return
        LOG.info("received signal %s, stopping listeners", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    LOG.info("starting %s listener(s)", len(threads))
    for thread in threads:
        thread.start()

    completed: dict[str, ListenerResult] = {}
    try:
        if not threads:
            stop_event.wait()
            return 0

        while len(completed) < len(threads):
            result = results.get()
            completed[result.name] = result
            if result.error is not None:
                LOG.error(
                    "%s listener crashed:\n%s",
                    result.name,
                    result.traceback_text,
                )
            else:
                LOG.info("%s listener exited rc=%s", result.name, result.rc)
            stop_event.set()
    finally:
        if background_scheduler is not None:
            background_scheduler.shutdown(wait=False)
        shutdown_listeners(stop_event, threads)

    for result in completed.values():
        if result.error is not None:
            LOG.exception(
                "%s listener failed with exception:\n%s",
                result.name,
                result.traceback_text,
            )
            return 1
        if result.rc is not None and result.rc != 0:
            return result.rc
    return 0
