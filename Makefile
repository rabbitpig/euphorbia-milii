.DEFAULT_GOAL := help

SHELL := /bin/sh

UV ?= uv
ENV_FILE ?= .env
PYTHON_FILES := $(sort $(shell find src/imsg_codex -type f -name '*.py'))

.PHONY: help run check clean-pycache install-dependencies

help:
	@printf '%s\n' \
		'Targets:' \
		'  make run            Load .env, disable bytecode writes, and start the listeners via uv' \
		'  make check          Run an in-memory Python syntax check via uv' \
		'  make clean-pycache  Remove __pycache__ directories and .pyc/.pyo files' \
		'  make install-dependencies  Install Homebrew packages and build TDLib into runtime/telegram/tdlib'

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

install-dependencies:
	@set -eu; \
	ARCH="$$(uname -m)"; \
	OS_NAME="$$(uname -s)"; \
	if [ "$$OS_NAME" != "Darwin" ]; then \
		printf '%s\n' 'install-dependencies only supports macOS.' >&2; \
		exit 1; \
	fi; \
	if [ "$$ARCH" != "arm64" ]; then \
		printf '%s\n' "install-dependencies requires Apple Silicon (arm64), got $$ARCH." >&2; \
		exit 1; \
	fi; \
	if ! command -v brew >/dev/null 2>&1; then \
		printf '%s\n' 'Homebrew not found. Installing Homebrew...'; \
		/bin/bash -c "$$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; \
	fi; \
	if ! xcode-select -p >/dev/null 2>&1; then \
		printf '%s\n' 'Xcode Command Line Tools not found. Running xcode-select --install...'; \
		xcode-select --install || true; \
		printf '%s\n' 'Please finish the Xcode Command Line Tools installation if prompted, then rerun make install-dependencies.'; \
	fi; \
	printf '%s\n' 'Installing Homebrew packages: python uv imsg gperf cmake openssl'; \
	brew install python uv imsg gperf cmake openssl; \
	ROOT_DIR="$$(pwd)"; \
	RUNTIME_DIR="$$ROOT_DIR/runtime/telegram"; \
	TD_SRC_DIR="$$RUNTIME_DIR/td"; \
	TD_BUILD_DIR="$$TD_SRC_DIR/build"; \
	TD_INSTALL_DIR="$$RUNTIME_DIR/tdlib"; \
	OPENSSL_ROOT="/opt/homebrew/opt/openssl"; \
	mkdir -p "$$RUNTIME_DIR"; \
	if [ ! -d "$$TD_SRC_DIR/.git" ]; then \
		printf '%s\n' 'Cloning TDLib source...'; \
		rm -rf "$$TD_SRC_DIR"; \
		git clone https://github.com/tdlib/td.git "$$TD_SRC_DIR"; \
	else \
		printf '%s\n' 'Reusing existing TDLib source checkout.'; \
	fi; \
	rm -rf "$$TD_BUILD_DIR"; \
	mkdir -p "$$TD_BUILD_DIR"; \
	printf '%s\n' "Building TDLib into $$TD_INSTALL_DIR"; \
	cd "$$TD_BUILD_DIR"; \
	cmake \
		-DCMAKE_BUILD_TYPE=Release \
		-DOPENSSL_ROOT_DIR="$$OPENSSL_ROOT" \
		-DCMAKE_INSTALL_PREFIX:PATH="$$TD_INSTALL_DIR" \
		-DTD_ENABLE_LTO=ON \
		..; \
	cmake --build . --target install; \
	printf '%s\n' 'TDLib install contents:'; \
	ls -l "$$TD_INSTALL_DIR"
