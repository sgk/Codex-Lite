from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str | None = None
    path: str


class ProjectUpdate(BaseModel):
    name: str | None = None


class ProjectCandidateImport(BaseModel):
    paths: list[str] | None = None


class ChatCreate(BaseModel):
    title: str | None = None


class ChatUpdate(BaseModel):
    title: str | None = None


class AutomationCreate(BaseModel):
    name: str
    prompt: str = Field(min_length=1)
    interval_minutes: int = Field(ge=1)
    enabled: bool = True


class AutomationUpdate(BaseModel):
    name: str | None = None
    prompt: str | None = None
    interval_minutes: int | None = Field(default=None, ge=1)
    enabled: bool | None = None


class MessageAttachment(BaseModel):
    path: str
    name: str | None = None
    kind: str = "file"


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)
    attachments: list[MessageAttachment] = Field(default_factory=list)


class RunSteer(BaseModel):
    content: str = Field(min_length=1)


def project_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "path": row["path"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def chat_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "title": _display_title(row["title"]),
        "codexSessionId": row["codex_session_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "canContinue": bool(row.get("can_continue", 1)),
        "continueDisabledReason": row.get("continue_disabled_reason"),
    }


def message_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "chatId": row["chat_id"],
        "role": row["role"],
        "content": row["content"],
        "runId": row["run_id"],
        "createdAt": row["created_at"],
        "kind": row["kind"],
    }


def automation_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "chatId": row["chat_id"],
        "name": row["name"],
        "prompt": row["prompt"],
        "scheduleKind": row["schedule_kind"],
        "intervalMinutes": row["interval_minutes"],
        "enabled": bool(row["enabled"]),
        "running": bool(row.get("running", 0)),
        "nextRunAt": row.get("next_run_at"),
        "lastRunAt": row.get("last_run_at"),
        "lastError": row.get("last_error"),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def run_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "chatId": row["chat_id"],
        "status": row["status"],
        "pid": row["pid"],
        "exitCode": row["exit_code"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "logPath": row["log_path"],
        "error": row["error"],
    }


def _display_title(value: str | None) -> str:
    text = " ".join((value or "").split())
    if not text:
        return "New Chat"
    if len(text) > 80:
        return text[:77].rstrip() + "..."
    return text
