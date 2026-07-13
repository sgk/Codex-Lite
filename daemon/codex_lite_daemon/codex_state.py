from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config


class CodexStateSchemaError(Exception):
    pass


@dataclass(frozen=True)
class CodexThreadMetadata:
    id: str
    path: Path
    title: str | None
    transcript_path: Path | None
    created_at: str | None
    updated_at: str | None
    archived_at: str | None
    archived: bool
    can_continue: bool
    continue_disabled_reason: str | None


class CodexStateService:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._status: dict[str, Any] = {
            "ok": False,
            "path": None,
            "error": None,
            "threadCount": 0,
        }

    def list_threads(self) -> list[CodexThreadMetadata]:
        db_path = self._find_state_db()
        if db_path is None:
            self._status = {"ok": False, "path": None, "error": "Codex state DB with threads table was not found.", "threadCount": 0}
            return []
        try:
            threads = self._read_threads(db_path)
        except CodexStateSchemaError as exc:
            self._status = {"ok": False, "path": str(db_path), "error": str(exc), "threadCount": 0}
            return []
        except sqlite3.Error as exc:
            self._status = {"ok": False, "path": str(db_path), "error": f"SQLite read failed: {type(exc).__name__}", "threadCount": 0}
            return []
        except OSError as exc:
            self._status = {"ok": False, "path": str(db_path), "error": f"Path validation failed: {type(exc).__name__}", "threadCount": 0}
            return []
        self._status = {"ok": True, "path": str(db_path), "error": None, "threadCount": len(threads)}
        return threads

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._status)

    def _find_state_db(self) -> Path | None:
        root = self.config.codex_sqlite_home
        if not root.exists():
            return None
        candidates = sorted(root.glob("state*.sqlite"), key=lambda path: (path.name == "state.sqlite", -_safe_mtime(path)))
        for candidate in candidates:
            if candidate.name == "state.sqlite" and candidate.stat().st_size == 0:
                continue
            try:
                with sqlite3.connect(f"file:{candidate}?mode=ro", uri=True) as conn:
                    if _has_threads_table(conn):
                        return candidate.resolve()
            except (sqlite3.Error, OSError):
                continue
        return None

    def _read_threads(self, db_path: Path) -> list[CodexThreadMetadata]:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            columns = _table_columns(conn, "threads")
            required = {"id", "cwd", "title", "archived", "rollout_path"}
            if not required.issubset(columns):
                missing = ", ".join(sorted(required - columns))
                raise CodexStateSchemaError(f"threads table is missing columns: {missing}")
            select_columns = [
                "id",
                "cwd",
                "title",
                "preview" if "preview" in columns else "NULL AS preview",
                "first_user_message" if "first_user_message" in columns else "NULL AS first_user_message",
                "archived",
                "archived_at" if "archived_at" in columns else "NULL AS archived_at",
                "rollout_path",
                "created_at" if "created_at" in columns else "NULL AS created_at",
                "updated_at" if "updated_at" in columns else "NULL AS updated_at",
                "recency_at" if "recency_at" in columns else "NULL AS recency_at",
                "created_at_ms" if "created_at_ms" in columns else "NULL AS created_at_ms",
                "updated_at_ms" if "updated_at_ms" in columns else "NULL AS updated_at_ms",
                "recency_at_ms" if "recency_at_ms" in columns else "NULL AS recency_at_ms",
                "archived_at_ms" if "archived_at_ms" in columns else "NULL AS archived_at_ms",
            ]
            rows = conn.execute(f"SELECT {', '.join(select_columns)} FROM threads").fetchall()

        threads: list[CodexThreadMetadata] = []
        for row in rows:
            thread = self._thread_from_row(row)
            if thread is not None:
                threads.append(thread)
        return threads

    def _thread_from_row(self, row: sqlite3.Row) -> CodexThreadMetadata | None:
        thread_id = row["id"]
        cwd = row["cwd"]
        if not isinstance(thread_id, str) or not thread_id.strip() or not isinstance(cwd, str):
            return None
        project_path = self._candidate_path(cwd)
        if project_path is None:
            return None
        transcript_path = self._transcript_path(row["rollout_path"])
        archived = bool(row["archived"])
        can_continue, continue_disabled_reason = self._continue_state(transcript_path, archived)
        created_at = _timestamp(row["created_at_ms"]) or _timestamp(row["created_at"])
        updated_at = _timestamp(row["recency_at_ms"]) or _timestamp(row["updated_at_ms"]) or _timestamp(row["recency_at"]) or _timestamp(row["updated_at"]) or created_at
        archived_at = _timestamp(row["archived_at_ms"]) or _timestamp(row["archived_at"])
        title = _thread_title(row["title"], row["preview"], row["first_user_message"])
        return CodexThreadMetadata(
            id=thread_id,
            path=project_path,
            title=title,
            transcript_path=transcript_path,
            created_at=created_at,
            updated_at=updated_at,
            archived_at=archived_at or (updated_at if archived else None),
            archived=archived,
            can_continue=can_continue,
            continue_disabled_reason=continue_disabled_reason,
        )

    def _continue_state(self, transcript_path: Path | None, archived: bool) -> tuple[bool, str | None]:
        if archived:
            return False, "このチャットはCodex側でアーカイブ済みです。"
        if transcript_path is None:
            return False, "履歴ファイルが見つからないため、このチャットは履歴表示専用です。"
        try:
            if transcript_path.is_relative_to(self.config.codex_home / "sessions"):
                return True, None
        except ValueError:
            pass
        return False, "このチャットは現在のCODEX_HOME外の履歴のため、Codex Liteからは継続できません。"

    def _candidate_path(self, value: str) -> Path | None:
        if "\x00" in value:
            return None
        path_value = _windows_path_to_wsl(value)
        if "\\" in path_value:
            return None
        path = Path(path_value)
        if not path.is_absolute():
            return None
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if str(resolved).startswith("/mnt/c") and not self.config.allow_mnt_c_projects:
            return None
        if not resolved.exists() or not resolved.is_dir():
            return None
        return resolved

    def _transcript_path(self, value: Any) -> Path | None:
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        path_value = _windows_path_to_wsl(value)
        if "\\" in path_value:
            return None
        path = Path(path_value)
        if not path.is_absolute():
            path = self.config.codex_home / path
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_file() or resolved.suffix != ".jsonl":
            return None
        allowed_roots = [self.config.codex_home / child for child in ("sessions", "archived_sessions")]
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            return None
        return resolved


def _has_threads_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'threads'").fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _timestamp(int(text))
        return text
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds = seconds / 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _thread_title(title: Any, preview: Any, first_user_message: Any) -> str | None:
    title_text = _clean_title(title)
    preview_text = _clean_title(preview)
    first_user_text = _clean_title(first_user_message)
    if title_text is not None and preview_text is not None and first_user_text is not None and title_text == first_user_text:
        return preview_text
    if title_text is not None and title_text.casefold() not in {"new chat", "chat"}:
        return title_text
    return preview_text or title_text


def _clean_title(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


def _windows_path_to_wsl(value: str) -> str:
    if len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"} and value[0].isalpha():
        drive = value[0].lower()
        rest = value[3:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return value
