#!/usr/bin/env python3
"""Listen to iMessage events and wrap the external `imsg` CLI."""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .env_config import get_env, get_env_bool

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class IMessageConfig:
    """Merged listener + transport configuration for the iMessage module."""

    enabled: bool
    binary: str
    log_level: str


def resolve_config() -> IMessageConfig:
    """Read listener and transport configuration from env."""

    return IMessageConfig(
        enabled=get_env_bool("IMESSAGE_ENABLED", default=True),
        binary=get_env("IMSG_BIN", default="imsg") or "imsg",
        log_level=get_env("IMSG_LOG_LEVEL", default="info") or "info",
    )


def start_rpc_process() -> subprocess.Popen[str]:
    """Start the long-lived `imsg rpc` subprocess."""

    config = resolve_config()
    command = [
        config.binary,
        "rpc",
        "--json",
        "--log-level",
        config.log_level,
    ]
    LOG.info("starting: %s", " ".join(command))
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def send_rpc_request(
    process: subprocess.Popen[str],
    request_id: int,
    method: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Send one JSON-RPC request line to the running `imsg rpc` process."""

    assert process.stdin is not None
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        payload["params"] = params
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def read_stderr(stderr: Iterable[str]) -> None:
    """Forward `imsg` stderr into structured application logs."""

    for raw_line in stderr:
        line = raw_line.strip()
        if line:
            LOG.warning("imsg stderr: %s", line)


def extract_result(obj: dict[str, Any]) -> dict[str, Any]:
    """Validate the shape of an RPC success response."""

    result = obj.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected RPC result payload: {obj}")
    return result


def format_json_for_log(payload: dict[str, Any]) -> str:
    """Pretty-print transport payloads for logs."""

    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def is_incoming_user_message(message: dict[str, Any]) -> bool:
    """Return True only for user-authored inbound text messages."""

    if message.get("is_from_me") is True:
        return False
    if message.get("from_me") is True:
        return False
    return message.get("text") not in (None, "")


def chat_key_for_message(message: dict[str, Any]) -> str:
    """Build a stable chat key from the available message routing fields."""

    for key in ("chat_guid", "chat_identifier", "chat_id", "sender"):
        value = message.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    raise ValueError(f"unable to determine chat key from message: {message}")


def build_send_command(
    message: dict[str, Any],
    reply_text: str,
) -> list[str]:
    """Build an `imsg send` command targeting the same chat as the source message."""

    config = resolve_config()
    command = [config.binary, "send", "--json"]

    chat_id = message.get("chat_id")
    chat_guid = message.get("chat_guid")
    chat_identifier = message.get("chat_identifier")
    sender = message.get("sender")

    if chat_id not in (None, ""):
        command.extend(["--chat-id", str(chat_id)])
    elif chat_guid not in (None, ""):
        command.extend(["--chat-guid", str(chat_guid)])
    elif chat_identifier not in (None, ""):
        command.extend(["--chat-identifier", str(chat_identifier)])
    elif sender not in (None, ""):
        command.extend(["--to", str(sender)])
    else:
        raise ValueError(f"unable to determine reply target from message: {message}")

    command.extend(["--text", reply_text])
    return command


def send_reply(
    message: dict[str, Any],
    reply_text: str,
) -> None:
    """Send a reply message back through the `imsg` CLI."""

    command = build_send_command(message, reply_text)
    LOG.info("sending reply via chat=%s", chat_key_for_message(message))
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(
            "failed to send iMessage reply: "
            f"rc={completed.returncode} stderr={completed.stderr.strip()!r}"
        )


def run(
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    """Run the long-lived iMessage listener loop."""

    config = resolve_config()
    if not config.enabled:
        LOG.info("iMessage listener disabled via IMESSAGE_ENABLED=false")
        return 0

    process = start_rpc_process()

    shutdown_requested = threading.Event()
    subscription_id: int | None = None
    pending_request_ids = {"subscribe": 1, "unsubscribe": 2}
    forced_shutdown = False

    def request_shutdown(reason: str) -> None:
        nonlocal forced_shutdown
        if shutdown_requested.is_set():
            return

        shutdown_requested.set()
        if stop_event is not None:
            stop_event.set()
        LOG.info("%s", reason)

        if process.poll() is None:
            try:
                if subscription_id is not None:
                    send_rpc_request(
                        process,
                        pending_request_ids["unsubscribe"],
                        "watch.unsubscribe",
                        {"subscription": subscription_id},
                    )
            except BrokenPipeError, OSError:
                LOG.debug("unable to send unsubscribe request during shutdown")

            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass

            try:
                process.terminate()
                forced_shutdown = True
            except OSError:
                pass

    if stop_event is not None:

        def watch_stop_event() -> None:
            stop_event.wait()
            request_shutdown(
                "received shared stop request, shutting down iMessage listener"
            )

        threading.Thread(target=watch_stop_event, daemon=True).start()

    if install_signal_handlers:

        def shutdown(signum: int, _frame: Any) -> None:
            request_shutdown(f"received signal {signum}, shutting down")

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

    if process.stderr is not None:
        threading.Thread(
            target=read_stderr, args=(process.stderr,), daemon=True
        ).start()

    try:
        send_rpc_request(
            process,
            pending_request_ids["subscribe"],
            "watch.subscribe",
        )

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("non-json rpc output: %s", line)
                continue

            if obj.get("id") == pending_request_ids["subscribe"]:
                if "error" in obj:
                    raise RuntimeError(f"watch.subscribe failed: {obj['error']}")
                result = extract_result(obj)
                raw_subscription = result.get("subscription")
                if not isinstance(raw_subscription, int):
                    raise RuntimeError(f"invalid subscription response: {obj}")
                subscription_id = raw_subscription
                LOG.info("subscribed to watch stream subscription=%s", subscription_id)
                continue

            if obj.get("id") == pending_request_ids["unsubscribe"]:
                if "error" in obj:
                    LOG.warning("watch.unsubscribe failed: %s", obj["error"])
                else:
                    LOG.info("unsubscribed from watch stream")
                continue

            if obj.get("method") != "message":
                if "error" in obj:
                    LOG.error(
                        "rpc error: %s", json.dumps(obj["error"], ensure_ascii=False)
                    )
                continue

            params = obj.get("params")
            if not isinstance(params, dict):
                LOG.warning("message notification missing params: %s", obj)
                continue

            message = params.get("message")
            if not isinstance(message, dict):
                LOG.warning("message notification missing message payload: %s", obj)
                continue

            if not is_incoming_user_message(message):
                LOG.debug(
                    "ignoring non-incoming message: %s",
                    json.dumps(message, ensure_ascii=False),
                )
                continue

            try:
                chat_key = chat_key_for_message(message)
            except ValueError:
                LOG.exception("unable to determine chat key for incoming message")
                continue

            LOG.info(
                "incoming iMessage chat=%s payload:\n%s",
                chat_key,
                format_json_for_log(message),
            )
    finally:
        if process.poll() is None:
            try:
                process.terminate()
                forced_shutdown = True
            except OSError:
                pass

        rc = process.wait()
        normalized_rc = 0 if forced_shutdown else rc
        if normalized_rc == 0:
            LOG.info("imsg rpc exited cleanly")
        else:
            LOG.error("imsg rpc exited with code %s", normalized_rc)

    return 0 if normalized_rc == 0 else normalized_rc
