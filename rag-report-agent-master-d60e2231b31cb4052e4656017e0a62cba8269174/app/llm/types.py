from typing import Any, Literal, TypedDict


class Message(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class Usage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResult(TypedDict):
    text: str
    usage: Usage
    raw: Any


class StreamDelta(TypedDict, total=False):
    delta: str
    usage: Usage | None
