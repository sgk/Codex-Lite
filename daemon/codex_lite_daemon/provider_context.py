PROVIDER_CONTEXT_PREFIX = "以下はCodex Liteの同じチャットで、別のモデル提供元が応答した過去の表示内容です。"


def visible_user_request(value: str) -> str:
    """Remove our exact transport envelope, not arbitrary quoted user text."""
    if not value.startswith(PROVIDER_CONTEXT_PREFIX):
        return value
    boundary = "--- 過去の会話ここまで ---\n\n--- 今回の依頼 ---\n"
    if "\n\n--- 過去の会話 ---\n" not in value or boundary not in value:
        return value
    return value.split(boundary, 1)[1]
