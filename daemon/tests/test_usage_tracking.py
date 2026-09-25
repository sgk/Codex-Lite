from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from codex_lite_daemon.app_services import AppServerUsageService
from codex_lite_daemon.db import Database
from codex_lite_daemon.usage_tracking import UsageTrackingService


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_tracker(tmp_path) -> tuple[Database, UsageTrackingService]:
    db = Database(tmp_path / "usage.sqlite")
    db.migrate()
    now = "2026-09-21T00:00:00.000Z"
    db.execute(
        "INSERT INTO projects(id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("project-a", "Alpha", "/tmp/alpha", now, now),
    )
    db.execute(
        "INSERT INTO projects(id, name, path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("project-b", "Beta", "/tmp/beta", now, now),
    )
    db.execute(
        "INSERT INTO chats(id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("chat-a", "project-a", "First", now, now),
    )
    db.execute(
        "INSERT INTO chats(id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("chat-b", "project-b", "Second", now, now),
    )
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('run-a', 'chat-a', 'succeeded')")
    db.execute("INSERT INTO runs(id, chat_id, status) VALUES ('run-b', 'chat-b', 'succeeded')")
    return db, UsageTrackingService(db)


def usage(last_total: int, thread_total: int, *, input_tokens: int = 0, output_tokens: int = 0) -> dict:
    return {
        "last": {
            "inputTokens": input_tokens,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": 0,
            "totalTokens": last_total,
        },
        "total": {
            "inputTokens": thread_total - output_tokens,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": 0,
            "totalTokens": thread_total,
        },
        "modelContextWindow": 200000,
    }


def test_usage_samples_are_deduplicated_and_grouped_by_current_windows(tmp_path) -> None:
    db, tracker = create_tracker(tmp_path)
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    capacity = {
        "fetchedAt": iso(now),
        "fiveHour": {"windowMinutes": 300, "resetsAt": iso(now + timedelta(hours=2)), "usedPercent": 20},
        "weekly": {"windowMinutes": 10080, "resetsAt": iso(now + timedelta(days=3)), "usedPercent": 40},
    }
    tracker.record_capacity("openai", capacity)
    assert tracker.record_thread_usage(
        run_id="run-a",
        chat_id="chat-a",
        provider="openai",
        thread_id="thread-a",
        turn_id="turn-a",
        token_usage=usage(120, 500, input_tokens=100, output_tokens=20),
        recorded_at=iso(now - timedelta(hours=1)),
    )
    assert not tracker.record_thread_usage(
        run_id="run-a",
        chat_id="chat-a",
        provider="openai",
        thread_id="thread-a",
        turn_id="turn-a",
        token_usage=usage(120, 500, input_tokens=100, output_tokens=20),
        recorded_at=iso(now - timedelta(hours=1)),
    )
    assert tracker.record_thread_usage(
        run_id="run-b",
        chat_id="chat-b",
        provider="openai",
        thread_id="thread-b",
        turn_id="turn-b",
        token_usage=usage(80, 80, input_tokens=60, output_tokens=20),
        recorded_at=iso(now - timedelta(days=2)),
    )

    result = tracker.breakdown("openai", now=now)

    assert result["fiveHour"]["totalTokens"] == 120
    assert result["fiveHour"]["projects"] == [
        {
            "projectId": "project-a",
            "name": "Alpha",
            "inputTokens": 100,
            "cachedInputTokens": 0,
            "cacheWriteInputTokens": 0,
            "outputTokens": 20,
            "reasoningOutputTokens": 0,
            "totalTokens": 120,
        }
    ]
    assert result["weekly"]["totalTokens"] == 200
    assert [item["chatId"] for item in result["weekly"]["chats"]] == ["chat-a", "chat-b"]
    db.close()


def test_new_capacity_window_switches_display_without_deleting_history(tmp_path) -> None:
    db, tracker = create_tracker(tmp_path)
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    tracker.record_capacity(
        "openai",
        {"fetchedAt": iso(now), "fiveHour": {"windowMinutes": 300, "resetsAt": iso(now + timedelta(hours=1))}},
    )
    tracker.record_thread_usage(
        run_id="run-a", chat_id="chat-a", provider="openai", thread_id="thread-a", turn_id="turn-a",
        token_usage=usage(50, 50), recorded_at=iso(now),
    )
    assert tracker.breakdown("openai", now=now)["fiveHour"]["totalTokens"] == 50

    later = now + timedelta(hours=2)
    tracker.record_capacity(
        "openai",
        {"fetchedAt": iso(later), "fiveHour": {"windowMinutes": 300, "resetsAt": iso(later + timedelta(hours=5))}},
    )
    assert tracker.breakdown("openai", now=later)["fiveHour"]["totalTokens"] == 0
    assert db.fetchone("SELECT COUNT(*) AS count FROM token_usage_samples")["count"] == 1
    db.close()


class FakeAppServer:
    async def request(self, method: str) -> dict:
        assert method == "account/rateLimits/read"
        now = datetime.now(timezone.utc)
        return {
            "rateLimits": {
                "primary": {
                    "usedPercent": 10,
                    "windowDurationMins": 300,
                    "resetsAt": int((now + timedelta(hours=4)).timestamp()),
                },
                "secondary": {
                    "usedPercent": 25,
                    "windowDurationMins": 10080,
                    "resetsAt": int((now + timedelta(days=6)).timestamp()),
                },
            }
        }


@pytest.mark.asyncio
async def test_capacity_response_includes_observed_breakdown(tmp_path) -> None:
    db, tracker = create_tracker(tmp_path)
    result = await AppServerUsageService(FakeAppServer(), usage_tracking=tracker).read_capacity()
    assert result["breakdown"]["fiveHour"] is not None
    assert result["breakdown"]["weekly"] is not None
    db.close()
