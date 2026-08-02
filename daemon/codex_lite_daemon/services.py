from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .db import Database
from .errors import AppError
from .models import chat_out, message_out, project_out
from .util.ids import new_id
from .util.time import utc_now


class ProjectService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    def list_projects(self) -> list[dict]:
        rows = self.db.fetchall("SELECT * FROM projects ORDER BY updated_at DESC, name ASC")
        return [project_out(row) for row in rows]

    def get_project_row(self, project_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            raise AppError("project_not_found", "Project was not found.", 404)
        return row

    def get_project(self, project_id: str) -> dict:
        return project_out(self.get_project_row(project_id))

    def create_project(self, path: str, name: str | None = None) -> dict:
        project_path = self._validate_project_path(path)
        now = utc_now()
        project_id = new_id("prj")
        project_name = (name or project_path.name).strip() or project_path.name
        try:
            self.db.execute(
                "INSERT INTO projects(id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, project_name, str(project_path), now, now),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise AppError("validation_error", "Project path is already registered.", 409) from exc
            raise
        return self.get_project(project_id)

    def update_project(self, project_id: str, name: str | None) -> dict:
        self.get_project_row(project_id)
        clean_name = (name or "").strip()
        if not clean_name:
            raise AppError("validation_error", "Project name must not be empty.")
        self.db.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (clean_name, utc_now(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        self.get_project_row(project_id)
        chat_rows = self.db.fetchall("SELECT id FROM chats WHERE project_id = ?", (project_id,))
        chat_ids = [row["id"] for row in chat_rows]
        if chat_ids:
            placeholders = ", ".join("?" for _ in chat_ids)
            self.db.execute(f"DELETE FROM runs WHERE chat_id IN ({placeholders})", tuple(chat_ids))
            self.db.execute(f"DELETE FROM messages WHERE chat_id IN ({placeholders})", tuple(chat_ids))
            self.db.execute(f"DELETE FROM chats WHERE id IN ({placeholders})", tuple(chat_ids))
        self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def _validate_project_path(self, value: str) -> Path:
        if "\x00" in value:
            raise AppError("project_path_invalid", "Project path contains a NUL character.")
        if "\\" in value or _looks_like_windows_path(value):
            raise AppError("project_path_invalid", "Windows paths are not supported in MVP.")
        path = Path(os.path.expandvars(value)).expanduser()
        if not path.is_absolute():
            raise AppError("project_path_invalid", "Project path must be an absolute WSL path.")
        if str(path).startswith("/mnt/c") and not self.config.allow_mnt_c_projects:
            raise AppError("project_path_invalid", "/mnt/c projects are disabled by default.")
        if not path.exists():
            raise AppError("project_path_not_directory", "Project path does not exist.", 404)
        if not path.is_dir():
            raise AppError("project_path_not_directory", "Project path is not a directory.", 400)
        return path.resolve()


class ChatService:
    def __init__(self, db: Database, projects: ProjectService) -> None:
        self.db = db
        self.projects = projects

    def list_chats(self, project_id: str) -> list[dict]:
        self.projects.get_project_row(project_id)
        rows = self.db.fetchall(
            "SELECT * FROM chats WHERE project_id = ? AND archived_at IS NULL AND can_continue = 1 ORDER BY updated_at DESC, created_at DESC",
            (project_id,),
        )
        return [chat_out(row) for row in _dedupe_chat_rows(rows)]

    def get_chat_row(self, project_id: str, chat_id: str) -> dict:
        self.projects.get_project_row(project_id)
        row = self.db.fetchone("SELECT * FROM chats WHERE id = ? AND project_id = ?", (chat_id, project_id))
        if row is None:
            raise AppError("chat_not_found", "Chat was not found.", 404)
        return row

    def get_chat_row_by_id(self, chat_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM chats WHERE id = ?", (chat_id,))
        if row is None:
            raise AppError("chat_not_found", "Chat was not found.", 404)
        return row

    def get_chat(self, project_id: str, chat_id: str) -> dict:
        return chat_out(self.get_chat_row(project_id, chat_id))

    def create_chat(self, project_id: str, title: str | None = None, settings: dict[str, str] | None = None) -> dict:
        self.projects.get_project_row(project_id)
        now = utc_now()
        chat_id = new_id("cht")
        clean_title = _clean_title(title)
        self.db.execute(
            """
            INSERT INTO chats(
                id, project_id, title, codex_session_id, transcript_path,
                created_at, updated_at, archived_at, can_continue,
                continue_disabled_reason, permission_profile, approval_policy,
                approvals_reviewer, model, reasoning_effort
            ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, 1, NULL, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                project_id,
                clean_title,
                now,
                now,
                (settings or {}).get("permission_profile"),
                (settings or {}).get("approval_policy"),
                (settings or {}).get("approvals_reviewer"),
                (settings or {}).get("model"),
                (settings or {}).get("reasoning_effort"),
            ),
        )
        return self.get_chat(project_id, chat_id)

    def get_chat_settings_row(self, project_id: str, chat_id: str) -> dict:
        row = self.get_chat_row(project_id, chat_id)
        return {
            "permission_profile": row.get("permission_profile"),
            "approval_policy": row.get("approval_policy"),
            "approvals_reviewer": row.get("approvals_reviewer"),
            "model": row.get("model"),
            "reasoning_effort": row.get("reasoning_effort"),
        }

    def update_chat_settings(self, project_id: str, chat_id: str, settings: dict[str, str]) -> dict:
        self.get_chat_row(project_id, chat_id)
        self.db.execute(
            """
            UPDATE chats
            SET permission_profile = ?, approval_policy = ?, approvals_reviewer = ?,
                model = ?, reasoning_effort = ?
            WHERE id = ?
            """,
            (
                settings.get("permission_profile"),
                settings.get("approval_policy"),
                settings.get("approvals_reviewer"),
                settings.get("model"),
                settings.get("reasoning_effort"),
                chat_id,
            ),
        )
        return self.get_chat_settings_row(project_id, chat_id)

    def upsert_chat_index(
        self,
        project_id: str,
        chat_id: str,
        title: str | None,
        codex_session_id: str | None,
        created_at: str | None,
        updated_at: str | None,
        transcript_path: str | None = None,
        archived_at: str | None = None,
        sync_archived: bool = False,
        can_continue: bool = True,
        continue_disabled_reason: str | None = None,
    ) -> dict:
        self.projects.get_project_row(project_id)
        now = utc_now()
        clean_title = _clean_title(title)
        created = created_at or now
        updated = updated_at or created
        archived_update = "archived_at = ?" if sync_archived else "archived_at = archived_at"
        title_update = "title = CASE WHEN title_override_at IS NOT NULL AND title <> ? THEN title WHEN julianday(updated_at) > julianday(?) THEN title ELSE ? END"
        title_override_update = "title_override_at = title_override_at"
        updated_update = "updated_at = CASE WHEN julianday(updated_at) > julianday(?) THEN updated_at ELSE ? END"
        if codex_session_id:
            existing = self.db.fetchone(
                "SELECT * FROM chats WHERE project_id = ? AND codex_session_id = ? AND id <> ?",
                (project_id, codex_session_id, chat_id),
            )
            if existing is not None:
                self.db.execute(
                    f"""
                    UPDATE chats
                    SET {title_update}, {title_override_update}, codex_session_id = ?, transcript_path = COALESCE(?, transcript_path), created_at = COALESCE(created_at, ?), {updated_update}, {archived_update}, can_continue = ?, continue_disabled_reason = ?
                    WHERE id = ?
                    """,
                    _archived_params(clean_title, updated, clean_title, codex_session_id, transcript_path, created, updated, updated, archived_at, int(can_continue), continue_disabled_reason, existing["id"], sync_archived),
                )
                return self.get_chat(project_id, existing["id"])
        archived_conflict = "excluded.archived_at" if sync_archived else "chats.archived_at"
        self.db.execute(
            f"""
            INSERT INTO chats(id, project_id, title, codex_session_id, transcript_path, created_at, updated_at, archived_at, can_continue, continue_disabled_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              project_id = excluded.project_id,
              title = CASE WHEN chats.title_override_at IS NOT NULL AND chats.title <> excluded.title THEN chats.title WHEN julianday(chats.updated_at) > julianday(excluded.updated_at) THEN chats.title ELSE excluded.title END,
              title_override_at = chats.title_override_at,
              codex_session_id = excluded.codex_session_id,
              transcript_path = COALESCE(excluded.transcript_path, chats.transcript_path),
              created_at = COALESCE(chats.created_at, excluded.created_at),
              updated_at = CASE WHEN julianday(chats.updated_at) > julianday(excluded.updated_at) THEN chats.updated_at ELSE excluded.updated_at END,
              archived_at = {archived_conflict},
              can_continue = excluded.can_continue,
              continue_disabled_reason = excluded.continue_disabled_reason
            """,
            (chat_id, project_id, clean_title, codex_session_id, transcript_path, created, updated, archived_at, int(can_continue), continue_disabled_reason),
        )
        return self.get_chat(project_id, chat_id)

    def update_chat(self, project_id: str, chat_id: str, title: str | None) -> dict:
        self.get_chat_row(project_id, chat_id)
        clean_title = _clean_title(title, allow_default=False)
        if not clean_title:
            raise AppError("validation_error", "Chat title must not be empty.")
        now = utc_now()
        self.db.execute("UPDATE chats SET title = ?, title_override_at = ?, updated_at = ? WHERE id = ?", (clean_title, now, now, chat_id))
        return self.get_chat(project_id, chat_id)

    def update_chat_session_id(self, project_id: str, chat_id: str, codex_session_id: str) -> dict:
        self.get_chat_row(project_id, chat_id)
        self.db.execute("UPDATE chats SET codex_session_id = ?, updated_at = ? WHERE id = ?", (codex_session_id, utc_now(), chat_id))
        return self.get_chat(project_id, chat_id)

    def update_chat_transcript_path(self, project_id: str, chat_id: str, transcript_path: str) -> None:
        self.get_chat_row(project_id, chat_id)
        self.db.execute("UPDATE chats SET transcript_path = ? WHERE id = ?", (transcript_path, chat_id))

    def archive_chat(self, project_id: str, chat_id: str) -> dict:
        self.get_chat_row(project_id, chat_id)
        now = utc_now()
        self.db.execute("UPDATE chats SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, chat_id))
        return self.get_chat(project_id, chat_id)

    def archive_stale_imported_chats(self, project_id: str, active_chat_ids: set[str]) -> int:
        self.projects.get_project_row(project_id)
        now = utc_now()
        active_filter = ""
        params: list[object] = [now, now, project_id]
        if active_chat_ids:
            placeholders = ", ".join("?" for _ in active_chat_ids)
            active_filter = f"AND id NOT IN ({placeholders})"
            params.extend(sorted(active_chat_ids))
        cursor = self.db.execute(
            f"""
            UPDATE chats
            SET archived_at = ?, updated_at = ?
            WHERE project_id = ?
              AND archived_at IS NULL
              {active_filter}
              AND (codex_session_id IS NOT NULL OR transcript_path IS NOT NULL)
            """,
            tuple(params),
        )
        return int(cursor.rowcount or 0)

    def delete_chat(self, project_id: str, chat_id: str) -> None:
        self.get_chat_row(project_id, chat_id)
        self.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


class MessageService:
    def __init__(self, db: Database, chats: ChatService) -> None:
        self.db = db
        self.chats = chats

    def list_messages(self, project_id: str, chat_id: str) -> list[dict]:
        self.chats.get_chat_row(project_id, chat_id)
        rows = self.db.fetchall("SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC", (chat_id,))
        return [message_out(row) for row in rows]

    def insert_message(self, chat_id: str, role: str, content: str, run_id: str | None = None, kind: str | None = None) -> dict:
        message_id = new_id("msg")
        now = utc_now()
        clean_kind = _message_kind(role, kind)
        self.db.execute(
            "INSERT INTO messages(id, chat_id, role, content, run_id, created_at, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, chat_id, role, content, run_id, now, clean_kind),
        )
        self.db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        row = self.db.fetchone("SELECT * FROM messages WHERE id = ?", (message_id,))
        assert row is not None
        return message_out(row)


def _looks_like_windows_path(value: str) -> bool:
    return len(value) >= 3 and value[1:3] == ":/"


def _message_kind(role: str, kind: str | None) -> str:
    if kind in {"instruction", "work", "conclusion", "waiting", "status", "activity"}:
        return kind
    raise AppError("validation_error", "Message kind is required.", 400)


def _clean_title(value: str | None, allow_default: bool = True) -> str:
    text = " ".join((value or "").split())
    if not text:
        return "New Chat" if allow_default else ""
    if len(text) > 80:
        return text[:77].rstrip() + "..."
    return text


def _archived_params(*values: object) -> tuple[object, ...]:
    *prefix, archived_at, can_continue, continue_disabled_reason, row_id, sync_archived = values
    if sync_archived:
        return (*prefix, archived_at, can_continue, continue_disabled_reason, row_id)
    return (*prefix, can_continue, continue_disabled_reason, row_id)


def _dedupe_chat_rows(rows: list[dict]) -> list[dict]:
    duplicate_session_ids = {
        session_id
        for session_id in (row.get("codex_session_id") for row in rows)
        if session_id and sum(1 for item in rows if item.get("codex_session_id") == session_id) > 1
    }
    if not duplicate_session_ids:
        return rows

    visible: list[dict] = []
    seen_sessions: set[str] = set()
    for row in rows:
        session_id = row.get("codex_session_id")
        if not session_id or session_id not in duplicate_session_ids:
            visible.append(row)
            continue
        if session_id in seen_sessions:
            continue
        exact = next((item for item in rows if item.get("codex_session_id") == session_id and item.get("id") == session_id), None)
        visible.append(exact or row)
        seen_sessions.add(session_id)
    return visible
