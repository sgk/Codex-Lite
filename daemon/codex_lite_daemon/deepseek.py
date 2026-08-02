from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_API_KEY_FILE = Path(".config") / "codex-lite" / "deepseek.env"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
DEEPSEEK_BALANCE_TIMEOUT_SECONDS = 5

# Codex requires a complete model metadata entry when a custom catalog is used.
# Keep the catalog limited to the currently supported Codex model and leave the
# provider's base instructions empty; Codex still supplies the normal workspace
# instructions and tool protocol around the model.
_MODEL_CATALOG = {
    "models": [
        {
            "slug": DEEPSEEK_MODEL,
            "prefer_websockets": False,
            "support_verbosity": True,
            "default_verbosity": "low",
            "apply_patch_tool_type": "freeform",
            "web_search_tool_type": "text",
            "input_modalities": ["text"],
            "supports_image_detail_original": False,
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "supports_parallel_tool_calls": True,
            "tool_mode": None,
            "multi_agent_version": "v2",
            "use_responses_lite": False,
            "include_skills_usage_instructions": False,
            "auto_review_model_override": None,
            "context_window": 1048576,
            "max_context_window": 1048576,
            "effective_context_window_percent": 95,
            "auto_compact_token_limit": None,
            "comp_hash": "3000",
            "reasoning_summary_format": "experimental",
            "default_reasoning_summary": "none",
            "display_name": "DeepSeek-V4-Flash",
            "description": "DeepSeek V4 Flash",
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Fast responses with lighter reasoning"},
                {"effort": "high", "description": "Extra high reasoning depth for complex problems"},
                {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
            ],
            "shell_type": "shell_command",
            "visibility": "list",
            "minimal_client_version": "0.144.0",
            "supported_in_api": True,
            "availability_nux": None,
            "upgrade": None,
            "priority": 1,
            "experimental_supported_tools": [],
            "supports_search_tool": True,
            "default_service_tier": None,
            "supports_reasoning_summaries": True,
            "base_instructions": "",
        }
    ]
}


def model_provider_for_model(model: str) -> str:
    return DEEPSEEK_PROVIDER if model.strip().lower().startswith("deepseek-") else "openai"


def deepseek_model_ids() -> list[str]:
    return [DEEPSEEK_MODEL] if deepseek_api_key_configured() else []


def deepseek_reasoning_efforts() -> list[str]:
    return ["low", "high", "max"]


def deepseek_api_key_configured() -> bool:
    value = os.environ.get(DEEPSEEK_API_KEY_ENV)
    if value and value.startswith("sk-") and not any(char.isspace() for char in value):
        return True
    return bool(_read_api_key_file())


def read_deepseek_api_key() -> str | None:
    value = os.environ.get(DEEPSEEK_API_KEY_ENV)
    if value and value.startswith("sk-") and not any(char.isspace() for char in value):
        return value
    return _read_api_key_file()


async def read_deepseek_balance() -> dict | None:
    """Read the provider's balance without exposing the API key to callers."""
    api_key = read_deepseek_api_key()
    if not api_key:
        return None
    return await asyncio.to_thread(_fetch_deepseek_balance, api_key)


def _fetch_deepseek_balance(api_key: str) -> dict:
    request = Request(
        DEEPSEEK_BALANCE_URL,
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=DEEPSEEK_BALANCE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "isAvailable": False, "balanceInfos": []}
    return _normalize_deepseek_balance(payload)


def _normalize_deepseek_balance(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"status": "unavailable", "isAvailable": False, "balanceInfos": []}
    infos: list[dict[str, str]] = []
    raw_infos = payload.get("balance_infos")
    if isinstance(raw_infos, list):
        for item in raw_infos:
            if not isinstance(item, dict):
                continue
            currency = item.get("currency")
            total = _balance_value(item.get("total_balance"))
            if not isinstance(currency, str) or not total:
                continue
            infos.append(
                {
                    "currency": currency,
                    "totalBalance": total,
                    "grantedBalance": _balance_value(item.get("granted_balance")),
                    "toppedUpBalance": _balance_value(item.get("topped_up_balance")),
                }
            )
    return {
        "status": "ok",
        "isAvailable": payload.get("is_available") is True,
        "balanceInfos": infos,
    }


def _balance_value(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return str(value)


def ensure_model_catalog(app_data_dir: Path) -> Path:
    app_data_dir.mkdir(parents=True, exist_ok=True)
    path = app_data_dir / "deepseek-models.json"
    payload = json.dumps(_MODEL_CATALOG, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        if path.read_text(encoding="utf-8") == payload:
            return path
    except OSError:
        pass
    fd, temporary_path = tempfile.mkstemp(prefix="deepseek-models-", suffix=".json", dir=app_data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
    return path


def _read_api_key_file() -> str | None:
    path = Path.home() / DEEPSEEK_API_KEY_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith(f"{DEEPSEEK_API_KEY_ENV}="):
            continue
        value = line.partition("=")[2].strip()
        if value.startswith("sk-") and not any(char.isspace() for char in value):
            return value
    return None
