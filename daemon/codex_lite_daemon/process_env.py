from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import Config
from .deepseek import DEEPSEEK_API_KEY_ENV, read_deepseek_api_key


DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/lib/wsl/lib"

_LOGIN_ENV: dict[str, str] | None = None

_SAFE_LOGIN_ENV_NAMES = {
    "COLORTERM",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "GPG_AGENT_INFO",
    "GPG_TTY",
    "LANG",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_MESSAGES",
    "NVM_DIR",
    "PATH",
    "PNPM_HOME",
    "SSH_AGENT_PID",
    "SSH_AUTH_SOCK",
    "TERM",
    "WAYLAND_DISPLAY",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "XDG_RUNTIME_DIR",
}


def codex_process_env(config: Config) -> dict[str, str]:
    env = _login_shell_env()
    deepseek_api_key = read_deepseek_api_key()
    env.update(
        {
            "HOME": str(Path.home()),
            "USER": os.environ.get("USER", Path.home().name),
            "SHELL": "/bin/bash",
            "CODEX_HOME": str(config.codex_home),
            "CODEX_SQLITE_HOME": str(config.codex_sqlite_home),
            "PATH": env.get("PATH") or DEFAULT_PATH,
        }
    )
    if deepseek_api_key:
        env[DEEPSEEK_API_KEY_ENV] = deepseek_api_key
    return env


def _login_shell_env() -> dict[str, str]:
    global _LOGIN_ENV
    if _LOGIN_ENV is not None:
        return dict(_LOGIN_ENV)
    _LOGIN_ENV = _read_login_shell_env()
    return dict(_LOGIN_ENV)


def _read_login_shell_env() -> dict[str, str]:
    command = "python3 -c 'import os,json; print(\"__CODEX_LITE_ENV__\" + json.dumps(dict(os.environ)))'"
    return _try_read_shell_env(["bash", "-lic", command]) or {"PATH": DEFAULT_PATH}


def _try_read_shell_env(bash_args: list[str]) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            bash_args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    marker = "__CODEX_LITE_ENV__"
    payload = ""
    for line in result.stdout.splitlines():
        if marker in line:
            payload = line.split(marker, 1)[1]
    try:
        raw_env = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_env, dict):
        return None
    safe_env: dict[str, str] = {}
    for key, value in raw_env.items():
        if key not in _SAFE_LOGIN_ENV_NAMES or not isinstance(value, str) or "\x00" in value:
            continue
        safe_env[key] = _sanitize_path(value) if key == "PATH" else value
    safe_env.setdefault("PATH", DEFAULT_PATH)
    return safe_env


def _sanitize_path(value: str) -> str:
    entries = [
        entry
        for entry in value.split(":")
        if entry and not _looks_like_windows_mount_path(entry)
    ]
    return ":".join(entries) or DEFAULT_PATH


def _looks_like_windows_mount_path(value: str) -> bool:
    return len(value) > 7 and value.startswith("/mnt/") and value[5].isalpha() and value[6] == "/"
