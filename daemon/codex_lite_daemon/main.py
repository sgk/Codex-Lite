from __future__ import annotations

import asyncio
import json
import platform
import socket
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .automation_service import AutomationService, run_automation_scheduler
from .app_server import AppServerClient
from .app_services import AppServerRunService, AppServerRuntimeSettings, AppServerThreadService, AppServerUsageService
from .config import Config, load_config
from .codex_state import CodexStateService
from .db import Database
from .errors import AppError
from .file_service import FileService
from .models import AutomationCreate, AutomationUpdate, ChatCreate, ChatUpdate, MessageCreate, ProjectCandidateImport, ProjectCreate, ProjectUpdate, RunSteer
from .process_env import codex_process_env
from .run_service import RunService
from .runner.codex_runner import CodexRunner
from .runner.fake_runner import FakeRunner
from .services import ChatService, MessageService, ProjectService
from .transcript_import import TranscriptImportService


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    cfg.app_data_dir.mkdir(parents=True, exist_ok=True)
    cfg.run_log_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.database_path)
    db.migrate()

    projects = ProjectService(db, cfg)
    chats = ChatService(db, projects)
    messages = MessageService(db, chats)
    codex_runner = CodexRunner(cfg)
    runner = FakeRunner() if cfg.runner_mode == "fake" else codex_runner
    app_server = AppServerClient(cfg, codex_runner)
    codex_state = CodexStateService(cfg)
    runs = RunService(db, cfg, projects, chats, messages, runner)
    files = FileService(db, projects)
    transcript_import = TranscriptImportService(cfg, projects, chats, codex_state)
    app_settings = _load_app_settings(cfg)
    app_threads = AppServerThreadService(projects, chats, messages, transcript_import, app_server, app_settings)
    app_runs = AppServerRunService(projects, app_threads, messages, app_server, cfg.max_concurrent_runs, app_settings)
    app_usage = AppServerUsageService(app_server)
    automations = AutomationService(db, projects, chats)
    runs.recover_stale_runs()
    use_app_server = cfg.runner_mode == "app-server"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler_stop = asyncio.Event()
        scheduler_task: asyncio.Task | None = None
        try:
            scheduler_task = asyncio.create_task(run_automation_scheduler(automations, app_runs if use_app_server else runs, scheduler_stop))
            yield
        finally:
            scheduler_stop.set()
            if scheduler_task is not None:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
            await app_server.close()
            db.close()

    app = FastAPI(title="Codex Lite daemon", version=__version__, lifespan=lifespan)
    app.state.config = cfg
    app.state.db = db
    app.state.codex_runner = codex_runner
    app.state.runs = runs
    app.state.app_server = app_server

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Request validation failed.", "details": {"errors": exc.errors()}}},
        )

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "databasePath": str(cfg.database_path),
            "codexPath": codex_runner.resolved_path_sync(),
            "codexVersion": codex_runner.codex_version,
            "codexHome": str(cfg.codex_home),
        }

    @app.get("/diagnostics")
    async def diagnostics() -> dict:
        codex_path, codex_version = await resolve_codex_info(codex_runner)
        active_runs = app_runs.list_run_diagnostics() if use_app_server else runs.list_run_diagnostics()
        process_env = codex_process_env(cfg)
        return {
            "daemonPid": __import__("os").getpid(),
            "pythonVersion": sys.version,
            "platform": platform.platform(),
            "databasePath": str(cfg.database_path),
            "databaseFileExists": cfg.database_path.exists(),
            "codexPath": codex_path,
            "codexVersion": codex_version,
            "codexHome": str(cfg.codex_home),
            "codexHomeExists": cfg.codex_home.exists(),
            "codexSqliteHome": str(cfg.codex_sqlite_home),
            "codexSqliteHomeExists": cfg.codex_sqlite_home.exists(),
            "activeRunIds": [run["id"] for run in active_runs],
            "activeRuns": active_runs,
            "effectivePath": process_env.get("PATH", ""),
            "sshAgentConfigured": bool(process_env.get("SSH_AUTH_SOCK")),
            "appDataDir": str(cfg.app_data_dir),
            "runLogDir": str(cfg.run_log_dir),
            "runnerMode": cfg.runner_mode,
            "permissionProfile": app_settings.permission_profile,
            "approvalPolicy": app_settings.approval_policy,
            "model": app_settings.model,
            "appServerRunning": app_server.is_running,
            "appServerEnvironment": app_server.environment_diagnostics(),
            "appServerStderrTail": app_server.stderr_tail,
            "codexStateSync": codex_state.diagnostics(),
        }

    @app.get("/settings")
    async def get_settings() -> dict:
        return {
            "permissionProfile": app_settings.permission_profile,
            "approvalPolicy": app_settings.approval_policy,
            "model": app_settings.model,
            "availablePermissionProfiles": [":read-only", ":workspace", ":danger-full-access"],
            "availableApprovalPolicies": ["untrusted", "on-failure", "on-request", "never"],
            "availableModels": ["", "gpt-5", "gpt-5-codex"],
        }

    @app.get("/usage/capacity")
    async def usage_capacity() -> dict:
        if not use_app_server:
            raise AppError("usage_unavailable", "Codex usage capacity requires app-server mode.", 503)
        return await app_usage.read_capacity()

    @app.patch("/settings")
    async def update_settings(body: dict) -> dict:
        if "permissionProfile" in body:
            app_settings.permission_profile = _normalized_permission_profile(str(body.get("permissionProfile") or ""))
        if "approvalPolicy" in body:
            app_settings.approval_policy = _normalized_approval_policy(str(body.get("approvalPolicy") or ""))
        if "model" in body:
            app_settings.model = _normalized_model(str(body.get("model") or ""))
        _save_app_settings(cfg, app_settings)
        return {
            "permissionProfile": app_settings.permission_profile,
            "approvalPolicy": app_settings.approval_policy,
            "model": app_settings.model,
            "availablePermissionProfiles": [":read-only", ":workspace", ":danger-full-access"],
            "availableApprovalPolicies": ["untrusted", "on-failure", "on-request", "never"],
            "availableModels": ["", "gpt-5", "gpt-5-codex"],
        }

    @app.post("/shutdown")
    async def shutdown() -> dict:
        server = getattr(app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return {"ok": True}

    @app.get("/projects")
    async def list_projects() -> list[dict]:
        return projects.list_projects()

    @app.post("/projects")
    async def create_project(body: ProjectCreate) -> dict:
        project = projects.create_project(body.path, body.name)
        if use_app_server:
            transcript_import.index_project(project)
        return project

    @app.get("/projects/{project_id}")
    async def get_project(project_id: str) -> dict:
        return projects.get_project(project_id)

    @app.patch("/projects/{project_id}")
    async def update_project(project_id: str, body: ProjectUpdate) -> dict:
        return projects.update_project(project_id, body.name)

    @app.delete("/projects/{project_id}", status_code=204)
    async def delete_project(project_id: str) -> None:
        projects.delete_project(project_id)

    @app.get("/project-candidates")
    async def list_project_candidates() -> list[dict]:
        return transcript_import.list_project_candidates()

    @app.post("/project-candidates/import")
    async def import_project_candidates(body: ProjectCandidateImport | None = None) -> list[dict]:
        return transcript_import.import_project_candidates(body.paths if body else None)

    @app.get("/projects/{project_id}/chats")
    async def list_chats(project_id: str) -> list[dict]:
        if use_app_server:
            return await app_threads.list_chats(project_id)
        return chats.list_chats(project_id)

    @app.post("/projects/{project_id}/chats")
    async def create_chat(project_id: str, body: ChatCreate) -> dict:
        if use_app_server:
            return await app_threads.create_chat(project_id, body.title)
        return chats.create_chat(project_id, body.title)

    @app.get("/projects/{project_id}/chats/{chat_id}")
    async def get_chat(project_id: str, chat_id: str) -> dict:
        if use_app_server:
            return await app_threads.get_chat(project_id, chat_id)
        return chats.get_chat(project_id, chat_id)

    @app.patch("/projects/{project_id}/chats/{chat_id}")
    async def update_chat(project_id: str, chat_id: str, body: ChatUpdate) -> dict:
        if use_app_server:
            return await app_threads.update_chat(project_id, chat_id, body.title)
        return chats.update_chat(project_id, chat_id, body.title)

    @app.post("/projects/{project_id}/chats/{chat_id}/archive")
    async def archive_chat(project_id: str, chat_id: str) -> dict:
        if use_app_server:
            return await app_threads.archive_chat(project_id, chat_id)
        return chats.archive_chat(project_id, chat_id)

    @app.get("/projects/{project_id}/chats/{chat_id}/automations")
    async def list_automations(project_id: str, chat_id: str) -> list[dict]:
        return automations.list_automations(project_id, chat_id)

    @app.post("/projects/{project_id}/chats/{chat_id}/automations")
    async def create_automation(project_id: str, chat_id: str, body: AutomationCreate) -> dict:
        return automations.create_automation(project_id, chat_id, body.name, body.prompt, body.interval_minutes, body.enabled)

    @app.patch("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}")
    async def update_automation(project_id: str, chat_id: str, automation_id: str, body: AutomationUpdate) -> dict:
        return automations.update_automation(project_id, chat_id, automation_id, body.name, body.prompt, body.interval_minutes, body.enabled)

    @app.post("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}/run")
    async def run_automation_now(project_id: str, chat_id: str, automation_id: str) -> dict:
        return await automations.run_now(project_id, chat_id, automation_id, app_runs if use_app_server else runs)

    @app.delete("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}", status_code=204)
    async def delete_automation(project_id: str, chat_id: str, automation_id: str) -> None:
        automations.delete_automation(project_id, chat_id, automation_id)

    @app.delete("/projects/{project_id}/chats/{chat_id}", status_code=204)
    async def delete_chat(project_id: str, chat_id: str) -> None:
        if use_app_server:
            await app_threads.delete_chat(project_id, chat_id)
            return
        chats.delete_chat(project_id, chat_id)

    @app.get("/projects/{project_id}/chats/{chat_id}/messages")
    async def list_messages(project_id: str, chat_id: str) -> list[dict]:
        if use_app_server:
            return await app_threads.list_messages(project_id, chat_id)
        return messages.list_messages(project_id, chat_id)

    @app.get("/projects/{project_id}/chats/{chat_id}/messages/page")
    async def list_message_page(project_id: str, chat_id: str, limit: int = 200, before_created_at: str | None = None, before_id: str | None = None) -> dict:
        if use_app_server:
            all_messages = await app_threads.list_messages(project_id, chat_id)
        else:
            all_messages = messages.list_messages(project_id, chat_id)
        return message_page(all_messages, limit, before_created_at, before_id)

    @app.post("/projects/{project_id}/chats/{chat_id}/messages")
    async def create_message(project_id: str, chat_id: str, body: MessageCreate) -> dict:
        if use_app_server:
            return await app_runs.start_message_run(project_id, chat_id, body.content, [item.model_dump() for item in body.attachments])
        return runs.start_message_run(project_id, chat_id, body.content)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        if use_app_server:
            return app_runs.get_run(run_id)
        return runs.get_run(run_id)

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        stream = app_runs.stream_events(run_id) if use_app_server else runs.stream_events(run_id)
        return StreamingResponse(stream, media_type="text/event-stream")

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        if use_app_server:
            return await app_runs.cancel_run(run_id)
        return await runs.cancel_run(run_id)

    @app.post("/runs/{run_id}/steer")
    async def steer_run(run_id: str, body: RunSteer) -> dict:
        if not use_app_server:
            raise AppError("steer_not_supported", "Steering is only supported with Codex app-server.", 409)
        return await app_runs.steer_run(run_id, body.content, [item.model_dump() for item in body.attachments])

    @app.get("/projects/{project_id}/files")
    async def list_files(project_id: str, path: str = "") -> dict:
        return files.list_files(project_id, path)

    @app.get("/projects/{project_id}/files/content")
    async def read_file(project_id: str, path: str) -> dict:
        return files.read_content(project_id, path)

    return app


async def resolve_codex_info(codex_runner: CodexRunner) -> tuple[str | None, str | None]:
    try:
        path = await codex_runner.resolve()
        return path, codex_runner.codex_version
    except AppError:
        return codex_runner.resolved_path_sync(), codex_runner.codex_version


def _normalized_permission_profile(value: str) -> str:
    aliases = {
        "read-only": ":read-only",
        "workspace": ":workspace",
        "workspace-write": ":workspace",
        "danger-full-access": ":danger-full-access",
        "full-access": ":danger-full-access",
    }
    normalized = aliases.get(value, value)
    if normalized not in {":read-only", ":workspace", ":danger-full-access"}:
        raise AppError("validation_error", "Permission profile is invalid.", 400)
    return normalized


def _normalized_approval_policy(value: str) -> str:
    aliases = {
        "ask": "on-request",
        "on_request": "on-request",
        "on-failure": "on-failure",
        "on_failure": "on-failure",
        "never": "never",
        "untrusted": "untrusted",
    }
    normalized = aliases.get(value, value)
    if normalized not in {"untrusted", "on-failure", "on-request", "never"}:
        raise AppError("validation_error", "Approval policy is invalid.", 400)
    return normalized


def _normalized_model(value: str) -> str:
    normalized = value.strip()
    if "\x00" in normalized or len(normalized) > 120:
        raise AppError("validation_error", "Model is invalid.", 400)
    return normalized


def _settings_path(config: Config) -> Path:
    return config.app_data_dir / "settings.json"


def _load_app_settings(config: Config) -> AppServerRuntimeSettings:
    permission_profile = config.permission_profile
    approval_policy = config.approval_policy
    model = config.model
    path = _settings_path(config)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            permission_value = data.get("permissionProfile")
            approval_value = data.get("approvalPolicy")
            model_value = data.get("model")
            if isinstance(permission_value, str):
                permission_profile = permission_value
            if isinstance(approval_value, str):
                approval_policy = approval_value
            if isinstance(model_value, str):
                model = model_value
    except (OSError, json.JSONDecodeError, AppError):
        pass
    return AppServerRuntimeSettings(
        permission_profile=_normalized_permission_profile(permission_profile),
        approval_policy=_normalized_approval_policy(approval_policy),
        model=_normalized_model(model),
    )


def _save_app_settings(config: Config, settings: AppServerRuntimeSettings) -> None:
    path = _settings_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "permissionProfile": settings.permission_profile,
                "approvalPolicy": settings.approval_policy,
                "model": settings.model,
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")


def message_page(messages: list[dict], limit: int, before_created_at: str | None, before_id: str | None) -> dict:
    ordered = sorted(messages, key=message_sort_key)
    total_count = len(ordered)
    bounded_limit = max(1, min(limit, 500))
    if before_created_at:
        cursor = (_message_timestamp(before_created_at), before_id or "")
        ordered = [message for message in ordered if message_sort_key(message) < cursor]
    page_messages = ordered[-bounded_limit:]
    return {
        "messages": page_messages,
        "totalCount": total_count,
        "hasMoreBefore": len(ordered) > len(page_messages),
    }


def message_sort_key(message: dict) -> tuple[datetime, str]:
    return (_message_timestamp(str(message.get("createdAt") or "")), str(message.get("id") or ""))


def _message_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def main() -> None:
    cfg = load_config()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((cfg.host, cfg.port))
    sock.listen(2048)
    sock.set_inheritable(True)
    host, port = sock.getsockname()[:2]

    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, reload=False))
    app.state.server = server
    threading.Thread(target=_request_shutdown_on_stdin_eof, args=(server,), daemon=True).start()
    print(json.dumps({"event": "ready", "host": host, "port": port}), flush=True)
    server.run(sockets=[sock])


def _request_shutdown_on_stdin_eof(server: uvicorn.Server) -> None:
    for _ in sys.stdin.buffer:
        pass
    server.should_exit = True


if __name__ == "__main__":
    main()
