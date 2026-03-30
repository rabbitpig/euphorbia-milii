.DEFAULT_GOAL := help

SHELL := /bin/sh

UV ?= uv
ENV_FILE ?= .env
PYTHON_FILES := $(sort $(shell find src/imsg_codex -type f -name '*.py'))

.PHONY: help run check clean-pycache

help:
	@printf '%s\n' \
		'Targets:' \
		'  make run            Load .env, disable bytecode writes, and start the listeners via uv' \
		'  make check          Run an in-memory Python syntax check via uv' \
		'  make clean-pycache  Remove __pycache__ directories and .pyc/.pyo files'

run:
	@set -eu; \
	if [ -f "$(ENV_FILE)" ]; then \
		set -a; \
		. "$(ENV_FILE)"; \
		set +a; \
	fi; \
	export PYTHONDONTWRITEBYTECODE=1; \
	exec $(UV) run python -m imsg_codex

check:
	@exec $(UV) run python -c "from pathlib import Path; import sys; [compile(Path(path).read_text(encoding='utf-8'), path, 'exec') for path in sys.argv[1:]]" $(PYTHON_FILES)

clean-pycache:
	@find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
