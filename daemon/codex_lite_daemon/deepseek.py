from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_API_KEY_FILE = Path(".config") / "codex-lite" / "deepseek.env"

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
