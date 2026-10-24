from codex_lite_daemon.config import default_config
from codex_lite_daemon.db import Database
from codex_lite_daemon.services import ChatService, MessageService, ProjectService


def test_unread_marker_changes_only_for_visible_assistant_messages(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    db.migrate()
    try:
        root = tmp_path / "project"
        root.mkdir()
        projects = ProjectService(db, default_config())
        project_id = projects.create_project(str(root))["id"]
        chats = ChatService(db, projects)
        messages = MessageService(db, chats)
        chat_id = chats.create_chat(project_id, "test")["id"]
        marker = lambda: chats.list_chats(project_id)[0]["latestMessageVersion"]
        assert marker() == ""
        messages.insert_message(chat_id, "user", "指示", kind="instruction")
        assert marker() == ""
        first = messages.insert_message(chat_id, "assistant", "作業内容", kind="work", created_at="2026-10-25T01:00:00Z")
        version = marker()
        assert version == "2026-10-25T01:00:00.000Z\n" + first["id"]
        chats.update_chat(project_id, chat_id, "renamed")
        assert marker() == version
        messages.insert_message(chat_id, "assistant", "", kind="conclusion")
        messages.insert_message(chat_id, "assistant", "waiting", kind="waiting")
        assert marker() == version
        messages.insert_message(chat_id, "assistant", "過去履歴", kind="conclusion", created_at="2026-10-24T01:00:00Z")
        assert marker() == version
        messages.insert_message(chat_id, "assistant", "同じ秒内の作業", kind="work", created_at="2026-10-25T01:00:00.003Z")
        assert marker() > version
        version = marker()
        messages.insert_message(chat_id, "assistant", "結論", kind="conclusion", created_at="2026-10-25T01:01:00Z")
        assert marker() > version
        assert chats.get_chat(project_id, chat_id)["latestMessageVersion"] == marker()
    finally:
        db.close()
