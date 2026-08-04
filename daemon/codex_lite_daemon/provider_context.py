PROVIDER_CONTEXT_PREFIX = "以下はCodex Liteの同じチャットで、別のモデル提供元が応答した過去の表示内容です。"


def is_provider_context_title(value: object) -> bool:
    return isinstance(value, str) and value.strip().startswith(PROVIDER_CONTEXT_PREFIX)
