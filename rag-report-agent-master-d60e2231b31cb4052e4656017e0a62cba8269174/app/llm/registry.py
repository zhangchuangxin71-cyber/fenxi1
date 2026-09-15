from app.llm.base import LLMProvider

_providers: dict[str, LLMProvider] = {}


def register(name: str, provider: LLMProvider):
    _providers[name] = provider


def clear_providers():
    _providers.clear()


def get_provider(name: str = "doubao") -> LLMProvider:
    if name not in _providers:
        init_providers()
    return _providers[name]


def init_providers():
    if "doubao" in _providers:
        return
    from app.llm.doubao import DoubaoProvider

    register("doubao", DoubaoProvider())
