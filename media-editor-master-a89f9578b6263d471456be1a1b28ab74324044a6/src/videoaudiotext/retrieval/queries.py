"""将口播文案改写为适合 Chinese-CLIP 检索的中文视觉 query。"""

from __future__ import annotations

import re
from typing import List, Literal, Sequence, Tuple

from videoaudiotext.text.segment_meta import SegmentVisualMeta

QueryWeight = Tuple[str, float]

VisualQueryStrategy = Literal[
    "normal", "inherit_prev", "forward_scan", "topic_fallback"
]

RETRIEVAL_TOPIC_CHOICES: tuple[str, ...] = (
    "自动",
    "通用",
    "美食",
    "旅行",
    "自然",
    "城市",
)

_NEUTRAL_TOPIC_FALLBACK = "城市 日常 氛围"

_TOPIC_KEYWORD_MAP: dict[str, str] = {
    "通用": _NEUTRAL_TOPIC_FALLBACK,
    "城市": _NEUTRAL_TOPIC_FALLBACK,
    "美食": "美食 餐饮 摊位 菜品",
    "旅行": "旅行 风景 街道 地标",
    "自然": "自然 山水 风景 天空",
}

# 抽象/情绪词对 CLIP 画面匹配帮助不大
_ABSTRACT_WORDS = frozenset(
    """
    纠结 内耗 缘分 定数 天意 恩赐 常态 回忆 情绪 关系 全貌 懂得 明白 慢慢 渐渐
    后来 从前 曾经 总是 一直 终于 开始 已经 将会 可以 能够 如果 因为 所以 但是
    而且 或者 并不 不再 不再 其实 真的 非常 十分 有些 某种 一种 这份 那段 这段
    为什么 怎么 如何 什么 谁 哪 哪个 哪些 自己 彼此 互相 双方 双方 双方
    """.split()
)

_STOPWORDS = frozenset(
    """
    的 了 吗 呢 吧 啊 呀 么 在 是 我 你 他 她 它 我们 你们 他们 这 那 这些 那些
    就 都 也 还 又 很 并 与 和 及 而 被 把 给 让 向 从 到 于 以 为 会 能 要 想
    觉得 认为 知道 希望 需要 应该 可能 已经 正在 还是 只是 只有 只要 不过 然而
    """.split()
)

def comma_material_parts(text: str, *, min_parts: int = 2) -> List[str] | None:
    """
    按逗号/顿号/分号切分句子；≥2 段时每段单独检索、单独选素材。
    切分规则与段内 progressive 字幕一致，保证画面切换与字幕同步。
    """
    from videoaudiotext.subtitle.build import split_phrases_for_timing

    parts = [p.strip() for p in split_phrases_for_timing(_normalize(text)) if len(p.strip()) >= 2]
    if len(parts) < min_parts:
        return None
    return parts


def _normalize(text: str) -> str:
    return text.strip().replace("\n", "").replace("\r", "")


def _split_comma_phrases(text: str) -> List[str]:
    parts: List[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "，、；":
            chunk = buf.strip().rstrip("，、；").strip()
            if chunk:
                parts.append(chunk)
            buf = ""
    if buf.strip():
        parts.append(buf.strip().rstrip("。！？；"))
    return parts


def _concrete_nouns(text: str) -> List[str]:
    """具象名词：n* 词性、长度>1，排除群体称呼尾缀「们」（非画面主体）。"""
    nouns: List[str] = []
    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(text):
            if not flag.startswith("n") or len(word) <= 1:
                continue
            if word.endswith("们"):
                continue
            nouns.append(word)
    except ImportError:
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            if not token.endswith("们"):
                nouns.append(token)
    return nouns


def _has_visual_subject(text: str) -> bool:
    """
    是否含可检索的具象画面主体。
    排除 jieba 误标名词的极短感叹（如「太绝了！」→ 伪名词「太绝」）。
    """
    nouns = _concrete_nouns(text)
    if not nouns:
        return False
    verbs: List[str] = []
    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(text):
            if flag.startswith("v") and len(word) > 1:
                verbs.append(word)
    except ImportError:
        pass
    if (
        len(text) <= 10
        and text.endswith(("！", "?", "!", "？"))
        and len(nouns) <= 1
        and not verbs
    ):
        return False
    if text.endswith(("！", "?", "!", "？")) and not verbs:
        if nouns and all(len(n) <= 3 for n in nouns) and len(text) <= 14:
            return False
    if len(nouns) >= 2 or verbs:
        return True
    if len(nouns) == 1 and len(text) <= 14 and text.endswith(("！", "?", "!", "？")):
        return False
    return bool(nouns)


def _resolve_from_llm_meta(
    text: str,
    llm_meta: SegmentVisualMeta | None,
) -> tuple[str, VisualQueryStrategy] | None:
    """LLM 视觉标优先；返回 None 表示应回退规则判定。"""
    if llm_meta is None:
        return None
    if not llm_meta.visual:
        return None
    q = llm_meta.normalized_query()
    if q:
        return q, "normal"
    return _build_concrete_query(text)


def concrete_visual_query(
    text: str,
    *,
    llm_meta: SegmentVisualMeta | None = None,
) -> str | None:
    """仅当句内具象可检索时返回 query，否则 None（供全文预扫）。"""
    text = _normalize(text)
    if not text:
        return None
    from_llm = _resolve_from_llm_meta(text, llm_meta)
    if from_llm is not None:
        q, strategy = from_llm
        return q if strategy == "normal" and q else None
    if not _has_visual_subject(text):
        return None
    q, strategy = _build_concrete_query(text)
    if strategy == "normal" and q:
        return q
    return None


def forward_visual_query(
    sentences: Sequence[str],
    index: int,
    *,
    visual_meta: Sequence[SegmentVisualMeta | None] | None = None,
) -> str | None:
    """从 index 之后找第一个具象句的 query。"""
    for j in range(index + 1, len(sentences)):
        meta = visual_meta[j] if visual_meta is not None and j < len(visual_meta) else None
        q = concrete_visual_query(sentences[j], llm_meta=meta)
        if q:
            return q
    return None


def resolve_retrieval_context_topic(value: str | None) -> str:
    """UI/CLI 传入的主题；「自动」或未填则用配置默认。"""
    from videoaudiotext.config import video_context_topic as default_topic

    topic = (value or "").strip()
    if not topic or topic == "自动":
        return default_topic()
    return topic


def topic_fallback_query(video_context_topic: str | None = None) -> str:
    """纯情绪句兜底检索词：优先用 UI/配置主题，否则中性城市氛围。"""
    topic = resolve_retrieval_context_topic(video_context_topic)
    mapped = _TOPIC_KEYWORD_MAP.get(topic)
    if mapped:
        return mapped
    if topic and topic != "通用":
        return topic
    return _NEUTRAL_TOPIC_FALLBACK


def get_universal_search_query(
    current_text: str,
    *,
    previous_query: str | None = None,
    forward_query: str | None = None,
    video_context_topic: str | None = None,
    llm_meta: SegmentVisualMeta | None = None,
) -> tuple[str, VisualQueryStrategy]:
    """
    二级检索视觉意图转化：无具象名词 → 继承上句 / 全文预扫 / 主题兜底。
    llm_meta.visual=false 时跳过 jieba 规则，直接走二级拦截。
    """
    text = _normalize(current_text)
    if not text:
        return "", "normal"

    if llm_meta is not None and not llm_meta.visual:
        non_visual = True
    elif llm_meta is not None and llm_meta.visual:
        from_llm = _resolve_from_llm_meta(text, llm_meta)
        if from_llm is not None:
            return from_llm
        non_visual = False
    else:
        non_visual = not _has_visual_subject(text)

    if non_visual:
        prev = (previous_query or "").strip()
        if prev:
            return prev, "inherit_prev"
        fwd = (forward_query or "").strip()
        if fwd:
            return fwd, "forward_scan"
        return topic_fallback_query(video_context_topic), "topic_fallback"

    return _build_concrete_query(text)


def _build_concrete_query(text: str) -> tuple[str, VisualQueryStrategy]:
    nouns = _concrete_nouns(text)
    verbs: List[str] = []
    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(text):
            if flag.startswith("v") and len(word) > 1:
                verbs.append(word)
    except ImportError:
        pass

    combined = nouns[:2] + verbs[:1]
    return " ".join(combined), "normal"


def resolve_visual_search_query(
    current_text: str,
    *,
    previous_query: str | None = None,
    forward_query: str | None = None,
    video_context_topic: str | None = None,
    llm_meta: SegmentVisualMeta | None = None,
) -> tuple[str, VisualQueryStrategy]:
    """公开别名，与 get_universal_search_query 同源。"""
    return get_universal_search_query(
        current_text,
        previous_query=previous_query,
        forward_query=forward_query,
        video_context_topic=video_context_topic,
        llm_meta=llm_meta,
    )


def _keyword_query(text: str) -> str:
    words: List[str] = []
    seen: set[str] = set()

    def add(w: str) -> None:
        w = w.strip()
        if len(w) < 2 or w in seen or w in _STOPWORDS or w in _ABSTRACT_WORDS:
            return
        seen.add(w)
        words.append(w)

    try:
        import jieba.posseg as pseg

        for word, flag in pseg.cut(text):
            if flag.startswith("n") or flag in ("v", "vn", "a"):
                add(word)
    except ImportError:
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            add(token)

    if not words:
        return ""
    return " ".join(words[:8])


def build_retrieval_queries(
    sentence: str,
    *,
    previous_query: str | None = None,
    forward_query: str | None = None,
    video_context_topic: str | None = None,
    llm_meta: SegmentVisualMeta | None = None,
) -> List[QueryWeight]:
    """
    生成多路中文检索 query，权重越高越优先。
    纯情绪/抽象句走二级拦截（继承上句 / 全文预扫 / 主题兜底）。
    """
    text = _normalize(sentence)
    if not text:
        return []

    primary, strategy = get_universal_search_query(
        text,
        previous_query=previous_query,
        forward_query=forward_query,
        video_context_topic=video_context_topic,
        llm_meta=llm_meta,
    )
    if strategy != "normal" and primary:
        return [(primary, 1.0)]

    out: List[QueryWeight] = []
    seen: set[str] = set()

    def add(q: str, weight: float) -> None:
        q = q.strip().strip("，、；。！？.!?;")
        if len(q) < 2:
            return
        key = q.lower()
        if key in seen:
            return
        seen.add(key)
        out.append((q, weight))

    phrases = _split_comma_phrases(text)
    if len(phrases) > 1:
        for phrase in phrases:
            if len(phrase) >= 3:
                add(phrase, 1.0)
        if phrases:
            add(" ".join(phrases[:3]), 0.92)
    elif len(phrases) == 1 and phrases[0] != text:
        add(phrases[0], 0.95)

    kw = _keyword_query(text)
    if kw:
        add(kw, 0.93)

    if len(text) <= 24:
        add(text, 0.88)
    else:
        add(text, 0.72)

    out.sort(key=lambda item: -item[1])
    return out
