from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .automation_service import AutomationService
from .errors import AppError
from .services import ChatService


SET_TITLE_TOOL = "codex_lite_set_thread_title"
LIST_AUTOMATIONS_TOOL = "codex_lite_list_automations"
CREATE_AUTOMATION_TOOL = "codex_lite_create_automation"
UPDATE_AUTOMATION_TOOL = "codex_lite_update_automation"
DELETE_AUTOMATION_TOOL = "codex_lite_delete_automation"


@dataclass(frozen=True)
class DynamicToolExecution:
    response: dict[str, Any]
    summary: str


class CodexLiteDynamicTools:
    def __init__(self, chats: ChatService, automations: AutomationService) -> None:
        self.chats = chats
        self.automations = automations
        self._change_listener: Callable[[str], None] | None = None

    def set_change_listener(self, listener: Callable[[str], None] | None) -> None:
        self._change_listener = listener

    def _notify_changed(self, chat_id: str) -> None:
        if self._change_listener is not None:
            self._change_listener(chat_id)

    @property
    def specs(self) -> list[dict[str, Any]]:
        schedule_properties = {
            "schedule_kind": {
                "type": "string",
                "enum": ["interval_minutes", "hourly_minute", "daily_time"],
                "description": "interval_minutes=指定分ごと、hourly_minute=毎時の指定分、daily_time=毎日のローカル時刻。",
            },
            "schedule_value": {
                "type": "integer",
                "description": "interval_minutesは間隔の分数、hourly_minuteは0〜59、daily_timeは午前0時からの分数（例: 09:30は570）。",
            },
        }
        return [
            _function_tool(
                SET_TITLE_TOOL,
                "新規スレッドの最初の指示を要約した、簡潔で具体的な日本語タイトルを一度だけ設定します。手入力済みタイトルは変更しません。",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"title": {"type": "string", "minLength": 1, "maxLength": 80}},
                    "required": ["title"],
                },
            ),
            _function_tool(
                LIST_AUTOMATIONS_TOOL,
                "現在のスレッドに保存されているCodex Liteオートメーションを一覧します。変更や削除の前にIDを確認するために使います。",
                {"type": "object", "additionalProperties": False, "properties": {}},
            ),
            _function_tool(
                CREATE_AUTOMATION_TOOL,
                "利用者が依頼した定期実行を、現在のスレッドにCodex Liteオートメーションとして作成します。時刻は利用者のローカル時刻として指定します。",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 200},
                        "prompt": {"type": "string", "minLength": 1},
                        **schedule_properties,
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name", "prompt", "schedule_kind", "schedule_value", "enabled"],
                },
            ),
            _function_tool(
                UPDATE_AUTOMATION_TOOL,
                "利用者が明示的に依頼した場合に、現在のスレッドの既存オートメーションを変更します。先に一覧ツールでIDを確認します。",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "automation_id": {"type": "string", "minLength": 1},
                        "name": {"type": "string", "minLength": 1, "maxLength": 200},
                        "prompt": {"type": "string", "minLength": 1},
                        **schedule_properties,
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["automation_id"],
                },
            ),
            _function_tool(
                DELETE_AUTOMATION_TOOL,
                "利用者が削除を明示的に依頼した場合だけ、現在のスレッドのオートメーションを削除します。先に一覧ツールでIDを確認します。",
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"automation_id": {"type": "string", "minLength": 1}},
                    "required": ["automation_id"],
                },
            ),
        ]

    @property
    def developer_instructions(self) -> str:
        return (
            "Codex Lite固有機能: 新規スレッドの最初の利用者指示を理解したら、最終回答より前に "
            f"{SET_TITLE_TOOL} を一度呼び、指示文の丸写しではない簡潔なタイトルを付ける。"
            "利用者がオートメーションの作成・確認・変更・削除を依頼した場合は、説明だけで済ませず、"
            "対応する codex_lite_*_automation ツールを呼んで実際に反映する。変更・削除前は一覧でIDを確認し、"
            "依頼されていないオートメーション操作は行わない。"
        )

    async def execute(
        self,
        tool: str,
        arguments: Any,
        chat_id: str,
        thread_id: str,
        app_server: Any,
    ) -> DynamicToolExecution:
        try:
            args = _object_arguments(arguments)
            chat = self.chats.get_chat_row_by_id(chat_id)
            project_id = str(chat["project_id"])
            if tool == SET_TITLE_TOOL:
                title = _required_string(args, "title", 80)
                updated = self.chats.set_automatic_title(project_id, chat_id, title)
                if not updated:
                    return _success({"updated": False, "title": chat["title"], "reason": "title_already_set"}, "タイトルは変更しませんでした")
                try:
                    await app_server.request("thread/name/set", {"threadId": thread_id, "name": title})
                except Exception:
                    self.chats.clear_automatic_title(project_id, chat_id, str(chat["title"]))
                    raise
                self._notify_changed(chat_id)
                return _success({"updated": True, "title": title}, f"タイトルを「{title}」に設定しました")
            if tool == LIST_AUTOMATIONS_TOOL:
                items = self.automations.list_automations(project_id, chat_id)
                return _success({"automations": items}, f"オートメーション {len(items)}件を確認しました")
            if tool == CREATE_AUTOMATION_TOOL:
                item = self.automations.create_automation(
                    project_id,
                    chat_id,
                    _required_string(args, "name", 200),
                    _required_string(args, "prompt", 100_000),
                    _required_integer(args, "schedule_value"),
                    _required_boolean(args, "enabled"),
                    _required_string(args, "schedule_kind", 40),
                )
                self._notify_changed(chat_id)
                return _success({"automation": item}, f"オートメーション「{item['name']}」を作成しました")
            if tool == UPDATE_AUTOMATION_TOOL:
                automation_id = _required_string(args, "automation_id", 200)
                update_keys = {"name", "prompt", "schedule_kind", "schedule_value", "enabled"} & args.keys()
                if not update_keys:
                    raise AppError("validation_error", "変更する項目がありません。", 400)
                item = self.automations.update_automation(
                    project_id,
                    chat_id,
                    automation_id,
                    name=_optional_string(args, "name", 200),
                    prompt=_optional_string(args, "prompt", 100_000),
                    interval_minutes=_optional_integer(args, "schedule_value"),
                    enabled=_optional_boolean(args, "enabled"),
                    schedule_kind=_optional_string(args, "schedule_kind", 40),
                )
                self._notify_changed(chat_id)
                return _success({"automation": item}, f"オートメーション「{item['name']}」を更新しました")
            if tool == DELETE_AUTOMATION_TOOL:
                automation_id = _required_string(args, "automation_id", 200)
                item = self.automations.get_automation(project_id, chat_id, automation_id)
                self.automations.delete_automation(project_id, chat_id, automation_id)
                self._notify_changed(chat_id)
                return _success({"deleted": True, "automationId": automation_id}, f"オートメーション「{item['name']}」を削除しました")
            raise AppError("dynamic_tool_not_found", f"未対応のCodex Liteツールです: {tool}", 400)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            return DynamicToolExecution(
                {"success": False, "contentItems": [{"type": "inputText", "text": message}]},
                f"Codex Liteツールエラー: {message}",
            )


def _function_tool(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": description, "inputSchema": input_schema}


def _success(value: dict[str, Any], summary: str) -> DynamicToolExecution:
    return DynamicToolExecution(
        {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(value, ensure_ascii=False)}]},
        summary,
    )


def _object_arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppError("validation_error", "ツール引数はオブジェクトで指定してください。", 400)
    return value


def _required_string(args: dict[str, Any], key: str, max_length: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise AppError("validation_error", f"{key} が不正です。", 400)
    return value.strip()


def _optional_string(args: dict[str, Any], key: str, max_length: int) -> str | None:
    return _required_string(args, key, max_length) if key in args else None


def _required_integer(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError("validation_error", f"{key} が不正です。", 400)
    return value


def _optional_integer(args: dict[str, Any], key: str) -> int | None:
    return _required_integer(args, key) if key in args else None


def _required_boolean(args: dict[str, Any], key: str) -> bool:
    value = args.get(key)
    if not isinstance(value, bool):
        raise AppError("validation_error", f"{key} が不正です。", 400)
    return value


def _optional_boolean(args: dict[str, Any], key: str) -> bool | None:
    return _required_boolean(args, key) if key in args else None
