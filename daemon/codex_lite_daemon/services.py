from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

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

    def relocate_project(self, project_id: str, parent_path: str, directory_name: str) -> dict:
        project = self.get_project_row(project_id)
        source = Path(project["path"])
        if not source.exists() or not source.is_dir():
            raise AppError("project_path_not_directory", "The registered project directory does not exist.", 404)
        source = source.resolve()
        if source == Path("/") or source == Path.home().resolve():
            raise AppError("project_move_unsafe_source", "The filesystem root and user home cannot be moved as projects.", 409)
        parent = self._validate_project_path(parent_path)
        clean_directory_name = directory_name.strip()
        if (
            not clean_directory_name
            or clean_directory_name in {".", ".."}
            or "/" in clean_directory_name
            or "\\" in clean_directory_name
            or "\x00" in clean_directory_name
        ):
            raise AppError("project_directory_name_invalid", "Project directory name must be one path component.")

        target = (parent / clean_directory_name).resolve()
        if target == source:
            return self.get_project(project_id)
        try:
            if target.is_relative_to(source):
                raise AppError("project_move_invalid", "A project cannot be moved inside itself.")
        except ValueError:
            pass
        if target.exists():
            raise AppError("project_destination_exists", "The destination already exists.", 409)
        registered = self.db.fetchone("SELECT id FROM projects WHERE path = ? AND id <> ?", (str(target), project_id))
        if registered is not None:
            raise AppError("project_destination_registered", "The destination is already registered.", 409)
        active_run = self.db.fetchone(
            """
            SELECT runs.id
            FROM runs
            JOIN chats ON chats.id = runs.chat_id
            WHERE chats.project_id = ? AND runs.status IN ('queued', 'running')
            LIMIT 1
            """,
            (project_id,),
        )
        if active_run is not None:
            raise AppError("project_move_active_run", "Wait for active project runs to finish before moving the directory.", 409)
        active_automation = self.db.fetchone(
            "SELECT id FROM automations WHERE project_id = ? AND running = 1 LIMIT 1",
            (project_id,),
        )
        if active_automation is not None:
            raise AppError("project_move_active_automation", "Wait for the running automation to finish before moving the directory.", 409)
        try:
            if source.stat().st_dev != parent.stat().st_dev:
                raise AppError("project_move_cross_device", "Moving projects across filesystems is not supported.", 409)
        except OSError as exc:
            raise AppError("project_move_failed", f"Could not inspect the project location: {exc}", 409) from exc

        try:
            source.rename(target)
        except OSError as exc:
            raise AppError("project_move_failed", f"Could not move the project directory: {exc}", 409) from exc
        try:
            self.db.execute("UPDATE projects SET path = ?, updated_at = ? WHERE id = ?", (str(target), utc_now(), project_id))
        except Exception as exc:
            try:
                target.rename(source)
            except OSError as rollback_exc:
                raise AppError(
                    "project_move_rollback_failed",
                    f"The directory moved to {target}, but registration failed and rollback also failed: {rollback_exc}",
                    500,
                ) from exc
            raise AppError("project_move_failed", "Project registration could not be updated; the directory was restored.", 500) from exc
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

    def mark_resume_on_start(self, chat_ids: list[str]) -> int:
        now = utc_now()
        marked = 0
        for chat_id in dict.fromkeys(chat_ids):
            cursor = self.db.execute(
                "UPDATE chats SET resume_on_start = 1, resume_requested_at = ? WHERE id = ? AND archived_at IS NULL AND can_continue = 1",
                (now, chat_id),
            )
            marked += int(cursor.rowcount or 0)
        return marked

    def list_resume_on_start(self) -> list[dict]:
        return self.db.fetchall(
            """
            SELECT id AS chat_id, project_id, resume_requested_at
            FROM chats
            WHERE resume_on_start = 1 AND archived_at IS NULL AND can_continue = 1
            ORDER BY resume_requested_at ASC, created_at ASC
            """
        )

    def clear_resume_on_start(self, chat_id: str) -> None:
        self.db.execute(
            "UPDATE chats SET resume_on_start = 0, resume_requested_at = NULL WHERE id = ?",
            (chat_id,),
        )

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

    def record_model_reasoning_choice(self, model: str, reasoning_effort: str) -> list[dict]:
        clean_model = str(model or "").strip()
        clean_effort = str(reasoning_effort or "").strip()
        if not clean_model:
            return self.list_model_reasoning_history()
        now = utc_now()
        self.db.execute(
            "DELETE FROM model_reasoning_history WHERE model = ? AND reasoning_effort = ?",
            (clean_model, clean_effort),
        )
        self.db.execute(
            "INSERT INTO model_reasoning_history(model, reasoning_effort, used_at) VALUES (?, ?, ?)",
            (clean_model, clean_effort, now),
        )
        self.db.execute(
            """
            DELETE FROM model_reasoning_history
            WHERE id NOT IN (
                SELECT id FROM model_reasoning_history
                ORDER BY used_at DESC, id DESC
                LIMIT 3
            )
            """
        )
        return self.list_model_reasoning_history()

    def list_model_reasoning_history(self) -> list[dict]:
        return self.db.fetchall(
            "SELECT model, reasoning_effort FROM model_reasoning_history ORDER BY used_at DESC, id DESC LIMIT 3"
        )

    def seed_model_reasoning_history(self, current_model: str, current_reasoning_effort: str) -> list[dict]:
        """Backfill the first history entries from settings that predate the feature."""
        if self.db.fetchone("SELECT id FROM model_reasoning_history LIMIT 1") is not None:
            return self.list_model_reasoning_history()

        rows = self.db.fetchall(
            """
            SELECT model, reasoning_effort
            FROM chats
            WHERE model IS NOT NULL AND trim(model) <> ''
            ORDER BY updated_at DESC, created_at DESC
            """
        )
        choices: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in reversed(rows):
            choice = (str(row.get("model") or "").strip(), str(row.get("reasoning_effort") or "").strip())
            if choice[0] and choice not in seen:
                choices.append(choice)
                seen.add(choice)
        current = (str(current_model or "").strip(), str(current_reasoning_effort or "").strip())
        if current[0] and current not in seen:
            choices.append(current)
        for model, effort in choices:
            self.record_model_reasoning_choice(model, effort)
        return self.list_model_reasoning_history()

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

    def get_provider_thread(self, project_id: str, chat_id: str, provider: str) -> dict | None:
        self.get_chat_row(project_id, chat_id)
        return self.db.fetchone(
            "SELECT * FROM chat_provider_threads WHERE chat_id = ? AND provider = ?",
            (chat_id, provider),
        )

    def list_provider_threads(self, project_id: str, chat_id: str) -> list[dict]:
        self.get_chat_row(project_id, chat_id)
        return self.db.fetchall(
            "SELECT * FROM chat_provider_threads WHERE chat_id = ? ORDER BY provider ASC",
            (chat_id,),
        )

    def upsert_provider_thread(
        self,
        project_id: str,
        chat_id: str,
        provider: str,
        thread_id: str,
        transcript_path: str | None = None,
        history_initialized: bool | None = None,
    ) -> dict:
        self.get_chat_row(project_id, chat_id)
        now = utc_now()
        initialized_value = int(bool(history_initialized)) if history_initialized is not None else None
        self.db.execute(
            """
            INSERT INTO chat_provider_threads(
                chat_id, provider, thread_id, transcript_path,
                history_initialized, created_at, updated_at
            ) VALUES (?, ?, ?, ?, COALESCE(?, 0), ?, ?)
            ON CONFLICT(chat_id, provider) DO UPDATE SET
              thread_id = excluded.thread_id,
              transcript_path = COALESCE(excluded.transcript_path, chat_provider_threads.transcript_path),
              history_initialized = COALESCE(?, chat_provider_threads.history_initialized),
              updated_at = excluded.updated_at
            """,
            (chat_id, provider, thread_id, transcript_path, initialized_value, now, now, initialized_value),
        )
        row = self.get_provider_thread(project_id, chat_id, provider)
        assert row is not None
        return row

    def update_provider_thread_transcript_path(self, project_id: str, chat_id: str, provider: str, transcript_path: str) -> None:
        self.get_chat_row(project_id, chat_id)
        self.db.execute(
            "UPDATE chat_provider_threads SET transcript_path = ?, updated_at = ? WHERE chat_id = ? AND provider = ?",
            (transcript_path, utc_now(), chat_id, provider),
        )

    def touch_provider_thread(self, chat_id: str, provider: str) -> None:
        self.db.execute(
            "UPDATE chat_provider_threads SET updated_at = ? WHERE chat_id = ? AND provider = ?",
            (utc_now(), chat_id, provider),
        )

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
        self._insert_listener: Callable[[str], None] | None = None

    def set_insert_listener(self, listener: Callable[[str], None]) -> None:
        self._insert_listener = listener

    def list_messages(self, project_id: str, chat_id: str) -> list[dict]:
        self.chats.get_chat_row(project_id, chat_id)
        rows = self.db.fetchall(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY created_at ASC, rowid ASC",
            (chat_id,),
        )
        return [message_out(row) for row in rows]

    def list_input_history(self, project_id: str, chat_id: str, limit: int = 100) -> list[str]:
        self.chats.get_chat_row(project_id, chat_id)
        rows = self.db.fetchall(
            """
            SELECT content
            FROM messages
            WHERE chat_id = ? AND role = 'user' AND kind = 'instruction'
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return [str(row["content"]).strip() for row in reversed(rows) if str(row["content"]).strip()]

    def get_run_conclusion(self, run_id: str) -> dict | None:
        run = self.db.fetchone("SELECT id FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise AppError("run_not_found", "Run was not found.", 404)
        row = self.db.fetchone(
            """
            SELECT *
            FROM messages
            WHERE run_id = ? AND role = 'assistant' AND kind = 'conclusion'
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (run_id,),
        )
        return message_out(row) if row is not None else None

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
        if self._insert_listener is not None:
            self._insert_listener(chat_id)
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
