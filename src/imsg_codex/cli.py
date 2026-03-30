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

from . import imessage_imsg , telegram
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
    return get_env_bool("IMSG_CODEX_VERBOSE", "TDLIB_VERBOSE")


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


def shutdown_listeners(stop_event: threading.Event, threads: Sequence[threading.Thread]) -> None:
    stop_event.set()
    for thread in threads:
        thread.join()


def main() -> int:
    if len(sys.argv) > 1:
        raise SystemExit("imsg-codex no longer accepts CLI arguments; configure it via .env.")

    configure_logging(resolve_verbose())

    stop_event = threading.Event()
    results: queue.Queue[ListenerResult] = queue.Queue()
    threads = build_listener_threads(stop_event, results)
    if not threads:
        LOG.error(
            "no listeners enabled; set IMESSAGE_ENABLED=true or TG_LISTENER_ENABLED=true"
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
        while len(completed) < len(threads):
            result = results.get()
            completed[result.name] = result
            if result.error is not None:
                LOG.error("%s listener crashed:\n%s", result.name, result.traceback_text)
            else:
                LOG.info("%s listener exited rc=%s", result.name, result.rc)
            stop_event.set()
    finally:
        shutdown_listeners(stop_event, threads)

    for result in completed.values():
        if result.error is not None:
            return 1
        if result.rc not in (None, 0):
            return result.rc
    return 0
