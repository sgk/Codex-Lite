from __future__ import annotations

import pytest

from codex_lite_daemon.app_services import _provider_context_prompt
from codex_lite_daemon.provider_context import PROVIDER_CONTEXT_PREFIX, visible_user_request
from codex_lite_daemon.transcript_import import _transcript_message
from test_history_tail import history


def test_provider_envelope_restores_only_current_user_request():
    text = _provider_context_prompt("User: previous instruction", "current instruction")
    assert visible_user_request(text) == "current instruction"
    quoted = PROVIDER_CONTEXT_PREFIX + "quoted text"
    assert visible_user_request(quoted) == quoted
    message = _transcript_message("chat", {"type": "event_msg", "payload": {
        "type": "user_message", "message": text}}, 1)
    assert message["content"] == "current instruction"


def test_confirmed_archival_never_hides_unmentioned_chat(history):
    service, _, messages, db, project_id, _, _ = history
    messages.insert_message("thread-tail", "user", "keep", kind="instruction")
    assert service.chats.archive_confirmed_imported_chats(project_id, {"different-thread"}) == 0
    assert service.chats.archive_confirmed_imported_chats(project_id, set()) == 0
    assert service.chats.get_chat_row(project_id, "thread-tail")["archived_at"] is None
    assert service.chats.archive_confirmed_imported_chats(project_id, {"thread-tail"}) == 1
    assert service.chats.get_chat_row(project_id, "thread-tail")["archived_at"] is not None


@pytest.mark.asyncio
async def test_accepted_attachment_message_is_not_its_own_provider_context(history):
    service, _, messages, _, project_id, _, _ = history
    service.chats.upsert_provider_thread(project_id, "thread-tail", "openai", "thread-tail", history_initialized=False)
    accepted = messages.insert_message("thread-tail", "user", "inspect\n\n添付: image.png", kind="instruction")
    assert await service.context_for_provider(project_id, "thread-tail", "openai",
        exclude_content="inspect", exclude_message_id=accepted["id"]) == ""
