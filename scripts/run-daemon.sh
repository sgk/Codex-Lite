#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_ROOT="${CODEX_LITE_DAEMON_DIR:-$ROOT/daemon}"
VENV="${CODEX_LITE_DAEMON_VENV:-$HOME/.local/share/codex-lite/daemon-venv}"
PYTHON="$VENV/bin/python"
STAMP="$VENV/.codex-lite-daemon-pyproject.sha256"

if [ ! -f "$DAEMON_ROOT/pyproject.toml" ]; then
  echo "Codex Lite daemon pyproject.toml was not found: $DAEMON_ROOT" >&2
  exit 1
fi

if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$VENV"
fi

current_stamp="$(
  {
    sha256sum "$DAEMON_ROOT/pyproject.toml"
    find "$DAEMON_ROOT/codex_lite_daemon" -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum
  } | sha256sum | awk '{print $1}'
)"
installed_stamp=""
if [ -f "$STAMP" ]; then
  installed_stamp="$(cat "$STAMP")"
fi

if [ "$current_stamp" != "$installed_stamp" ]; then
  echo "codex-lite-daemon-setup:start" >&2
  "$PYTHON" -m pip install --upgrade pip >&2
  "$PYTHON" -m pip install "$DAEMON_ROOT" >&2
  printf '%s' "$current_stamp" > "$STAMP"
  echo "codex-lite-daemon-setup:finish" >&2
fi

cd "$DAEMON_ROOT"
exec "$PYTHON" -m codex_lite_daemon.main
