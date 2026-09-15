import re

AGENT_IDENTITY_REPLY = "我是广州日报粤传媒和光明实验室联合研发的智能体，可以回答你有关知识库文档内的问题，还可以帮助你进行报告生成哦。"

IDENTITY_PATTERNS = (
    "你是谁",
    "你是什么",
    "介绍一下你自己",
    "自我介绍一下",
)

CONNECTORS = (
    "然后",
    "再",
    "并且",
    "还有",
    "另外",
    "顺便",
    "以及",
    "同时",
)

_IDENTITY_RE = re.compile("|".join(re.escape(item) for item in IDENTITY_PATTERNS))
_LEADING_PUNCT_RE = re.compile(r"^[\s？?。！!，,、；;：:]+")
_LEADING_MODAL_RE = re.compile(r"^(?:可以|能不能|能否|能|请|麻烦你|麻烦)[\s，,、]*")


def split_identity_request(query: str) -> tuple[bool, str]:
    text = (query or "").strip()
    match = _IDENTITY_RE.search(text)
    if not match:
        return False, text

    remainder = f"{text[: match.start()]}{text[match.end() :]}".strip()
    for _ in range(3):
        before = remainder
        remainder = _LEADING_PUNCT_RE.sub("", remainder)
        for connector in CONNECTORS:
            if remainder.startswith(connector):
                remainder = remainder[len(connector) :].lstrip(" ，,、")
                break
        remainder = _LEADING_MODAL_RE.sub("", remainder)
        if remainder == before:
            break
    return True, remainder.strip(" \t\r\n，,。？?")


def is_identity_only_question(query: str) -> bool:
    has_identity, remainder = split_identity_request(query)
    return has_identity and not remainder


def is_compound_query(query: str) -> bool:
    text = (query or "").strip()
    if not text:
        return False
    _, remainder = split_identity_request(text)
    if remainder and remainder != text:
        return True
    if any(mark in text for mark in ["；", ";"]):
        return True
    return any(connector in text for connector in CONNECTORS)
