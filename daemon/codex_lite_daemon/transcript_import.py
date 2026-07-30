from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .codex_state import CodexStateService, CodexThreadMetadata
from .config import Config
from .services import ChatService, ProjectService


@dataclass
class ProjectCandidate:
    path: Path
    thread_count: int = 0
    last_used_at: str | None = None


@dataclass
class TranscriptSession:
    id: str
    path: Path
    title: str
    transcript_path: Path | None
    timestamp: str | None = None
    created_at: str | None = None
    archived_at: str | None = None
    archived: bool = False
    archived_state_known: bool = False
    can_continue: bool = False
    continue_disabled_reason: str | None = "このチャットは取り込み済み履歴のため、Codex Liteからは継続できません。"


class TranscriptImportService:
    def __init__(self, config: Config, projects: ProjectService, chats: ChatService, codex_state: CodexStateService | None = None) -> None:
        self.config = config
        self.projects = projects
        self.chats = chats
        self.codex_state = codex_state

    def list_project_candidates(self) -> list[dict]:
        candidates: dict[Path, ProjectCandidate] = {}
        registered = {_path_key(Path(project["path"]).resolve()) for project in self.projects.list_projects()}
        for session in self._transcript_sessions():
            if session.archived or _path_key(session.path) in registered:
                continue
            candidate = candidates.setdefault(session.path, ProjectCandidate(path=session.path))
            candidate.thread_count += 1
            if session.timestamp is not None and (candidate.last_used_at is None or session.timestamp > candidate.last_used_at):
                candidate.last_used_at = session.timestamp
        return [
            {
                "path": str(candidate.path),
                "name": candidate.path.name,
                "threadCount": candidate.thread_count,
                "lastUsedAt": candidate.last_used_at,
            }
            for candidate in sorted(candidates.values(), key=lambda item: item.last_used_at or "", reverse=True)
        ]

    def import_project_candidates(self, paths: list[str] | None = None) -> list[dict]:
        imported: list[dict] = []
        candidates = self._candidates_from_paths(paths) if paths is not None else self.list_project_candidates()
        sessions_by_path = self._sessions_by_path()
        for candidate in candidates:
            try:
                project = self.projects.create_project(candidate["path"], candidate["name"])
                self.index_project(project, sessions_by_path)
                imported.append(project)
            except Exception:
                continue
        return imported

    def index_registered_projects(self) -> int:
        sessions_by_path = self._sessions_by_path()
        indexed = 0
        for project in self.projects.list_projects():
            indexed += self.index_project(project, sessions_by_path)
        return indexed

    def index_project(self, project: dict, sessions_by_path: dict[Path, list[TranscriptSession]] | None = None) -> int:
        try:
            project_path = Path(project["path"]).resolve()
        except OSError:
            project_path = Path(project["path"])
        indexed = 0
        all_sessions = sessions_by_path or self._sessions_by_path()
        sessions = all_sessions.get(project_path, [])
        if not sessions:
            project_key = _path_key(project_path)
            sessions = [session for path, path_sessions in all_sessions.items() if _path_key(path) == project_key for session in path_sessions]
        active_session_ids = {session.id for session in sessions if session.can_continue}
        for session in sessions:
            if not session.can_continue:
                continue
            self.chats.upsert_chat_index(
                project["id"],
                session.id,
                session.title,
                session.id,
                session.created_at or session.timestamp,
                session.timestamp,
                str(session.transcript_path) if session.transcript_path is not None else None,
                session.archived_at if session.archived else None,
                session.archived_state_known,
                session.can_continue,
                session.continue_disabled_reason,
            )
            indexed += 1
        if sessions and self.codex_state is not None and self.codex_state.diagnostics().get("ok"):
            self.chats.archive_stale_imported_chats(project["id"], active_session_ids)
        return indexed

    def _candidates_from_paths(self, paths: list[str]) -> list[dict]:
        candidates: list[dict] = []
        seen: set[Path] = set()
        registered = {_path_key(Path(project["path"]).resolve()) for project in self.projects.list_projects()}
        for value in paths:
            path = self._candidate_path(value)
            if path is None or _path_key(path) in registered or path in seen:
                continue
            seen.add(path)
            candidates.append({"path": str(path), "name": path.name, "threadCount": 0, "lastUsedAt": None})
        return candidates

    def _transcript_files(self) -> list[Path]:
        roots = []
        for codex_home in self._codex_homes():
            roots.append(codex_home / "sessions")
        files: list[Path] = []
        for root in roots:
            if root.exists():
                files.extend(root.glob("**/*.jsonl"))
        return files

    def _transcript_sessions(self) -> list[TranscriptSession]:
        if self.codex_state is not None:
            codex_sessions: dict[str, TranscriptSession] = {}
            for thread in self.codex_state.list_threads():
                session = self._session_from_codex_thread(thread, None)
                if session is not None:
                    codex_sessions[session.id] = session
            return list(codex_sessions.values())

        sessions_by_id: dict[str, TranscriptSession] = {}
        for transcript in self._transcript_files():
            session = self._read_transcript_session(transcript)
            if session is not None:
                sessions_by_id[session.id] = session
        return list(sessions_by_id.values())

    def _sessions_by_path(self) -> dict[Path, list[TranscriptSession]]:
        grouped: dict[Path, list[TranscriptSession]] = {}
        for session in self._transcript_sessions():
            grouped.setdefault(session.path, []).append(session)
        return grouped

    def _session_from_codex_thread(self, thread: CodexThreadMetadata, jsonl_session: TranscriptSession | None) -> TranscriptSession | None:
        transcript_path = thread.transcript_path or (jsonl_session.transcript_path if jsonl_session else None)
        return TranscriptSession(
            id=thread.id,
            path=thread.path,
            title=thread.title or (jsonl_session.title if jsonl_session else "New Chat"),
            transcript_path=transcript_path,
            timestamp=thread.updated_at or (jsonl_session.timestamp if jsonl_session else None),
            created_at=thread.created_at or (jsonl_session.created_at if jsonl_session else None),
            archived_at=thread.archived_at,
            archived=thread.archived,
            archived_state_known=True,
            can_continue=thread.can_continue,
            continue_disabled_reason=thread.continue_disabled_reason,
        )

    def _codex_homes(self) -> list[Path]:
        try:
            return [self.config.codex_home.resolve()]
        except OSError:
            return [self.config.codex_home]

    def _read_session_meta(self, path: Path) -> dict[str, Any]:
        session = self._read_transcript_session(path)
        if session is None:
            return {}
        return {"id": session.id, "cwd": str(session.path), "timestamp": session.timestamp}

    def _read_transcript_session(self, path: Path) -> TranscriptSession | None:
        meta: dict[str, Any] | None = None
        last_timestamp: str | None = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for _, line in zip(range(200), handle):
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    item_timestamp = item.get("timestamp")
                    if isinstance(item_timestamp, str):
                        last_timestamp = item_timestamp
                    if meta is None and item.get("type") == "session_meta" and isinstance(item.get("payload"), dict):
                        meta = item["payload"]
                    if meta is None:
                        found = _find_cwd_payload(item)
                        if found is not None:
                            meta = found
            for line in _tail_lines(path):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item_timestamp = item.get("timestamp")
                if isinstance(item_timestamp, str):
                    last_timestamp = item_timestamp
        except OSError:
            return None
        if meta is None:
            return None
        cwd = meta.get("cwd")
        session_id = meta.get("id")
        if not isinstance(cwd, str) or not isinstance(session_id, str) or not session_id.strip():
            return None
        project_path = self._candidate_path(cwd)
        if project_path is None:
            return None
        timestamp = meta.get("timestamp")
        session_timestamp = last_timestamp or (timestamp if isinstance(timestamp, str) else None)
        return TranscriptSession(
            id=session_id,
            path=project_path,
            title="New Chat",
            transcript_path=path,
            timestamp=session_timestamp,
            can_continue=False,
            continue_disabled_reason="このチャットはJSONLから取り込んだ履歴のため、Codex Liteからは継続できません。",
        )

    def list_messages(self, project_path: str, session_id: str, chat_id: str, transcript_path: str | None = None) -> list[dict]:
        transcript = self._validated_transcript_path(transcript_path, project_path, session_id) if transcript_path else None
        if transcript is None:
            transcript = self._find_transcript_path(project_path, session_id)
        if transcript is None:
            return []
        messages: list[dict] = []
        pending_calls: dict[str, dict[str, Any]] = {}
        try:
            with transcript.open("r", encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = _transcript_message(chat_id, item, index, pending_calls)
                    if message is not None:
                        messages.append(message)
        except OSError:
            return []
        return messages

    def find_transcript_path(self, project_path: str, session_id: str, transcript_path: str | None = None) -> Path | None:
        if transcript_path:
            transcript = self._validated_transcript_path(transcript_path, project_path, session_id)
            if transcript is not None:
                return transcript
        return self._find_transcript_path(project_path, session_id)

    def _find_transcript_path(self, project_path: str, session_id: str) -> Path | None:
        try:
            resolved_project = Path(project_path).resolve()
        except OSError:
            resolved_project = Path(project_path)
        for session in self._transcript_sessions():
            if session.id == session_id and session.path == resolved_project:
                return session.transcript_path
        return None

    def _validated_transcript_path(self, value: str | None, project_path: str, session_id: str) -> Path | None:
        if not value or "\x00" in value:
            return None
        path = Path(value)
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_file() or resolved.suffix != ".jsonl":
            return None
        allowed_roots = [home / child for home in self._codex_homes() for child in ("sessions", "archived_sessions")]
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            return None
        session = self._read_transcript_session(resolved)
        if session is None or session.id != session_id:
            return None
        try:
            resolved_project = Path(project_path).resolve()
        except OSError:
            resolved_project = Path(project_path)
        if session.path != resolved_project:
            return None
        return resolved

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


def _find_cwd_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        cwd = value.get("cwd")
        if isinstance(cwd, str):
            return value
        for child in value.values():
            found = _find_cwd_payload(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_cwd_payload(child)
            if found is not None:
                return found
    return None


def _transcript_message(chat_id: str, item: dict[str, Any], index: int, pending_calls: dict[str, dict[str, Any]] | None = None) -> dict | None:
    timestamp = item.get("timestamp")
    created_at = timestamp if isinstance(timestamp, str) else ""
    item_type = item.get("type")
    payload = item.get("payload")
    if item_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
        content = payload.get("message")
        if isinstance(content, str) and content.strip():
            clean_content, attachments = _extract_user_message_attachments(content)
            return {
                "id": str(payload.get("client_id") or f"{chat_id}-user-{index}"),
                "chatId": chat_id,
                "role": "user",
                "content": clean_content,
                "runId": None,
                "createdAt": created_at,
                "kind": "instruction",
                "attachments": attachments,
            }
        return None
    if item_type != "response_item" or not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")
    if payload_type == "function_call":
        call_id = payload.get("call_id")
        if pending_calls is not None and isinstance(call_id, str) and call_id:
            pending_calls[call_id] = payload
        return None
    if payload_type == "function_call_output":
        return _transcript_function_call_message(chat_id, payload, index, created_at, pending_calls or {})
    if payload_type == "reasoning":
        reasoning = _reasoning_summary_text(payload)
        if not reasoning:
            return None
        return {
            "id": str(payload.get("id") or f"{chat_id}-reasoning-{index}"),
            "chatId": chat_id,
            "role": "status",
            "content": "推論の要約",
            "runId": None,
            "createdAt": created_at,
            "kind": "status",
            "activityDetails": reasoning,
        }
    if payload_type != "message" or payload.get("role") != "assistant":
        return None
    content = _content_text(payload.get("content"))
    if not content.strip():
        return None
    return {
        "id": str(payload.get("id") or f"{chat_id}-assistant-{index}"),
        "chatId": chat_id,
        "role": "assistant",
        "content": content,
        "runId": None,
        "createdAt": created_at,
        "kind": _assistant_message_kind(payload),
    }


def _assistant_message_kind(payload: dict[str, Any]) -> str:
    return "work" if payload.get("phase") == "commentary" else "conclusion"


def _transcript_function_call_message(
    chat_id: str,
    payload: dict[str, Any],
    index: int,
    created_at: str,
    pending_calls: dict[str, dict[str, Any]],
) -> dict | None:
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    call = pending_calls.pop(call_id, {})
    name = call.get("name")
    output = payload.get("output")
    summary, details = _function_call_summary_and_details(
        str(name or "tool"),
        call.get("arguments") if isinstance(call, dict) else None,
        output if isinstance(output, str) else "",
    )
    if not summary:
        return None
    return {
        "id": str(payload.get("id") or f"{chat_id}-tool-{index}-{call_id}"),
        "chatId": chat_id,
        "role": "status",
        "content": summary,
        "runId": None,
        "createdAt": created_at,
        "kind": "status",
        "activityDetails": details,
    }


def _function_call_summary_and_details(name: str, arguments: Any, output: str) -> tuple[str, str]:
    if name == "exec_command":
        command = ""
        workdir = ""
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                command = str(parsed.get("cmd") or "")
                workdir = str(parsed.get("workdir") or "")
        summary = f"コマンドを実行しました: {_short_text(command)}" if command else "コマンドを実行しました"
        detail_lines = []
        if workdir:
            detail_lines.append(f"$ cd {workdir}")
        if command:
            detail_lines.append(f"$ {command}")
        if output:
            detail_lines.extend(["", output.rstrip()])
        return summary, "\n".join(detail_lines).strip()
    summary = f"ツールを実行しました: {_short_text(name)}" if name else "ツールを実行しました"
    detail_lines = []
    formatted_arguments = _format_tool_value(arguments)
    if formatted_arguments:
        detail_lines.extend(["引数:", formatted_arguments])
    if output:
        if detail_lines:
            detail_lines.append("")
        detail_lines.extend(["結果:", output.rstrip()])
    return summary, "\n".join(detail_lines).strip()


def _reasoning_summary_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
    return "\n\n".join(parts)


def _format_tool_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
        value = parsed
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value).strip()


def _short_text(value: str, limit: int = 96) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 1]}..."


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for part in value:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _extract_user_message_attachments(content: str) -> tuple[str, list[dict[str, str]]]:
    lines = content.splitlines()
    first_content_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_content_index is None:
        return content, []
    header = lines[first_content_index].strip().lstrip("#").strip()
    if header != "Files mentioned by the user:":
        return _strip_unreadable_image_notices(content).strip(), []

    attachments: list[dict[str, str]] = []
    index = first_content_index + 1
    while index < len(lines):
        line = lines[index].strip()
        if line.lstrip("#").strip() == "My request for Codex:":
            index += 1
            break
        if line.endswith(":"):
            name = line[:-1].strip()
            index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines):
                raw_path = lines[index].strip()
                if _looks_like_attachment_path(raw_path):
                    attachments.append(_attachment_from_path(name, raw_path))
        index += 1

    if index >= len(lines):
        clean_content = ""
    else:
        clean_content = "\n".join(lines[index:])
    clean_content = _strip_unreadable_image_notices(clean_content).strip()
    return clean_content, attachments


def _looks_like_attachment_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        (len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/")
        or normalized.startswith("/mnt/")
        or normalized.startswith("//wsl.localhost/")
        or normalized.startswith("//wsl$/")
    )


def _attachment_from_path(name: str, raw_path: str) -> dict[str, str]:
    wsl_path = _windows_path_to_wsl(raw_path)
    attachment_name = name or Path(wsl_path).name or "attachment"
    kind = "image" if _looks_like_image_path(raw_path) or _looks_like_image_path(wsl_path) else "file"
    return {
        "path": wsl_path,
        "name": attachment_name,
        "kind": kind,
        "uri": _attachment_uri(raw_path, wsl_path),
    }


def _looks_like_image_path(value: str) -> bool:
    return Path(value.replace("\\", "/")).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _attachment_uri(raw_path: str, wsl_path: str) -> str:
    windows_path = _wsl_mnt_path_to_windows(wsl_path) or raw_path
    normalized = windows_path.replace("\\", "/")
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "/":
        return "file:///" + quote(normalized, safe="/:")
    if normalized.startswith("//"):
        return "file:" + quote(normalized, safe="/:")
    return ""


def _wsl_mnt_path_to_windows(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/mnt/") and len(normalized) >= 8 and normalized[6] == "/":
        drive = normalized[5].upper()
        return f"{drive}:/{normalized[7:]}"
    return None


def _strip_unreadable_image_notices(value: str) -> str:
    return re.sub(r"Codex could not read the local image at `[^`]+`:.*?(?=Codex could not read the local image at `|$)", "", value, flags=re.DOTALL).strip()


def _tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if offset > 0 and lines:
        return lines[1:]
    return lines


def _clean_title(value: str | None) -> str:
    if not value:
        return ""
    text = " ".join(value.split())
    if len(text) > 80:
        return text[:77].rstrip() + "..."
    return text


def _windows_path_to_wsl(value: str) -> str:
    if len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"} and value[0].isalpha():
        drive = value[0].lower()
        rest = value[3:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return value


def _path_key(path: Path) -> str:
    text = str(path)
    if text.startswith("/mnt/") and len(text) > 6 and text[6] == "/":
        return text.lower()
    return text
