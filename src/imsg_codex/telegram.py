#!/usr/bin/env python3
"""Listen for messages from a specific Telegram channel using TDLib."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import platform
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .env_config import get_env, get_env_bool, get_env_int

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class TargetChannel:
    chat_id: int
    title: str
    supergroup_id: int
    username: str | None


@dataclass(slots=True)
class TelegramConfig:
    enabled: bool
    api_id: int | None
    api_hash: str | None
    channel_username: str | None
    channel_chat_id: int | None
    phone_number: str | None
    tdjson_lib: str | None
    state_dir: str
    database_key: str
    system_language_code: str
    dump_raw: bool
    verbose: bool


class TDLibError(RuntimeError):
    """Raised when TDLib returns an error object."""


class TDJsonClient:
    """Minimal TDLib JSON wrapper built on top of tdjson."""

    def __init__(
        self,
        library_path: str | None = None,
        receive_timeout: float = 1.0,
    ) -> None:
        tdjson_path = library_path or ctypes.util.find_library("tdjson")
        if not tdjson_path:
            raise SystemExit(
                "Unable to find the TDLib shared library `tdjson`.\n"
                "Install TDLib first, then pass "
                "--tdjson-lib /full/path/to/libtdjson.dylib "
                "or make it discoverable via the system library path."
            )

        tdjson = ctypes.CDLL(tdjson_path)

        self._td_create_client_id = tdjson.td_create_client_id
        self._td_create_client_id.restype = ctypes.c_int
        self._td_create_client_id.argtypes = []

        self._td_receive = tdjson.td_receive
        self._td_receive.restype = ctypes.c_char_p
        self._td_receive.argtypes = [ctypes.c_double]

        self._td_send = tdjson.td_send
        self._td_send.restype = None
        self._td_send.argtypes = [ctypes.c_int, ctypes.c_char_p]

        self._td_execute = tdjson.td_execute
        self._td_execute.restype = ctypes.c_char_p
        self._td_execute.argtypes = [ctypes.c_char_p]

        self.client_id = self._td_create_client_id()
        self.receive_timeout = receive_timeout
        self._next_extra = 1

    def execute(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        raw = self._td_execute(json.dumps(payload).encode("utf-8"))
        return json.loads(raw.decode("utf-8")) if raw else None

    def send(self, payload: dict[str, Any]) -> str:
        extra = str(self._next_extra)
        self._next_extra += 1
        request = dict(payload)
        request["@extra"] = extra
        self._td_send(self.client_id, json.dumps(request).encode("utf-8"))
        return extra

    def receive(self, timeout: float | None = None) -> dict[str, Any] | None:
        raw = self._td_receive(self.receive_timeout if timeout is None else timeout)
        return json.loads(raw.decode("utf-8")) if raw else None

    def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 60.0,
        update_sink: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        extra = self.send(payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"TDLib request timed out: {payload['@type']}")

            event = self.receive(min(self.receive_timeout, remaining))
            if event is None:
                continue

            event_extra = event.get("@extra")
            if event_extra == extra:
                if event.get("@type") == "error":
                    raise TDLibError(
                        f"{payload['@type']} failed: code={event.get('code')} "
                        f"message={event.get('message')!r}"
                    )
                return event

            if update_sink is not None:
                update_sink.append(event)


def resolve_config() -> TelegramConfig:
    config = TelegramConfig(
        enabled=get_env_bool("TG_LISTENER_ENABLED", default=True),
        api_id=get_env_int("TG_API_ID"),
        api_hash=get_env("TG_API_HASH"),
        channel_username=get_env("TG_CHANNEL_USERNAME"),
        channel_chat_id=get_env_int("TG_CHANNEL_CHAT_ID"),
        phone_number=get_env("TG_PHONE_NUMBER"),
        tdjson_lib=get_env("TDJSON_LIB"),
        state_dir=get_env("TDLIB_STATE_DIR", default=".tdlib-state") or ".tdlib-state",
        database_key=get_env("TDLIB_DATABASE_KEY", default="") or "",
        system_language_code=(
            get_env("TDLIB_SYSTEM_LANGUAGE_CODE", default="en") or "en"
        ),
        dump_raw=get_env_bool("TDLIB_DUMP_RAW"),
        verbose=get_env_bool("TDLIB_VERBOSE"),
    )
    if not config.enabled:
        return config
    if config.api_id is None:
        raise SystemExit("Telegram listener requires TG_API_ID.")
    if not config.api_hash:
        raise SystemExit("Telegram listener requires TG_API_HASH.")
    if config.channel_chat_id is None and not config.channel_username:
        raise SystemExit(
            "Telegram listener requires one of TG_CHANNEL_CHAT_ID "
            "or TG_CHANNEL_USERNAME."
        )
    return config


def prompt_input(prompt: str, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise SystemExit(f"TDLib requires interactive input for: {prompt}")
    if secret:
        import getpass

        return getpass.getpass(prompt)
    return input(prompt)


def tdlib_parameters(args: TelegramConfig, state_dir: Path) -> dict[str, Any]:
    return {
        "@type": "setTdlibParameters",
        "use_test_dc": False,
        "database_directory": str(state_dir / "db"),
        "files_directory": str(state_dir / "files"),
        "database_encryption_key": args.database_key,
        "use_file_database": True,
        "use_chat_info_database": True,
        "use_message_database": True,
        "use_secret_chats": False,
        "api_id": args.api_id,
        "api_hash": args.api_hash,
        "system_language_code": args.system_language_code,
        "device_model": platform.machine() or "unknown",
        "system_version": platform.platform(),
        "application_version": "imsg-codex/0.1.0",
    }


def drain_updates(
    buffered_updates: list[dict[str, Any]],
    auth_state: dict[str, Any],
) -> None:
    while buffered_updates:
        event = buffered_updates.pop(0)
        if event.get("@type") == "updateAuthorizationState":
            auth_state["authorization_state"] = event["authorization_state"]


def handle_authorization_state(
    client: TDJsonClient,
    args: TelegramConfig,
    state_dir: Path,
    auth_state: dict[str, Any],
    buffered_updates: list[dict[str, Any]],
) -> bool:
    state = auth_state["authorization_state"]
    state_type = state["@type"]
    LOG.debug("authorization state: %s", state_type)

    if state_type == "authorizationStateWaitTdlibParameters":
        client.request(tdlib_parameters(args, state_dir), update_sink=buffered_updates)
    elif state_type == "authorizationStateWaitPhoneNumber":
        phone_number = args.phone_number or os.environ.get("TG_PHONE_NUMBER")
        if not phone_number:
            phone_number = prompt_input("Telegram phone number: ")
        client.request(
            {
                "@type": "setAuthenticationPhoneNumber",
                "phone_number": phone_number,
                "settings": {
                    "@type": "phoneNumberAuthenticationSettings",
                    "allow_flash_call": False,
                    "allow_missed_call": False,
                    "is_current_phone_number": False,
                    "allow_sms_retriever_api": False,
                },
            },
            update_sink=buffered_updates,
        )
    elif state_type == "authorizationStateWaitCode":
        code = prompt_input("Telegram login code: ")
        client.request(
            {"@type": "checkAuthenticationCode", "code": code},
            update_sink=buffered_updates,
        )
    elif state_type == "authorizationStateWaitPassword":
        password = prompt_input("Telegram 2FA password: ", secret=True)
        client.request(
            {"@type": "checkAuthenticationPassword", "password": password},
            update_sink=buffered_updates,
        )
    elif state_type == "authorizationStateReady":
        return True
    elif state_type == "authorizationStateClosed":
        raise SystemExit("TDLib authorization closed unexpectedly.")
    elif state_type == "authorizationStateLoggingOut":
        raise SystemExit("TDLib is logging out.")
    elif state_type == "authorizationStateClosing":
        raise SystemExit("TDLib is closing.")
    elif state_type == "authorizationStateWaitEmailAddress":
        email = prompt_input("Telegram email address: ")
        client.request(
            {"@type": "setAuthenticationEmailAddress", "email_address": email},
            update_sink=buffered_updates,
        )
    elif state_type == "authorizationStateWaitEmailCode":
        code = prompt_input("Telegram email code: ")
        client.request(
            {
                "@type": "checkAuthenticationEmailCode",
                "code": {"@type": "emailAddressAuthenticationCode", "code": code},
            },
            update_sink=buffered_updates,
        )
    elif state_type == "authorizationStateWaitOtherDeviceConfirmation":
        link = state.get("link")
        raise SystemExit(
            "TDLib requires other-device confirmation. "
            f"Open the provided link in Telegram: {link}"
        )
    elif state_type == "authorizationStateWaitRegistration":
        raise SystemExit(
            "This account is not registered yet; automatic signup isn't implemented."
        )
    else:
        raise SystemExit(f"Unsupported TDLib authorization state: {state_type}")

    drain_updates(buffered_updates, auth_state)
    return False


def authorize(client: TDJsonClient, args: TelegramConfig, state_dir: Path) -> None:
    buffered_updates: list[dict[str, Any]] = []
    version = client.execute({"@type": "getOption", "name": "version"})
    if version:
        LOG.info("connected to TDLib version=%s", version.get("value", {}).get("value"))

    auth_state = {
        "authorization_state": client.request(
            {"@type": "getAuthorizationState"},
            update_sink=buffered_updates,
        )
    }
    while not handle_authorization_state(
        client,
        args,
        state_dir,
        auth_state,
        buffered_updates,
    ):
        if buffered_updates:
            drain_updates(buffered_updates, auth_state)
            continue
        event = client.receive(60.0)
        if event is None:
            continue
        if event.get("@type") == "updateAuthorizationState":
            auth_state["authorization_state"] = event["authorization_state"]

    me = client.request({"@type": "getMe"})
    user = me.get("usernames", {})
    usernames = user.get("active_usernames") or []
    LOG.info(
        "authorized as id=%s name=%s username=%s",
        me.get("id"),
        " ".join(
            part for part in [me.get("first_name"), me.get("last_name")] if part
        ).strip(),
        usernames[0] if usernames else None,
    )


def resolve_target_channel(client: TDJsonClient, args: TelegramConfig) -> TargetChannel:
    if args.channel_chat_id is not None:
        chat = client.request({"@type": "getChat", "chat_id": args.channel_chat_id})
    else:
        if args.channel_username is None:
            raise SystemExit(
                "TG_CHANNEL_USERNAME is required when TG_CHANNEL_CHAT_ID is unset."
            )
        channel_username = args.channel_username.lstrip("@")
        chat = client.request(
            {"@type": "searchPublicChat", "username": channel_username}
        )

    chat_type = chat.get("type", {})
    if chat_type.get("@type") != "chatTypeSupergroup" or not chat_type.get(
        "is_channel"
    ):
        raise SystemExit(
            "Resolved chat is not a channel. TDLib marks channels as "
            "`chatTypeSupergroup` with `is_channel=true`."
        )

    supergroup_id = int(chat_type["supergroup_id"])
    supergroup = client.request(
        {"@type": "getSupergroup", "supergroup_id": supergroup_id}
    )
    usernames = supergroup.get("usernames", {})
    active_usernames = usernames.get("active_usernames") or []
    username: str | None = active_usernames[0] if active_usernames else None

    channel = TargetChannel(
        chat_id=int(chat["id"]),
        title=str(chat.get("title", "")),
        supergroup_id=supergroup_id,
        username=username,
    )
    LOG.info(
        "listening to channel chat_id=%s title=%r username=%s supergroup_id=%s",
        channel.chat_id,
        channel.title,
        channel.username,
        channel.supergroup_id,
    )
    return channel


def extract_message_text(content: dict[str, Any]) -> str:
    content_type = content.get("@type")
    if content_type == "messageText":
        formatted = content.get("text", {})
        return str(formatted.get("text", "")).strip()
    if content_type == "messagePhoto":
        caption = content.get("caption", {})
        return str(caption.get("text", "")).strip()
    if content_type == "messageVideo":
        caption = content.get("caption", {})
        return str(caption.get("text", "")).strip()
    if content_type == "messageDocument":
        caption = content.get("caption", {})
        return str(caption.get("text", "")).strip()
    if content_type == "messageAnimation":
        caption = content.get("caption", {})
        return str(caption.get("text", "")).strip()
    if content_type == "messageAudio":
        caption = content.get("caption", {})
        return str(caption.get("text", "")).strip()
    return ""


def should_emit_message(message: dict[str, Any], target: TargetChannel) -> bool:
    if message.get("chat_id") != target.chat_id:
        return False
    return not message.get("is_outgoing")


def emit_message(
    message: dict[str, Any],
    target: TargetChannel,
    dump_raw: bool,
) -> None:
    if dump_raw:
        print(json.dumps(message, ensure_ascii=False, sort_keys=True))
        return

    content = message.get("content", {})
    payload = {
        "channel": target.username or target.title,
        "chat_id": message.get("chat_id"),
        "message_id": message.get("id"),
        "date": message.get("date"),
        "content_type": content.get("@type"),
        "text": extract_message_text(content),
    }
    print(json.dumps(payload, ensure_ascii=False))


def listen_for_channel_messages(
    client: TDJsonClient,
    target: TargetChannel,
    dump_raw: bool,
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    active_stop_event = stop_event or threading.Event()

    if install_signal_handlers:

        def request_stop(signum: int, _frame: Any) -> None:
            if active_stop_event.is_set():
                return
            LOG.info("received signal %s, stopping Telegram listener", signum)
            active_stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)

    while not active_stop_event.is_set():
        event = client.receive(1.0)
        if event is None:
            continue

        event_type = event.get("@type")
        if event_type == "updateAuthorizationState":
            state = event.get("authorization_state", {}).get("@type")
            if state != "authorizationStateReady":
                raise SystemExit(
                    f"TDLib authorization state changed while listening: {state}"
                )
            continue

        if event_type != "updateNewMessage":
            continue

        message = event.get("message")
        if not isinstance(message, dict):
            continue

        if should_emit_message(message, target):
            emit_message(message, target, dump_raw)
            sys.stdout.flush()


def run(
    *,
    stop_event: threading.Event | None = None,
    install_signal_handlers: bool = True,
) -> int:
    config = resolve_config()
    if not config.enabled:
        LOG.info("Telegram / TDLib listener disabled via TG_LISTENER_ENABLED=false")
        return 0

    state_dir = Path(config.state_dir).expanduser().resolve()
    (state_dir / "db").mkdir(parents=True, exist_ok=True)
    (state_dir / "files").mkdir(parents=True, exist_ok=True)

    client = TDJsonClient(config.tdjson_lib)
    authorize(client, config, state_dir)
    target = resolve_target_channel(client, config)
    listen_for_channel_messages(
        client,
        target,
        config.dump_raw,
        stop_event=stop_event,
        install_signal_handlers=install_signal_handlers,
    )
    return 0
