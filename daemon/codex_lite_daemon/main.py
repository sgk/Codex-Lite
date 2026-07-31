from __future__ import annotations

import asyncio
import inspect
import json
import platform
import socket
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import __version__
from .automation_service import AutomationService, run_automation_scheduler
from .app_server import AppServerClient
from .app_services import AppServerRunService, AppServerRuntimeSettings, AppServerThreadService, AppServerUsageService
from .config import Config, load_config
from .codex_state import CodexStateService
from .db import Database
from .errors import AppError
from .file_service import FileService
from .process_env import codex_process_env
from .run_service import RunService
from .runner.codex_runner import CodexRunner
from .runner.fake_runner import FakeRunner
from .services import ChatService, MessageService, ProjectService
from .transcript_import import TranscriptImportService


def create_app(config: Config | None = None) -> Starlette:
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
    async def lifespan(_: Starlette):
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

    app = Starlette(lifespan=lifespan)
    app.state.config = cfg
    app.state.db = db
    app.state.codex_runner = codex_runner
    app.state.runs = runs
    app.state.app_server = app_server

    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    app.add_exception_handler(AppError, app_error_handler)

    get, post, patch, delete = _route_helpers(app)

    @get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "databasePath": str(cfg.database_path),
            "codexPath": codex_runner.resolved_path_sync(),
            "codexVersion": codex_runner.codex_version,
            "codexHome": str(cfg.codex_home),
        }

    @get("/diagnostics")
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
            "reasoningEffort": app_settings.reasoning_effort,
            "autoCompactTokenLimit": cfg.auto_compact_token_limit,
            "autoCompactTokenLimitScope": cfg.auto_compact_token_limit_scope,
            "appServerRunning": app_server.is_running,
            "appServerEnvironment": app_server.environment_diagnostics(),
            "appServerStderrTail": app_server.stderr_tail,
            "codexStateSync": codex_state.diagnostics(),
        }

    @get("/settings")
    async def get_settings() -> dict:
        return _settings_out(app_settings, _static_model_options(app_settings.model))

    @get("/models")
    async def list_models() -> dict:
        if not use_app_server:
            return _model_list_out(_static_model_options(app_settings.model), {}, app_settings.model, dynamic=False)
        try:
            response = await app_server.request("model/list", {})
            models, efforts_by_model = _model_catalog_from_response(response)
            if models:
                return _model_list_out(models, efforts_by_model, app_settings.model, dynamic=True)
        except AppError:
            pass
        return _model_list_out(_static_model_options(app_settings.model), {}, app_settings.model, dynamic=False)

    @get("/usage/capacity")
    async def usage_capacity() -> dict:
        if not use_app_server:
            raise AppError("usage_unavailable", "Codex usage capacity requires app-server mode.", 503)
        return await app_usage.read_capacity()

    @patch("/settings")
    async def update_settings(body: dict) -> dict:
        if "permissionProfile" in body:
            app_settings.permission_profile = _normalized_permission_profile(str(body.get("permissionProfile") or ""))
        if "approvalPolicy" in body:
            app_settings.approval_policy = _normalized_approval_policy(str(body.get("approvalPolicy") or ""))
        if "model" in body:
            app_settings.model = _normalized_model(str(body.get("model") or ""))
        if "reasoningEffort" in body:
            app_settings.reasoning_effort = _normalized_reasoning_effort(str(body.get("reasoningEffort") or ""))
        _save_app_settings(cfg, app_settings)
        return _settings_out(app_settings, _static_model_options(app_settings.model))

    @post("/shutdown")
    async def shutdown() -> dict:
        server = getattr(app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return {"ok": True}

    @get("/projects")
    async def list_projects() -> list[dict]:
        return projects.list_projects()

    @post("/projects")
    async def create_project(body: dict) -> dict:
        project = projects.create_project(_required_str(body, "path"), _optional_str(body, "name"))
        if use_app_server:
            transcript_import.index_project(project)
        return project

    @get("/projects/{project_id}")
    async def get_project(project_id: str) -> dict:
        return projects.get_project(project_id)

    @patch("/projects/{project_id}")
    async def update_project(project_id: str, body: dict) -> dict:
        return projects.update_project(project_id, _optional_str(body, "name"))

    @delete("/projects/{project_id}", status_code=204)
    async def delete_project(project_id: str) -> None:
        projects.delete_project(project_id)

    @get("/project-candidates")
    async def list_project_candidates() -> list[dict]:
        return transcript_import.list_project_candidates()

    @post("/project-candidates/import")
    async def import_project_candidates(body: dict | None = None) -> list[dict]:
        return transcript_import.import_project_candidates(_optional_str_list(body, "paths") if body else None)

    @get("/projects/{project_id}/chats")
    async def list_chats(project_id: str, sync: bool = False) -> list[dict]:
        if use_app_server:
            return await app_threads.list_chats(project_id, sync)
        return chats.list_chats(project_id)

    @post("/projects/{project_id}/chats")
    async def create_chat(project_id: str, body: dict) -> dict:
        if use_app_server:
            return await app_threads.create_chat(project_id, _optional_str(body, "title"))
        return chats.create_chat(project_id, _optional_str(body, "title"))

    @get("/projects/{project_id}/chats/{chat_id}")
    async def get_chat(project_id: str, chat_id: str) -> dict:
        if use_app_server:
            return await app_threads.get_chat(project_id, chat_id)
        return chats.get_chat(project_id, chat_id)

    @patch("/projects/{project_id}/chats/{chat_id}")
    async def update_chat(project_id: str, chat_id: str, body: dict) -> dict:
        if use_app_server:
            return await app_threads.update_chat(project_id, chat_id, _optional_str(body, "title"))
        return chats.update_chat(project_id, chat_id, _optional_str(body, "title"))

    @post("/projects/{project_id}/chats/{chat_id}/archive")
    async def archive_chat(project_id: str, chat_id: str) -> dict:
        if use_app_server:
            return await app_threads.archive_chat(project_id, chat_id)
        return chats.archive_chat(project_id, chat_id)

    @get("/projects/{project_id}/chats/{chat_id}/automations")
    async def list_automations(project_id: str, chat_id: str) -> list[dict]:
        return automations.list_automations(project_id, chat_id)

    @post("/projects/{project_id}/chats/{chat_id}/automations")
    async def create_automation(project_id: str, chat_id: str, body: dict) -> dict:
        return automations.create_automation(
            project_id,
            chat_id,
            _required_str(body, "name"),
            _required_str(body, "prompt", min_length=1),
            _required_int(body, "interval_minutes", minimum=0),
            _optional_bool(body, "enabled", True),
            _optional_str(body, "schedule_kind") or "interval_minutes",
        )

    @patch("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}")
    async def update_automation(project_id: str, chat_id: str, automation_id: str, body: dict) -> dict:
        return automations.update_automation(
            project_id,
            chat_id,
            automation_id,
            _optional_str(body, "name"),
            _optional_str(body, "prompt"),
            _optional_int(body, "interval_minutes", minimum=0),
            _optional_bool(body, "enabled", None),
            _optional_str(body, "schedule_kind"),
        )

    @post("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}/run")
    async def run_automation_now(project_id: str, chat_id: str, automation_id: str) -> dict:
        return await automations.run_now(project_id, chat_id, automation_id, app_runs if use_app_server else runs)

    @delete("/projects/{project_id}/chats/{chat_id}/automations/{automation_id}", status_code=204)
    async def delete_automation(project_id: str, chat_id: str, automation_id: str) -> None:
        automations.delete_automation(project_id, chat_id, automation_id)

    @delete("/projects/{project_id}/chats/{chat_id}", status_code=204)
    async def delete_chat(project_id: str, chat_id: str) -> None:
        if use_app_server:
            await app_threads.delete_chat(project_id, chat_id)
            return
        chats.delete_chat(project_id, chat_id)

    @get("/projects/{project_id}/chats/{chat_id}/messages")
    async def list_messages(project_id: str, chat_id: str) -> list[dict]:
        if use_app_server:
            return await app_threads.list_messages(project_id, chat_id)
        return messages.list_messages(project_id, chat_id)

    @get("/projects/{project_id}/chats/{chat_id}/messages/page")
    async def list_message_page(project_id: str, chat_id: str, limit: int = 200, before_created_at: str | None = None, before_id: str | None = None) -> dict:
        if use_app_server:
            all_messages = await app_threads.list_messages(project_id, chat_id)
        else:
            all_messages = messages.list_messages(project_id, chat_id)
        return message_page(all_messages, limit, before_created_at, before_id)

    @post("/projects/{project_id}/chats/{chat_id}/messages")
    async def create_message(project_id: str, chat_id: str, body: dict) -> dict:
        if use_app_server:
            return await app_runs.start_message_run(project_id, chat_id, _required_str(body, "content", min_length=1), _attachments(body))
        return runs.start_message_run(project_id, chat_id, _required_str(body, "content", min_length=1))

    @get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        if use_app_server:
            return app_runs.get_run(run_id)
        return runs.get_run(run_id)

    @get("/runs/{run_id}/events")
    async def run_events(run_id: str) -> StreamingResponse:
        stream = app_runs.stream_events(run_id) if use_app_server else runs.stream_events(run_id)
        return StreamingResponse(stream, media_type="text/event-stream")

    @post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        if use_app_server:
            return await app_runs.cancel_run(run_id)
        return await runs.cancel_run(run_id)

    @post("/runs/{run_id}/steer")
    async def steer_run(run_id: str, body: dict) -> dict:
        if not use_app_server:
            raise AppError("steer_not_supported", "Steering is only supported with Codex app-server.", 409)
        return await app_runs.steer_run(run_id, _required_str(body, "content", min_length=1), _attachments(body))

    @get("/projects/{project_id}/files")
    async def list_files(project_id: str, path: str = "") -> dict:
        return files.list_files(project_id, path)

    @get("/projects/{project_id}/files/content")
    async def read_file(project_id: str, path: str) -> dict:
        return files.read_content(project_id, path)

    return app


def _route_helpers(app: Starlette):
    def route(method: str, path: str, status_code: int = 200):
        def decorator(func):
            signature = inspect.signature(func)

            async def endpoint(request: Request) -> Response:
                kwargs = {}
                for name, parameter in signature.parameters.items():
                    if name == "request":
                        kwargs[name] = request
                    elif name == "body":
                        kwargs[name] = await _json_body(request, required=parameter.default is inspect.Signature.empty)
                    elif name in request.path_params:
                        kwargs[name] = request.path_params[name]
                    else:
                        kwargs[name] = _query_param(request, name, parameter)
                result = await func(**kwargs)
                if isinstance(result, Response):
                    return result
                if status_code == 204:
                    return Response(status_code=204)
                return JSONResponse(result, status_code=status_code)

            app.add_route(path, endpoint, methods=[method])
            return func

        return decorator

    return (
        lambda path, status_code=200: route("GET", path, status_code),
        lambda path, status_code=200: route("POST", path, status_code),
        lambda path, status_code=200: route("PATCH", path, status_code),
        lambda path, status_code=200: route("DELETE", path, status_code),
    )


async def _json_body(request: Request, *, required: bool) -> dict | None:
    try:
        body = await request.body()
        if not body:
            if required:
                raise _validation_error("Request body is required.")
            return None
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise _validation_error("Request body must be valid JSON.", {"error": str(exc)}) from exc
    if not isinstance(data, dict):
        raise _validation_error("Request body must be a JSON object.")
    return data


def _query_param(request: Request, name: str, parameter: inspect.Parameter):
    if name in request.query_params:
        value = request.query_params[name]
    elif parameter.default is not inspect.Signature.empty:
        return parameter.default
    else:
        raise _validation_error(f"Query parameter is required: {name}", {"field": name})
    if isinstance(parameter.default, bool):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise _validation_error(f"Query parameter must be a boolean: {name}", {"field": name})
    if isinstance(parameter.default, int):
        try:
            return int(value)
        except ValueError as exc:
            raise _validation_error(f"Query parameter must be an integer: {name}", {"field": name}) from exc
    return value


def _required_str(body: dict, name: str, *, min_length: int = 0) -> str:
    if name not in body:
        raise _validation_error(f"Field is required: {name}", {"field": name})
    value = body[name]
    if not isinstance(value, str):
        raise _validation_error(f"Field must be a string: {name}", {"field": name})
    if len(value) < min_length:
        raise _validation_error(f"Field is too short: {name}", {"field": name, "minLength": min_length})
    return value


def _optional_str(body: dict, name: str) -> str | None:
    if name not in body or body[name] is None:
        return None
    return _required_str(body, name)


def _required_int(body: dict, name: str, *, minimum: int | None = None) -> int:
    if name not in body:
        raise _validation_error(f"Field is required: {name}", {"field": name})
    value = body[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise _validation_error(f"Field must be an integer: {name}", {"field": name})
    if minimum is not None and value < minimum:
        raise _validation_error(f"Field must be greater than or equal to {minimum}: {name}", {"field": name, "minimum": minimum})
    return value


def _optional_int(body: dict, name: str, *, minimum: int | None = None) -> int | None:
    if name not in body or body[name] is None:
        return None
    return _required_int(body, name, minimum=minimum)


def _optional_bool(body: dict, name: str, default: bool | None) -> bool | None:
    if name not in body or body[name] is None:
        return default
    value = body[name]
    if not isinstance(value, bool):
        raise _validation_error(f"Field must be a boolean: {name}", {"field": name})
    return value


def _optional_str_list(body: dict, name: str) -> list[str] | None:
    if name not in body or body[name] is None:
        return None
    value = body[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _validation_error(f"Field must be a string array: {name}", {"field": name})
    return value


def _attachments(body: dict) -> list[dict]:
    if "attachments" not in body or body["attachments"] is None:
        return []
    value = body["attachments"]
    if not isinstance(value, list):
        raise _validation_error("Field must be an array: attachments", {"field": "attachments"})
    attachments: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _validation_error("Attachment must be an object.", {"field": "attachments", "index": index})
        attachments.append(
            {
                "path": _required_str(item, "path", min_length=1),
                "name": _optional_str(item, "name"),
                "kind": _optional_str(item, "kind") or "file",
            }
        )
    return attachments


def _validation_error(message: str, details: dict | None = None) -> AppError:
    return AppError("validation_error", message, 422, details)


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
        "untrusted": "on-request",
    }
    normalized = aliases.get(value, value)
    if normalized not in {"on-failure", "on-request", "never"}:
        raise AppError("validation_error", "Approval policy is invalid.", 400)
    return normalized


def _normalized_model(value: str) -> str:
    normalized = value.strip()
    if "\x00" in normalized or len(normalized) > 120:
        raise AppError("validation_error", "Model is invalid.", 400)
    return normalized


def _normalized_reasoning_effort(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {"default": "", "none": ""}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise AppError("validation_error", "Reasoning effort is invalid.", 400)
    return normalized


def _static_model_options(selected: str = "") -> list[str]:
    return _with_selected_model(["", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5", "gpt-5-codex"], selected)


def _with_selected_model(models: list[str], selected: str) -> list[str]:
    result: list[str] = []
    for model in ["", *models, selected]:
        if isinstance(model, str) and model not in result:
            result.append(model)
    return result


def _model_catalog_from_response(response: object) -> tuple[list[str], dict[str, list[str]]]:
    if not isinstance(response, dict):
        return [], {}
    candidates = response.get("models")
    if not isinstance(candidates, list):
        candidates = response.get("data")
    if not isinstance(candidates, list):
        return [], {}
    result: list[str] = []
    efforts_by_model: dict[str, list[str]] = {}
    for item in candidates:
        if isinstance(item, str):
            model_id = item.strip()
            efforts: object = None
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or "").strip()
            efforts = item.get("supportedReasoningEfforts") or item.get("reasoningEfforts")
        else:
            model_id = ""
            efforts = None
        if model_id and "\x00" not in model_id and len(model_id) <= 120 and model_id not in result:
            result.append(model_id)
            if isinstance(efforts, list):
                normalized_efforts = []
                for effort in efforts:
                    if isinstance(effort, dict):
                        effort = effort.get("reasoningEffort") or effort.get("id") or ""
                    try:
                        normalized = _normalized_reasoning_effort(str(effort))
                    except AppError:
                        continue
                    if normalized and normalized not in normalized_efforts:
                        normalized_efforts.append(normalized)
                if normalized_efforts:
                    efforts_by_model[model_id] = normalized_efforts
    return result, efforts_by_model


def _model_ids_from_response(response: object) -> list[str]:
    return _model_catalog_from_response(response)[0]


def _static_reasoning_efforts(model: str = "") -> list[str]:
    if model.endswith("luna"):
        return ["", "low", "medium", "high", "xhigh", "max"]
    return ["", "low", "medium", "high", "xhigh", "max", "ultra"]


def _model_list_out(models: list[str], efforts_by_model: dict[str, list[str]], selected: str, dynamic: bool) -> dict:
    return {
        "availableModels": _with_selected_model(models, selected),
        "reasoningEffortsByModel": efforts_by_model,
        "dynamic": dynamic,
    }


def _settings_out(settings: AppServerRuntimeSettings, models: list[str]) -> dict:
    return {
        "permissionProfile": settings.permission_profile,
        "approvalPolicy": settings.approval_policy,
        "model": settings.model,
        "reasoningEffort": settings.reasoning_effort,
        "availablePermissionProfiles": [":read-only", ":workspace", ":danger-full-access"],
        "availableApprovalPolicies": ["on-failure", "on-request", "never"],
        "availableModels": models,
        "availableReasoningEfforts": _static_reasoning_efforts(settings.model),
    }


def _settings_path(config: Config) -> Path:
    return config.app_data_dir / "settings.json"


def _load_app_settings(config: Config) -> AppServerRuntimeSettings:
    permission_profile = config.permission_profile
    approval_policy = config.approval_policy
    model = config.model
    reasoning_effort = ""
    path = _settings_path(config)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            permission_value = data.get("permissionProfile")
            approval_value = data.get("approvalPolicy")
            model_value = data.get("model")
            reasoning_value = data.get("reasoningEffort")
            if isinstance(permission_value, str):
                permission_profile = permission_value
            if isinstance(approval_value, str):
                approval_policy = approval_value
            if isinstance(model_value, str):
                model = model_value
            if isinstance(reasoning_value, str):
                reasoning_effort = reasoning_value
    except (OSError, json.JSONDecodeError, AppError):
        pass
    return AppServerRuntimeSettings(
        permission_profile=_normalized_permission_profile(permission_profile),
        approval_policy=_normalized_approval_policy(approval_policy),
        model=_normalized_model(model),
        reasoning_effort=_normalized_reasoning_effort(reasoning_effort),
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
                "reasoningEffort": settings.reasoning_effort,
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
