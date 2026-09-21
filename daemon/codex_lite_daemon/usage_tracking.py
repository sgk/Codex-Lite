from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database
from .util.time import utc_now


_TOKEN_FIELDS = (
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
    "totalTokens",
)


class UsageTrackingService:
    """Persist observed per-turn usage and aggregate it into account windows."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record_thread_usage(
        self,
        *,
        run_id: str,
        chat_id: str,
        provider: str,
        thread_id: str,
        turn_id: str,
        token_usage: Any,
        recorded_at: str | None = None,
    ) -> bool:
        if not isinstance(token_usage, dict):
            return False
        last = token_usage.get("last")
        total = token_usage.get("total")
        if not isinstance(last, dict) or not isinstance(total, dict):
            return False
        last_values = {field: _token_count(last.get(field)) for field in _TOKEN_FIELDS}
        total_values = {field: _token_count(total.get(field)) for field in _TOKEN_FIELDS}
        signature = json.dumps(total_values, sort_keys=True, separators=(",", ":"))
        context_window = token_usage.get("modelContextWindow")
        context_window_value = _token_count(context_window) if isinstance(context_window, int) else None
        cursor = self.db.execute(
            """
            INSERT OR IGNORE INTO token_usage_samples(
              run_id, chat_id, provider, thread_id, turn_id, recorded_at,
              input_tokens, cached_input_tokens, cache_write_input_tokens,
              output_tokens, reasoning_output_tokens, total_tokens,
              thread_total_signature, model_context_window
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                chat_id,
                provider,
                thread_id,
                turn_id,
                recorded_at or utc_now(),
                last_values["inputTokens"],
                last_values["cachedInputTokens"],
                last_values["cacheWriteInputTokens"],
                last_values["outputTokens"],
                last_values["reasoningOutputTokens"],
                last_values["totalTokens"],
                signature,
                context_window_value,
            ),
        )
        return cursor.rowcount > 0

    def record_capacity(self, provider: str, capacity: dict[str, Any]) -> None:
        fetched_at = str(capacity.get("fetchedAt") or utc_now())
        for key, kind in (("fiveHour", "five_hour"), ("weekly", "weekly")):
            window = capacity.get(key)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowMinutes")
            resets_at = window.get("resetsAt")
            if not isinstance(minutes, int) or minutes <= 0 or not isinstance(resets_at, str):
                continue
            reset_time = _parse_time(resets_at)
            if reset_time is None:
                continue
            start_at = _format_time(reset_time - timedelta(minutes=minutes))
            used_percent = window.get("usedPercent")
            self.db.execute(
                """
                INSERT INTO usage_windows(provider, window_kind, window_minutes, start_at, resets_at, used_percent, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, window_kind) DO UPDATE SET
                  window_minutes = excluded.window_minutes,
                  start_at = excluded.start_at,
                  resets_at = excluded.resets_at,
                  used_percent = excluded.used_percent,
                  fetched_at = excluded.fetched_at
                """,
                (
                    provider,
                    kind,
                    minutes,
                    start_at,
                    _format_time(reset_time),
                    float(used_percent) if isinstance(used_percent, (int, float)) else None,
                    fetched_at,
                ),
            )

    def breakdown(self, provider: str, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        captured = self.db.fetchone(
            "SELECT MIN(recorded_at) AS captured_since FROM token_usage_samples WHERE provider = ?",
            (provider,),
        )
        result: dict[str, Any] = {
            "capturedSince": captured.get("captured_since") if captured else None,
            "fiveHour": None,
            "weekly": None,
        }
        for kind, key in (("five_hour", "fiveHour"), ("weekly", "weekly")):
            window = self.db.fetchone(
                "SELECT * FROM usage_windows WHERE provider = ? AND window_kind = ?",
                (provider, kind),
            )
            if window is None:
                continue
            reset_time = _parse_time(str(window["resets_at"]))
            if reset_time is None or current_time >= reset_time:
                continue
            result[key] = self._aggregate_window(provider, window)
        return result

    def _aggregate_window(self, provider: str, window: dict[str, Any]) -> dict[str, Any]:
        select_metrics = """
          SUM(s.input_tokens) AS input_tokens,
          SUM(s.cached_input_tokens) AS cached_input_tokens,
          SUM(s.cache_write_input_tokens) AS cache_write_input_tokens,
          SUM(s.output_tokens) AS output_tokens,
          SUM(s.reasoning_output_tokens) AS reasoning_output_tokens,
          SUM(s.total_tokens) AS total_tokens
        """
        range_sql = """
          s.provider = ?
          AND julianday(s.recorded_at) >= julianday(?)
          AND julianday(s.recorded_at) < julianday(?)
        """
        params = (provider, window["start_at"], window["resets_at"])
        totals = self.db.fetchone(
            f"SELECT {select_metrics} FROM token_usage_samples s WHERE {range_sql}",
            params,
        ) or {}
        projects = self.db.fetchall(
            f"""
            SELECT p.id AS project_id, p.name AS project_name, {select_metrics}
            FROM token_usage_samples s
            JOIN chats c ON c.id = s.chat_id
            JOIN projects p ON p.id = c.project_id
            WHERE {range_sql}
            GROUP BY p.id, p.name
            ORDER BY total_tokens DESC, p.name COLLATE NOCASE
            """,
            params,
        )
        chats = self.db.fetchall(
            f"""
            SELECT c.id AS chat_id, c.project_id, p.name AS project_name, c.title, {select_metrics}
            FROM token_usage_samples s
            JOIN chats c ON c.id = s.chat_id
            JOIN projects p ON p.id = c.project_id
            WHERE {range_sql}
            GROUP BY c.id, c.project_id, p.name, c.title
            ORDER BY total_tokens DESC, c.title COLLATE NOCASE
            """,
            params,
        )
        return {
            "startAt": window["start_at"],
            "resetsAt": window["resets_at"],
            **_metric_output(totals),
            "projects": [_group_output(row, "project") for row in projects],
            "chats": [_group_output(row, "chat") for row in chats],
        }


def _token_count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _metric_output(row: dict[str, Any]) -> dict[str, int]:
    return {
        "inputTokens": int(row.get("input_tokens") or 0),
        "cachedInputTokens": int(row.get("cached_input_tokens") or 0),
        "cacheWriteInputTokens": int(row.get("cache_write_input_tokens") or 0),
        "outputTokens": int(row.get("output_tokens") or 0),
        "reasoningOutputTokens": int(row.get("reasoning_output_tokens") or 0),
        "totalTokens": int(row.get("total_tokens") or 0),
    }


def _group_output(row: dict[str, Any], kind: str) -> dict[str, Any]:
    identity = (
        {"projectId": row["project_id"], "name": row["project_name"]}
        if kind == "project"
        else {
            "chatId": row["chat_id"],
            "projectId": row["project_id"],
            "projectName": row["project_name"],
            "title": row["title"],
        }
    )
    return identity | _metric_output(row)
