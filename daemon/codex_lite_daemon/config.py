from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    wsl_distro_name: str
    app_data_dir: Path
    database_path: Path
    run_log_dir: Path
    codex_home: Path
    codex_sqlite_home: Path
    codex_path: str
    max_concurrent_runs: int
    allow_mnt_c_projects: bool
    runner_mode: str
    permission_profile: str
    approval_policy: str
    model: str
    auto_compact_token_limit: int
    auto_compact_token_limit_scope: str


def default_config() -> Config:
    home = Path.home()
    app_data_dir = home / ".local" / "share" / "codex-lite"
    codex_home = home / ".codex"
    return Config(
        host="127.0.0.1",
        port=0,
        wsl_distro_name=os.environ.get("WSL_DISTRO_NAME", ""),
        app_data_dir=app_data_dir,
        database_path=app_data_dir / "codex-lite.db",
        run_log_dir=app_data_dir / "runs",
        codex_home=codex_home,
        codex_sqlite_home=codex_home / "sqlite",
        codex_path="",
        max_concurrent_runs=4,
        allow_mnt_c_projects=True,
        runner_mode=os.environ.get("CODEX_LITE_RUNNER", "app-server"),
        permission_profile=os.environ.get("CODEX_LITE_PERMISSION_PROFILE", ":danger-full-access"),
        approval_policy=os.environ.get("CODEX_LITE_APPROVAL_POLICY", "never"),
        model=os.environ.get("CODEX_LITE_MODEL", ""),
        auto_compact_token_limit=int(os.environ.get("CODEX_LITE_AUTO_COMPACT_TOKEN_LIMIT", "100000")),
        auto_compact_token_limit_scope=os.environ.get("CODEX_LITE_AUTO_COMPACT_TOKEN_LIMIT_SCOPE", "total"),
    )


def load_config(path: Path | None = None) -> Config:
    base = default_config()
    config_path = path or Path(os.environ.get("CODEX_LITE_CONFIG", Path.home() / ".config" / "codex-lite" / "config.toml"))
    if not config_path.exists():
        return _with_env_overrides(base)

    with config_path.open("rb") as fh:
        data = tomllib.load(fh)

    values = {
        "host": data.get("host", base.host),
        "port": int(data.get("port", base.port)),
        "wsl_distro_name": data.get("wsl_distro_name", base.wsl_distro_name),
        "app_data_dir": Path(data.get("app_data_dir", str(base.app_data_dir))),
        "database_path": Path(data.get("database_path", str(base.database_path))),
        "run_log_dir": Path(data.get("run_log_dir", str(base.run_log_dir))),
        "codex_home": Path(data.get("codex_home", str(base.codex_home))),
        "codex_sqlite_home": Path(data.get("codex_sqlite_home", str(base.codex_sqlite_home))),
        "codex_path": data.get("codex_path", base.codex_path),
        "max_concurrent_runs": int(data.get("max_concurrent_runs", base.max_concurrent_runs)),
        "allow_mnt_c_projects": bool(data.get("allow_mnt_c_projects", base.allow_mnt_c_projects)),
        "runner_mode": data.get("runner_mode", base.runner_mode),
        "permission_profile": data.get("permission_profile", base.permission_profile),
        "approval_policy": data.get("approval_policy", base.approval_policy),
        "model": data.get("model", base.model),
        "auto_compact_token_limit": int(data.get("auto_compact_token_limit", base.auto_compact_token_limit)),
        "auto_compact_token_limit_scope": data.get("auto_compact_token_limit_scope", base.auto_compact_token_limit_scope),
    }
    return _with_env_overrides(Config(**values))


def _with_env_overrides(config: Config) -> Config:
    runner_mode = os.environ.get("CODEX_LITE_RUNNER", config.runner_mode)
    permission_profile = os.environ.get("CODEX_LITE_PERMISSION_PROFILE", config.permission_profile)
    approval_policy = os.environ.get("CODEX_LITE_APPROVAL_POLICY", config.approval_policy)
    model = os.environ.get("CODEX_LITE_MODEL", config.model)
    auto_compact_token_limit = int(os.environ.get("CODEX_LITE_AUTO_COMPACT_TOKEN_LIMIT", config.auto_compact_token_limit))
    auto_compact_token_limit_scope = os.environ.get("CODEX_LITE_AUTO_COMPACT_TOKEN_LIMIT_SCOPE", config.auto_compact_token_limit_scope)
    database_path = Path(os.environ.get("CODEX_LITE_DATABASE", str(config.database_path)))
    app_data_dir = Path(os.environ.get("CODEX_LITE_APP_DATA_DIR", str(config.app_data_dir)))
    run_log_dir = Path(os.environ.get("CODEX_LITE_RUN_LOG_DIR", str(config.run_log_dir)))
    return Config(
        host=os.environ.get("CODEX_LITE_HOST", config.host),
        port=int(os.environ.get("CODEX_LITE_PORT", config.port)),
        wsl_distro_name=os.environ.get("CODEX_LITE_WSL_DISTRO", config.wsl_distro_name),
        app_data_dir=app_data_dir,
        database_path=database_path,
        run_log_dir=run_log_dir,
        codex_home=Path(os.environ.get("CODEX_LITE_CODEX_HOME", str(config.codex_home))),
        codex_sqlite_home=Path(os.environ.get("CODEX_LITE_CODEX_SQLITE_HOME", str(config.codex_sqlite_home))),
        codex_path=os.environ.get("CODEX_LITE_CODEX_PATH", config.codex_path),
        max_concurrent_runs=int(os.environ.get("CODEX_LITE_MAX_CONCURRENT_RUNS", config.max_concurrent_runs)),
        allow_mnt_c_projects=os.environ.get("CODEX_LITE_ALLOW_MNT_C_PROJECTS", str(config.allow_mnt_c_projects)).lower() in {"1", "true", "yes"},
        runner_mode=runner_mode,
        permission_profile=permission_profile,
        approval_policy=approval_policy,
        model=model,
        auto_compact_token_limit=auto_compact_token_limit,
        auto_compact_token_limit_scope=auto_compact_token_limit_scope,
    )
