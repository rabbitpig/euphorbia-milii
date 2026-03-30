#!/usr/bin/env python3
"""Lightweight helpers for loading configuration from a local .env file."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "on"}


def _load_dotenv(path: str | Path = ".env", *, overwrite: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = parse_env_value(raw_value.strip())
        if not overwrite and key in os.environ:
            continue
        os.environ[key] = value


def parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def get_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        normalized = value.strip()
        if normalized == "":
            continue
        return normalized
    return default


def get_env_int(*names: str, default: int | None = None) -> int | None:
    value = get_env(*names)
    if value is None:
        return default
    return int(value)


def get_env_bool(*names: str, default: bool = False) -> bool:
    value = get_env(*names)
    if value is None:
        return default
    return value.lower() in TRUE_VALUES


def get_env_list(*names: str, default: Sequence[str] | None = None) -> list[str]:
    value = get_env(*names)
    if value is None:
        return list(default) if default is not None else []
    return [item.strip() for item in value.split(",") if item.strip()]
