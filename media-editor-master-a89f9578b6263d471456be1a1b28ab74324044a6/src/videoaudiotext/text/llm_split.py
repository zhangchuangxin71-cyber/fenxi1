"""Semantic copy split via OpenAI-compatible chat completion API."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any, List, Sequence

from videoaudiotext.text.segment_meta import SegmentVisualMeta
from videoaudiotext.config import (
    LLM_API_BASE,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_TIMEOUT_SEC,
    llm_supports_json_mode,
    llm_thinking_payload,
)

# 内联切分标记：[V:词1,词2] 有画面；[NV] 无具体画面（后处理会并入相邻 [V:] 段）
_MARKER_RE = re.compile(r"(\[V:[^\]]*\]|\[NV\])")

SPLIT_SYSTEM_PROMPT = """你是短视频口播分镜助手，将文案拆分为「画面单元」。规则适用于各类口播题材（街景、风景、人物、产品、纪实、美食、知识口播等），不因题材改变判定标准。

【任务定义】
每个画面单元对应一次素材检索、一个主镜头；单元内可有口播停顿，但不因此增加单元数。
切分单位是「镜头/画面」，不是「逗号」——逗号仅是换气点，不等于换镜头。
纯情绪、感叹、抽象话术等无具体画面内容用 [NV] 标注，后处理会并入相邻有画面段。

【最高优先级 · 输出格式：内联标记法】
你必须一字不差地原样输出用户文案，绝对不能增删改任何汉字或标点。
不要 Markdown，不要解释，不要注释。只在每个镜头单元口播文本结束之后、下一段开始之前，插入标记：

- 有明确可拍画面：在该单元末尾插入 [V:关键词1,关键词2,关键词3]（2~3 个中文视觉检索词，逗号分隔；禁止在词内使用逗号或 ]）。
- 无具体画面（纯情绪/抽象/口播互动）：在该单元末尾插入 [NV]。

标记紧跟单元文本之后，禁止在句首或句中随意插入。全文最后一个单元也必须在末尾带上 [V:...] 或 [NV]。

示例输入：家人们谁懂啊，今天真的是累到不行。不过烤肉还是得吃对吧！
示例输出：家人们谁懂啊，[NV]今天真的是累到不行。[V:疲惫,打工人,下班]不过烤肉还是得吃对吧！[V:烤肉,美食,餐厅]

【强制红线（违反即错误）】
1. 保真边界：只允许在原文字符之间切分；不要润色、总结、扩写、删减核心词汇或改变语义。仅可在不影响拼接语义时微调段尾标点。
2. 语义完整性：不拆分主谓宾；引号、括号、省略号内内容整体保留，不跨符号切分。
3. 字数原则：常规单段含标点尽量 8~16 字，优先 8~12 字；同一画面连续描述可放宽至 18~22 字。若无自然画面边界，不要为了字数硬切主谓宾或切出残句。
4. 短段处理：不足 5 字的短语、单字/叠用语气词、场景引导词（细看、抬眼望去、放眼望去、走近瞧瞧等）禁止单独成段，必须与前后正文合并。
5. 续写禁止独段：若一句以独立虚词「在、于、而、也、都、仍、又、便」或「随之、渐渐、于是、因此」等承接前句，且不能独立成镜，必须与上一段合并；禁止输出 10~15 字的续写残句单独成片单元。若这些字属于实体词开头，如「都市、都江堰」，不受此限制。
6. 悬空词禁断：禁止把功能词悬空在段尾，例如段尾不能停在「被/把/在/与/和/或/如果/虽然/因为」等连接词、介词之后；若「的」是原文自然句末结构助词，可以保留。
7. 标点原则：优先在原文已有标点处切分；若原文切分点无标点，不要为了格式强行新增标点。除多主体枚举项外，禁止以逗号结尾的未完成语句单独成段。
8. 段数预算：[V:] 标记数量应明显少于全文逗号数，但不得退化为「一句一镜」。粗略下限 ≈ 可独立检索的具象主体数；上限 ≈ 句末标点（。！？；）数量 + 句内多主体枚举项数（逗号分隔且主语/画面中心不同者，各算 1 次切换）。禁止仅因句末有句号就把整句作为一个单元。
9. 并列禁断（窄义）：仅当相邻子句是同一主体的延续或固定对举（如「一边…，一边…」「第一组…，第一组…」）时，禁止切开。若逗号前后引入不同具象主体（主语/画面中心已变），即使句式平行、谓语相同，也必须按主体切开。
10. 同镜头延续：前段以逗号/顿号结尾、后段未引入新画面中心且仍在同一空间/主体链上时，必须合并。后段起首已是新的具象名词主体时，视为新画面，必须切开。

【画面判定与切分】
1. 【同画面单元 · 合并优先于逗号切分】
   逗号/顿号前后若同时满足以下全部，必须合并为一段，禁止切开：
   (a) 视觉主体未变：核心名词或指代仍指向同一对象/同一观察焦点
   (b) 空间未变：仍在同一场所、同一景别层级或同一连续镜头范围内
   (c) 语义延续：后句是前句的动作结果、状态变化、方式或细节补充，而非独立新事件
   典型延续信号（不限题材）：前句动作 + 后句结果/状态；后句承接前句但未引入新画面中心；对同一主体的连续动态、光影、声响、细节描写。

2. 【新画面单元 · 可切分】满足任一即可切开（优先在句号/问号/感叹号/分号，其次在逗号/顿号处）：
   (a) 视觉主体切换：画面中心转向新的对象或新的观察对象
   (b) 空间/景别切换：场所、方位、远近层级明显改变
   (c) 时间跳跃或并列独立画面：出现新场景标记词，或前后分句各自构成可独立成镜的画面

3. 切分优先级：画面主体切换 > 句末标点 > 逗号/顿号枚举边界 > 字数微调。不得因句末有句号就放弃句内已有的多主体切分机会。
4. 长修饰句：切分点前移至修饰短语前，或依托句读/画面边界断开。
5. 【多主体枚举 · 必须切分】一句内若用并列结构列举多个可独立成镜的具象主体（逗号分隔、主语/画面中心不同），每个主体各成一个 unit；引导词（放眼望去/抬眼望去/细看等）并入其后的第一个主体 unit，不单独成段。
6. 口播节奏：单元数量适中；段内口播应自然可念。多主体枚举句的段数应接近枚举项数，而非整句一段。

【检索视觉标定】
[V:] 内填写 Chinese-CLIP 检索词：
- 有具象可拍画面（人物/场所/物品/动作/食物/产品等）用 [V:...]。句末情绪评价或感叹，只要同段有拍摄主体，整段标 [V:] 并留在该单元。
- 纯情绪/感叹/抽象哲理/口播互动：标 [NV]，后处理会并入相邻 [V:] 段。
- 关键词只写可见画面，不写抽象评价词；可补足场景词，不得引入与原文矛盾的主体或地点。

【示范 · 应切开】
① 多主体枚举：往来的行人放慢了脚步，结伴出行的年轻人、饭后散步的街坊穿梭在街巷之中。
→ 往来的行人放慢了脚步，[V:街巷,行人,脚步]结伴出行的年轻人、[V:街巷,年轻人,结伴]饭后散步的街坊[V:街巷,街坊,散步]穿梭在街巷之中。[V:街巷,人群,穿梭]
② 主体/空间切换：湖面波光粼粼随风轻荡，岸边古木掩映着蜿蜒青石小径。
→ 湖面波光粼粼随风轻荡，[V:湖面,波光,微风]岸边古木掩映着蜿蜒青石小径。[V:岸边,古木,青石小径]

【示范 · 应合并】
③ 同画面延续：一轮红日缓缓沉入远山背后，天际被染成橘红与金紫交错的霞光。→ 整句 1 个 [V:]。
④ 引导词并入正文：放眼望去，远处层峦叠嶂笼罩在蒙蒙薄雾之中。→ 整句 1 个 [V:]。
⑤ 情绪感叹并入：炭火上的烤串滋滋冒油，谁能扛得住啊！→ 整句 1 个 [V:]，不切出感叹。
⑥ 固定对举：大家一边欣赏着街景，一边品尝着美味。→ 整句 1 个 [V:]，不在「一边」处切开。

【反例 · 勿学】
⑦ 续写残句独段：抬眼望去，一间间食铺静静伫立，在晨光里勾勒出温柔的轮廓。→ 禁止把「在晨光里…」单独成段，须与上一段合并。
⑧ 多主体整句一段：仓库里摆着木箱，码头停着货轮，路边等着卡车。→ 禁止 1 个 [V:] 覆盖三款主体；应按木箱/货轮/卡车各标 [V:]。
⑨ 同主体 comma 碎切：主体正在完成动作，随后产生结果状态。→ 禁止仅因逗号切开；同一主体/同一动作链默认 1 个 [V:]。

【输出前自检】
① 去掉所有 [V:...] 和 [NV] 后与原文逐字一致；② 无续写残句独段；③ 多主体枚举不得整句 1 个 [V:]；④ [V:] 内关键词非空；⑤ 固定对举不切、不同具象主体必须切。
"""

_MAX_SEG_LEN = 22
_MIN_SEG_LEN = 5
_SOFT_MIN_MERGE_LEN = 8
_SOFT_MAX_LEN = _MAX_SEG_LEN + 10
_REFINE_MAX_LEN = _MAX_SEG_LEN * 2 + 4
_MERGE_COHESION_THRESHOLD = 0.52
_MERGE_COHESION_RELAXED = 0.42
_CONTINUATION_START = frozenset("在于而也都仍又便")
_CONTINUATION_PREFIXES = ("随之", "渐渐", "于是", "因此")
_SCENE_RESET_PREFIXES = (
    "远处",
    "另一",
    "另一边",
    "随后",
    "接着",
    "转而",
    "来到",
    "走进",
)
_SCENE_SHIFT_PREFIXES = ("今天", "今日", "如今", "现在", "当代", "而今")
_CLAUSE_OPENER_SCAN = 6


def _starts_scene_or_time_shift(segment: str) -> bool:
    """段首为场景/时间转折（非「而在…」类续写）→ 不应与上一句末合并。"""
    text = segment.lstrip()
    if not text:
        return False
    if any(text.startswith(p) for p in _SCENE_RESET_PREFIXES):
        return True
    if text.startswith("而在"):
        return False
    if text.startswith("而"):
        if text.startswith("而今"):
            return True
        if len(text) >= 3 and text[1:3] in ("今天", "今日", "如今", "现在"):
            return True
        if len(text) >= 4 and text[1:4] == "今天的":
            return True
    return any(text.startswith(p) for p in _SCENE_SHIFT_PREFIXES)


def _forbidden_merge_boundary(prev: str, curr: str) -> bool:
    """句末标点后接场景/时间转折 → 禁止 merge（如 基因。+ 而今天的佛山）。"""
    if not _hard_sentence_boundary(prev):
        return False
    return _starts_scene_or_time_shift(curr)


def _should_merge_enumeration_run(prev: str, curr: str) -> bool:
    """同句内短顿号并列（美食/品牌碎片）→ 合并，避免 5~8 字孤段。"""
    if _forbidden_merge_boundary(prev, curr):
        return False
    if not prev.rstrip().endswith("、"):
        return False
    if _segment_len(curr) > 12:
        return False
    if _distinct_subject_enumeration_boundary(prev, curr):
        combined = _segment_len(prev) + _segment_len(curr)
        if combined <= _MAX_SEG_LEN + 6 and _segment_len(curr) <= _SOFT_MIN_MERGE_LEN:
            return True
        return False
    combined = _segment_len(prev) + _segment_len(curr)
    return combined <= _MAX_SEG_LEN + 10 and _segment_len(curr) <= _SOFT_MIN_MERGE_LEN + 4


def split_prompt_version_hash() -> str:
    """Prompt 版本指纹，用于 LLM 分句缓存隔离。"""
    return hashlib.sha256(SPLIT_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


_SEMANTIC_STRIP_PUNCT = set(
    "，、；：。！？,.!?; \t\n\r\u3000'\"\"''（）()【】[]《》<>—-…"
)
_SEMANTIC_MIN_RATIO = 0.88
_SEMANTIC_MIN_LEN_RATIO = 0.80
_SEMANTIC_MAX_LEN_RATIO = 1.20


def _semantic_core(text: str) -> str:
    """比对用语义核心：去空白与句读，保留实质用字。"""
    return "".join(ch for ch in text if ch not in _SEMANTIC_STRIP_PUNCT)


def _semantic_similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    a = _semantic_core(left)
    b = _semantic_core(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def segments_semantically_faithful(
    original: str,
    segments: List[str],
    *,
    min_ratio: float = _SEMANTIC_MIN_RATIO,
) -> bool:
    """拼接后与原文语义保真：允许微调语序、标点、少量用字。"""
    joined = "".join(str(s).strip() for s in segments)
    orig = original.strip()
    if not orig:
        return not joined
    if joined == orig:
        return True
    if _collapse_ws(joined) == _collapse_ws(orig):
        return True

    ratio = _semantic_similarity(joined, orig)
    if ratio < min_ratio:
        return False

    core_orig = len(_semantic_core(orig))
    core_join = len(_semantic_core(joined))
    if core_orig == 0:
        return core_join == 0
    len_ratio = core_join / core_orig
    return _SEMANTIC_MIN_LEN_RATIO <= len_ratio <= _SEMANTIC_MAX_LEN_RATIO


def _segment_len(segment: str) -> int:
    return len(segment.strip())


def _starts_continuation(segment: str) -> bool:
    text = segment.strip()
    if not text:
        return False
    if _starts_scene_or_time_shift(text):
        return False
    if text.startswith("而在"):
        return True
    if text[0] in _CONTINUATION_START:
        return True
    return any(text.startswith(prefix) for prefix in _CONTINUATION_PREFIXES)


def _text_concrete_nouns(text: str) -> set[str]:
    from videoaudiotext.retrieval.queries import _concrete_nouns

    return set(_concrete_nouns(text.strip()))


def _incomplete_clause_tail(text: str) -> bool:
    t = text.rstrip()
    return t.endswith("，") or t.endswith("、")


def _hard_sentence_boundary(text: str) -> bool:
    t = text.rstrip()
    return bool(t) and t[-1] in "。！？；"


def _last_comma_clause(text: str) -> str:
    parts = [p.strip() for p in text.rstrip("，、").split("，") if p.strip()]
    return parts[-1] if parts else text.strip()


def _first_comma_clause(text: str) -> str:
    parts = [p.strip() for p in text.lstrip().split("，") if p.strip()]
    return parts[0] if parts else text.strip()


def _is_anaphoric_reprise(left_clause: str, right_clause: str) -> bool:
    """同一指代/对举的延续（第一组…第一组…、一边…一边…）→ 同镜合并，非多主体枚举。"""
    if right_clause.startswith("一边") and "一边" in left_clause:
        return True
    for width in (4, 3):
        if len(left_clause) < width or len(right_clause) < width:
            continue
        prefix = left_clause[:width]
        if left_clause.startswith(prefix) and right_clause.startswith(prefix):
            if prefix[0] in "第这那各":
                return True
    return False


def _shared_predicate_different_subjects(left_clause: str, right_clause: str) -> bool:
    """两子句共享谓语片段、但前部主体不同（不依赖动词词表）。"""
    if len(left_clause) < 4 or len(right_clause) < 4:
        return False
    for length in range(min(5, len(left_clause), len(right_clause)), 1, -1):
        for i in range(len(left_clause) - length + 1):
            chunk = left_clause[i : i + length]
            j = right_clause.find(chunk)
            if j < 0:
                continue
            left_before = left_clause[:i].strip()
            right_before = right_clause[:j].strip()
            if left_before and right_before and left_before != right_before:
                return True
    return False


def _clauses_are_distinct_subjects(left_clause: str, right_clause: str) -> bool:
    """相邻逗号子句是否为可独立检索的不同具象主体。"""
    if len(left_clause) < 4 or len(right_clause) < 4:
        return False
    if _is_anaphoric_reprise(left_clause, right_clause):
        return False
    if _shared_predicate_different_subjects(left_clause, right_clause):
        return True
    pn = _text_concrete_nouns(left_clause)
    cn = _text_concrete_nouns(right_clause)
    if cn and (not pn or len(cn - pn) >= 1) and left_clause[:2] != right_clause[:2]:
        return True
    if (
        left_clause[:2] == right_clause[:2]
        and left_clause != right_clause
        and len(left_clause) >= 5
        and left_clause[0] not in "第这那各"
    ):
        return True
    return False


def _distinct_subject_enumeration_boundary(left: str, right: str) -> bool:
    """逗号边界两侧为不同具象主体 → 保留切分，不当作应合并的并列。"""
    if not _incomplete_clause_tail(left):
        return False
    last = _last_comma_clause(left)
    curr = _first_comma_clause(right)
    return _clauses_are_distinct_subjects(last, curr)


def _repeated_clause_opener_at_boundary(left: str, right: str) -> bool:
    """
    固定对举并列（一边…一边…、第一组…第一组…）→ 不应切开。
    多主体枚举（车流奔赴/行人奔赴、容纳相逢/容纳别离）不在此列。
    """
    if _distinct_subject_enumeration_boundary(left, right):
        return False

    right_s = right.lstrip()
    if not _incomplete_clause_tail(left) or len(right_s) < 2:
        return False

    last_clause = _last_comma_clause(left)
    if not last_clause:
        return False

    if right_s.startswith("一边") and "一边" in last_clause:
        return True

    head = last_clause[:_CLAUSE_OPENER_SCAN]
    for width in (4, 3, 2):
        if len(right_s) < width:
            continue
        prefix = right_s[:width]
        if right_s.startswith(prefix) and head.startswith(prefix):
            return True
    return False


def parallel_clause_boundary_broken(segments: Sequence[str]) -> bool:
    """相邻段是否在并列/重复起句结构处被错误切开。"""
    for i in range(1, len(segments)):
        if _repeated_clause_opener_at_boundary(segments[i - 1], segments[i]):
            return True
    return False


def _noun_overlap_ratio(prev: str, curr: str) -> float:
    cn = _text_concrete_nouns(curr)
    if not cn:
        return 0.65 if _incomplete_clause_tail(prev) else 0.0
    pn = _text_concrete_nouns(prev)
    if not pn:
        return 0.0
    return len(pn & cn) / len(cn)


def _count_enumerable_subject_clauses(text: str) -> int:
    """句内多主体逗号枚举项数（用于放宽 LLM 段数预算）。"""
    extra = 0
    for sentence in re.split(r"[。！？；]", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        clauses = [c.strip() for c in re.split(r"[，、]", sentence) if c.strip()]
        for i in range(1, len(clauses)):
            if _clauses_are_distinct_subjects(clauses[i - 1], clauses[i]):
                extra += 1
    return extra


def _estimate_split_budget(text: str) -> tuple[int, int, int]:
    """(comma_count, sentence_end_count, suggested_max_units) — 供 LLM 与后处理共用。"""
    comma_count = text.count("，") + text.count("、")
    sentence_ends = len(re.findall(r"[。！？；]", text))
    enumerable = _count_enumerable_subject_clauses(text)
    budget = max(
        sentence_ends + enumerable,
        sentence_ends + max(1, comma_count // 3),
        sentence_ends + 1,
    )
    return comma_count, sentence_ends, budget


def _dynamic_merge_limit(
    prev: str,
    curr: str,
    *,
    segment_count: int,
    comma_count: int,
) -> int:
    combined = _segment_len(prev) + _segment_len(curr)
    if segment_count > comma_count // 2 + 1:
        return max(_MAX_SEG_LEN + 24, min(combined, 48))
    return max(_SOFT_MAX_LEN, min(combined, _MAX_SEG_LEN + 16))


def _merge_cohesion(
    prev: str,
    curr: str,
    prev_meta: SegmentVisualMeta | None,
    curr_meta: SegmentVisualMeta | None,
) -> float:
    """同镜头合并得分：越高越应合并。题材无关，仅看标点/名词/检索词/并列结构。"""
    score = 0.0
    if _incomplete_clause_tail(prev):
        score += 0.36
    if _hard_sentence_boundary(prev):
        score -= 0.38
    q_overlap = _query_overlap_count(prev_meta, curr_meta)
    score += min(0.28, q_overlap * 0.12)
    if _incomplete_clause_tail(prev) and q_overlap >= 1:
        score += 0.14
    score += 0.22 * _noun_overlap_ratio(prev, curr)
    if _incomplete_clause_tail(prev):
        score += 0.08 * _semantic_similarity(prev, curr)
    if _repeated_clause_opener_at_boundary(prev, curr):
        score += 0.36
    if _distinct_subject_enumeration_boundary(prev, curr):
        score -= 0.45
    if _starts_continuation(curr):
        score += 0.18
    curr_text = curr.lstrip()
    if _starts_scene_or_time_shift(curr_text):
        score -= 0.55
    elif any(curr_text.startswith(p) for p in _SCENE_RESET_PREFIXES):
        score -= 0.42
    if prev.rstrip().endswith("、"):
        score -= 0.28
    pn = _text_concrete_nouns(prev)
    cn = _text_concrete_nouns(curr)
    if cn and pn and len(cn - pn) >= 2 and _hard_sentence_boundary(prev):
        score -= 0.2
    return score


def _should_merge_adjacent_fragments(
    prev: str,
    curr: str,
    prev_meta: SegmentVisualMeta | None,
    curr_meta: SegmentVisualMeta | None,
    *,
    segment_count: int,
    comma_count: int,
) -> bool:
    if _forbidden_merge_boundary(prev, curr):
        return False
    if _should_merge_enumeration_run(prev, curr):
        return True
    combined = _segment_len(prev) + _segment_len(curr)
    if _repeated_clause_opener_at_boundary(prev, curr):
        return combined <= _MAX_SEG_LEN * 2 + 12
    limit = _dynamic_merge_limit(
        prev, curr, segment_count=segment_count, comma_count=comma_count
    )
    if combined > limit:
        return False
    cohesion = _merge_cohesion(prev, curr, prev_meta, curr_meta)
    threshold = _MERGE_COHESION_THRESHOLD
    if segment_count > comma_count // 2 + 1:
        threshold = _MERGE_COHESION_RELAXED
    return cohesion >= threshold


def _merge_fragmented_adjacent_with_groups(
    segments: List[str],
    groups: List[int],
    meta: List[SegmentVisualMeta | None] | None = None,
    *,
    source_text: str | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    """按画面连贯性得分合并 LLM 的 comma 过度切分。"""
    if not segments:
        return [], [], []
    if len(segments) != len(groups):
        raise ValueError("segments and groups length mismatch")

    joined = source_text if source_text is not None else "".join(segments)
    comma_count, _, _ = _estimate_split_budget(joined)

    meta = _align_visual_meta(segments, meta)
    segs = list(segments)
    grps = list(groups)
    metas = list(meta)

    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(segs):
            if _should_merge_adjacent_fragments(
                segs[i - 1],
                segs[i],
                metas[i - 1],
                metas[i],
                segment_count=len(segs),
                comma_count=comma_count,
            ):
                segs[i - 1] += segs[i]
                grps[i - 1] = min(grps[i - 1], grps[i])
                metas[i - 1] = _combine_visual_meta(metas[i - 1], metas[i])
                segs.pop(i)
                grps.pop(i)
                metas.pop(i)
                changed = True
            else:
                i += 1
    return segs, grps, metas


def _trim_segments_to_budget_with_groups(
    segments: List[str],
    groups: List[int],
    meta: List[SegmentVisualMeta | None] | None = None,
    *,
    source_text: str | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    """段数仍明显高于标点预算时，按连贯性得分继续合并（题材无关）。"""
    if not segments:
        return [], [], []

    joined = source_text if source_text is not None else "".join(segments)
    comma_count, _, budget = _estimate_split_budget(joined)
    target = budget + 1

    meta = _align_visual_meta(segments, meta)
    segs = list(segments)
    grps = list(groups)
    metas = list(meta)

    while len(segs) > target:
        best_idx: int | None = None
        best_score = _MERGE_COHESION_RELAXED - 0.01
        for i in range(1, len(segs)):
            if _forbidden_merge_boundary(segs[i - 1], segs[i]):
                continue
            limit = _dynamic_merge_limit(
                segs[i - 1],
                segs[i],
                segment_count=len(segs),
                comma_count=comma_count,
            )
            if _segment_len(segs[i - 1]) + _segment_len(segs[i]) > limit:
                continue
            score = _merge_cohesion(segs[i - 1], segs[i], metas[i - 1], metas[i])
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx is None:
            break
        i = best_idx
        segs[i - 1] += segs[i]
        grps[i - 1] = min(grps[i - 1], grps[i])
        metas[i - 1] = _combine_visual_meta(metas[i - 1], metas[i])
        segs.pop(i)
        grps.pop(i)
        metas.pop(i)
    return segs, grps, metas


def _parallel_both_side_broken(left: str, right: str) -> bool:
    """兼容旧调用；实质为通用并列边界检测。"""
    return _repeated_clause_opener_at_boundary(left, right)


def _query_tokens(meta: SegmentVisualMeta | None) -> set[str]:
    if meta is None:
        return set()
    q = meta.normalized_query() or ""
    return {w for w in q.split() if len(w) >= 2}


def _query_overlap_count(
    left: SegmentVisualMeta | None, right: SegmentVisualMeta | None
) -> int:
    return len(_query_tokens(left) & _query_tokens(right))


def _merge_continuation_segments(segments: List[str]) -> List[str]:
    if not segments:
        return []
    merged = [segments[0]]
    for segment in segments[1:]:
        if _starts_continuation(segment) and merged:
            merged[-1] += segment
        else:
            merged.append(segment)
    return merged


def _merge_short_segments(segments: List[str], *, min_len: int = _MIN_SEG_LEN) -> List[str]:
    if not segments:
        return []
    merged = [segments[0]]
    for segment in segments[1:]:
        if _segment_len(merged[-1]) < min_len:
            merged[-1] += segment
        elif _segment_len(segment) < min_len:
            merged[-1] += segment
        else:
            merged.append(segment)
    if len(merged) > 1 and _segment_len(merged[-1]) < min_len:
        merged[-2] += merged.pop()
    return merged


def _split_long_segment(segment: str, *, max_len: int = _MAX_SEG_LEN) -> List[str]:
    if _segment_len(segment) <= max_len:
        return [segment]

    best: List[str] | None = None
    for idx, ch in enumerate(segment):
        if ch not in "，、；":
            continue
        left, right = segment[: idx + 1], segment[idx + 1 :]
        if _segment_len(left) < _MIN_SEG_LEN or _segment_len(right) < _MIN_SEG_LEN:
            continue
        if _starts_continuation(right):
            continue
        if _parallel_both_side_broken(left, right):
            continue
        left_parts = (
            _split_long_segment(left, max_len=max_len)
            if _segment_len(left) > max_len
            else [left]
        )
        right_parts = (
            _split_long_segment(right, max_len=max_len)
            if _segment_len(right) > max_len
            else [right]
        )
        parts = left_parts + right_parts
        if len(parts) <= 1:
            continue
        if any(_segment_len(part) > max_len for part in parts):
            continue
        if best is None or len(parts) < len(best):
            best = parts
    if best is not None:
        return best

    if "，" not in segment and "、" not in segment and "；" not in segment:
        from videoaudiotext.text.split import _find_smart_word_boundary_halves

        halves = _find_smart_word_boundary_halves(segment)
        if halves:
            left, right = halves
            left_parts = (
                _split_long_segment(left, max_len=max_len)
                if _segment_len(left) > max_len
                else [left]
            )
            right_parts = (
                _split_long_segment(right, max_len=max_len)
                if _segment_len(right) > max_len
                else [right]
            )
            parts = left_parts + right_parts
            if len(parts) > 1 and all(_segment_len(part) <= max_len for part in parts):
                return parts

    return [segment]


def _align_visual_meta(
    segments: List[str], meta: List[SegmentVisualMeta | None] | None
) -> List[SegmentVisualMeta | None]:
    if not segments:
        return []
    if meta is None or len(meta) != len(segments):
        return [None] * len(segments)
    return list(meta)


def _merge_search_queries(left: str, right: str) -> str:
    words: List[str] = []
    seen: set[str] = set()
    for part in (left, right):
        for word in part.split():
            if word and word not in seen:
                seen.add(word)
                words.append(word)
    return " ".join(words[:5])


def _combine_visual_meta(
    left: SegmentVisualMeta | None,
    right: SegmentVisualMeta | None,
) -> SegmentVisualMeta | None:
    if left is None and right is None:
        return None
    if left is None:
        return right
    if right is None:
        return left
    if not left.visual and not right.visual:
        return SegmentVisualMeta(visual=False, search_query=None)
    if left.visual and not right.visual:
        return left
    if right.visual and not left.visual:
        return right
    lq = left.normalized_query() or ""
    rq = right.normalized_query() or ""
    return SegmentVisualMeta(
        visual=True,
        search_query=_merge_search_queries(lq, rq) or lq or rq,
    )


def _infer_visual_meta_from_text(text: str) -> SegmentVisualMeta | None:
    from videoaudiotext.retrieval.queries import _build_concrete_query, _has_visual_subject

    segment = text.strip()
    if not segment:
        return None
    if not _has_visual_subject(segment):
        return SegmentVisualMeta(visual=False, search_query=None)
    query, _ = _build_concrete_query(segment)
    if not query:
        return SegmentVisualMeta(visual=False, search_query=None)
    return SegmentVisualMeta(visual=True, search_query=query)


def _derive_visual_meta_for_part(
    text: str,
    parent: SegmentVisualMeta | None,
) -> SegmentVisualMeta | None:
    inferred = _infer_visual_meta_from_text(text)
    if parent is None:
        return inferred
    if not parent.visual:
        return SegmentVisualMeta(visual=False, search_query=None)
    if inferred is not None and inferred.visual and inferred.normalized_query():
        return inferred
    parent_query = parent.normalized_query()
    if parent_query:
        return SegmentVisualMeta(visual=True, search_query=parent_query)
    return inferred


def _is_non_visual_meta(meta: SegmentVisualMeta | None) -> bool:
    return meta is not None and not meta.visual


def _promote_lone_non_visual_segment(
    segment: str,
    meta: SegmentVisualMeta | None,
) -> SegmentVisualMeta:
    """全文纯情绪/抽象句：推断检索词，否则主题兜底。"""
    inferred = _infer_visual_meta_from_text(segment)
    if inferred is not None and inferred.visual and inferred.normalized_query():
        return inferred
    from videoaudiotext.retrieval.queries import topic_fallback_query

    return SegmentVisualMeta(visual=True, search_query=topic_fallback_query(None))


def _merge_non_visual_units_with_groups(
    segments: List[str],
    groups: List[int],
    meta: List[SegmentVisualMeta | None] | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    """visual=false 段并入上一段；开篇 visual=false 则并入下一段。"""
    if not segments:
        return [], [], []
    if len(segments) != len(groups):
        raise ValueError("segments and groups length mismatch")

    meta = _align_visual_meta(segments, meta)
    segs = list(segments)
    grps = list(groups)
    metas = list(meta)

    while len(segs) > 1 and _is_non_visual_meta(metas[0]):
        segs[1] = segs[0] + segs[1]
        grps[1] = min(grps[0], grps[1])
        metas[1] = _combine_visual_meta(metas[0], metas[1])
        segs.pop(0)
        grps.pop(0)
        metas.pop(0)

    i = 1
    while i < len(segs):
        if _is_non_visual_meta(metas[i]):
            segs[i - 1] += segs[i]
            grps[i - 1] = min(grps[i - 1], grps[i])
            metas[i - 1] = _combine_visual_meta(metas[i - 1], metas[i])
            segs.pop(i)
            grps.pop(i)
            metas.pop(i)
        else:
            i += 1

    if len(segs) == 1 and _is_non_visual_meta(metas[0]):
        metas[0] = _promote_lone_non_visual_segment(segs[0], metas[0])

    return segs, grps, metas


def _merge_continuation_segments_with_groups(
    segments: List[str],
    groups: List[int],
    meta: List[SegmentVisualMeta | None] | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    if not segments:
        return [], [], []
    if len(segments) != len(groups):
        raise ValueError("segments and groups length mismatch")
    meta = _align_visual_meta(segments, meta)
    merged = [segments[0]]
    merged_groups = [groups[0]]
    merged_meta = [meta[0]]
    for segment, gi, mi in zip(segments[1:], groups[1:], meta[1:]):
        if _starts_continuation(segment) and merged:
            merged[-1] += segment
            merged_meta[-1] = _combine_visual_meta(merged_meta[-1], mi)
        else:
            merged.append(segment)
            merged_groups.append(gi)
            merged_meta.append(mi)
    return merged, merged_groups, merged_meta


def _merge_short_segments_with_groups(
    segments: List[str],
    groups: List[int],
    meta: List[SegmentVisualMeta | None] | None = None,
    *,
    min_len: int = _SOFT_MIN_MERGE_LEN,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    if not segments:
        return [], [], []
    if len(segments) != len(groups):
        raise ValueError("segments and groups length mismatch")
    meta = _align_visual_meta(segments, meta)
    merged = [segments[0]]
    merged_groups = [groups[0]]
    merged_meta = [meta[0]]
    for segment, gi, mi in zip(segments[1:], groups[1:], meta[1:]):
        if _segment_len(merged[-1]) < min_len or _segment_len(segment) < min_len:
            merged[-1] += segment
            merged_meta[-1] = _combine_visual_meta(merged_meta[-1], mi)
        else:
            merged.append(segment)
            merged_groups.append(gi)
            merged_meta.append(mi)
    if len(merged) > 1 and _segment_len(merged[-1]) < min_len:
        merged[-2] += merged.pop()
        merged_groups.pop()
        tail_meta = merged_meta.pop()
        merged_meta[-1] = _combine_visual_meta(merged_meta[-1], tail_meta)
    return merged, merged_groups, merged_meta


def refine_segments(segments: List[str], *, max_len: int = _MAX_SEG_LEN) -> List[str]:
    """轻量后处理：合并续写残段、过短段，并按字数切分超长段。"""
    refined, _, _ = refine_segments_with_groups(segments, max_len=max_len)
    return refined


def refine_segments_with_groups(
    segments: List[str],
    meta: List[SegmentVisualMeta | None] | None = None,
    *,
    max_len: int = _REFINE_MAX_LEN,
    source_text: str | None = None,
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    """
    与 refine_segments 相同，但保留 LLM 外层句 group id：
    合并/切分后同意外层句的子段共享 group，供横竖屏统一。
    """
    if not segments:
        return [], [], []

    groups = list(range(len(segments)))
    meta = _align_visual_meta(segments, meta)
    segments, groups, meta = _merge_non_visual_units_with_groups(segments, groups, meta)
    refined, groups, refined_meta = _merge_continuation_segments_with_groups(
        segments, groups, meta
    )
    refined, groups, refined_meta = _merge_fragmented_adjacent_with_groups(
        refined, groups, refined_meta, source_text=source_text
    )
    refined, groups, refined_meta = _merge_short_segments_with_groups(
        refined, groups, refined_meta
    )

    expanded: List[str] = []
    expanded_groups: List[int] = []
    expanded_meta: List[SegmentVisualMeta | None] = []
    for segment, gi, mi in zip(refined, groups, refined_meta):
        parts = _split_long_segment(segment, max_len=max_len)
        for part in parts:
            expanded.append(part)
            expanded_groups.append(gi)
            expanded_meta.append(
                _derive_visual_meta_for_part(part, mi) if len(parts) > 1 else mi
            )

    expanded, expanded_groups, expanded_meta = _merge_continuation_segments_with_groups(
        expanded, expanded_groups, expanded_meta
    )
    expanded, expanded_groups, expanded_meta = _merge_fragmented_adjacent_with_groups(
        expanded, expanded_groups, expanded_meta, source_text=source_text
    )
    expanded, expanded_groups, expanded_meta = _merge_short_segments_with_groups(
        expanded, expanded_groups, expanded_meta
    )
    expanded, expanded_groups, expanded_meta = _merge_non_visual_units_with_groups(
        expanded, expanded_groups, expanded_meta
    )
    out_segments, out_groups, out_meta = _trim_segments_to_budget_with_groups(
        expanded,
        expanded_groups,
        expanded_meta,
        source_text=source_text,
    )
    filtered_segments: List[str] = []
    filtered_groups: List[int] = []
    filtered_meta: List[SegmentVisualMeta | None] = []
    for segment, gi, mi in zip(out_segments, out_groups, out_meta):
        if segment.strip():
            filtered_segments.append(segment)
            filtered_groups.append(gi)
            filtered_meta.append(mi)
    return filtered_segments, filtered_groups, filtered_meta


def validate_segments(original: str, segments: List[str]) -> None:
    if not segments:
        raise ValueError("LLM 返回空 segments")
    if any(not str(s).strip() for s in segments):
        raise ValueError("LLM segments 含空段")
    if not segments_semantically_faithful(original, segments):
        raise ValueError("LLM 拆分结果语义与原文不一致")


def validate_visual_meta(
    segments: List[str], meta: List[SegmentVisualMeta | None]
) -> None:
    """Ensure every final segment has usable visual retrieval metadata."""
    if len(meta) != len(segments):
        raise ValueError("LLM units 元信息数量与分段数量不一致")
    for idx, item in enumerate(meta, 1):
        if item is None:
            raise ValueError(f"LLM units 第 {idx} 段缺少 visual/search_query")
        query = item.normalized_query()
        if not item.visual:
            raise ValueError(f"LLM units 第 {idx} 段 visual=false，应已并入相邻段")
        if not query:
            raise ValueError(f"LLM units 第 {idx} 段 visual=true 但 search_query 为空")


def _normalize_search_query(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "无"}:
        return None
    return text[:48]


def _parse_visual_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "是", "可视", "具象"}:
            return True
        if normalized in {"false", "no", "0", "否", "不可视", "抽象"}:
            return False
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def _parse_visual_meta_item(item: Any) -> SegmentVisualMeta | None:
    if not isinstance(item, dict):
        return None
    if "visual" not in item:
        return None
    visual = _parse_visual_bool(item.get("visual"))
    if visual is None:
        return None
    return SegmentVisualMeta(
        visual=visual,
        search_query=_normalize_search_query(item.get("search_query") or item.get("query")),
    )


def _strip_llm_fence(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return text
    fence = re.search(r"```(?:\w+)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def strip_split_markers(text: str) -> str:
    """去掉内联分句标记，用于与原文逐字比对。"""
    return _MARKER_RE.sub("", text)


def validate_marker_fidelity(original: str, llm_output: str) -> None:
    """内联标记输出必须与原文逐字一致（仅允许去掉标记本身）。"""
    stripped = strip_split_markers(_strip_llm_fence(llm_output))
    orig = original.strip()
    if stripped != orig:
        raise ValueError(
            "内联标记输出与原文逐字不一致"
            f"（原文 {len(orig)} 字，去标记后 {len(stripped)} 字）"
        )


def _query_from_marker_body(body: str) -> str | None:
    words = [w.strip() for w in re.split(r"[,，\s]+", body.strip()) if w.strip()]
    if not words:
        return None
    return " ".join(words[:5])[:48]


def parse_llm_marker_output(content: str) -> tuple[List[str], List[SegmentVisualMeta | None]]:
    """解析内联 [V:...] / [NV] 标记输出为 segments + visual meta。"""
    text = _strip_llm_fence(content)
    if not text:
        raise ValueError("LLM 返回空内容")

    parts = _MARKER_RE.split(text)
    segments: List[str] = []
    meta: List[SegmentVisualMeta | None] = []
    current = ""

    for part in parts:
        if not part:
            continue
        if part == "[NV]":
            if not current.strip():
                raise ValueError("内联标记 [NV] 前无对应口播文本")
            segments.append(current)
            meta.append(SegmentVisualMeta(visual=False, search_query=None))
            current = ""
            continue
        if part.startswith("[V:") and part.endswith("]"):
            query = _query_from_marker_body(part[3:-1])
            if not current.strip():
                raise ValueError("内联标记 [V:...] 前无对应口播文本")
            if not query:
                raise ValueError("内联标记 [V:...] 缺少有效检索词")
            segments.append(current)
            meta.append(SegmentVisualMeta(visual=True, search_query=query))
            current = ""
            continue
        current += part

    if current.strip():
        raise ValueError("末尾口播文本缺少 [V:...] 或 [NV] 标记")
    if not segments:
        raise ValueError("内联标记解析为空")
    return segments, meta


def _parse_llm_response_json(content: str) -> tuple[List[str], List[SegmentVisualMeta | None]]:
    text = _strip_llm_fence(content)
    if not text:
        raise ValueError("LLM 返回空内容")

    data: Any = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM JSON 格式无效")

    units = data.get("units")
    if isinstance(units, list) and units:
        segments: List[str] = []
        meta: List[SegmentVisualMeta | None] = []
        for item in units:
            if isinstance(item, dict):
                seg_text = str(
                    item.get("text") or item.get("segment") or item.get("sentence") or ""
                ).strip()
            else:
                seg_text = str(item).strip()
            if not seg_text:
                continue
            segments.append(seg_text)
            meta.append(_parse_visual_meta_item(item if isinstance(item, dict) else None))
        if not segments:
            raise ValueError("LLM units 解析为空")
        return segments, meta

    raw = data.get("segments") or data.get("sentences") or data.get("parts")
    if not isinstance(raw, list):
        raise ValueError("LLM segments 不是数组")

    segments = [str(s).strip() for s in raw if str(s).strip()]
    if not segments:
        raise ValueError("LLM segments 解析为空")
    return segments, [None] * len(segments)


def parse_llm_response(content: str) -> tuple[List[str], List[SegmentVisualMeta | None]]:
    """优先解析内联标记；无标记时回退 legacy JSON。"""
    text = _strip_llm_fence(content)
    if not text:
        raise ValueError("LLM 返回空内容")
    if _MARKER_RE.search(text):
        return parse_llm_marker_output(text)
    return _parse_llm_response_json(text)


def parse_llm_segments(content: str) -> List[str]:
    segments, _ = parse_llm_response(content)
    return segments


def _chat_completion(messages: List[dict[str, str]], *, json_mode: bool = True) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("未配置 LLM_API_KEY 或 OPENAI_API_KEY")

    url = f"{LLM_API_BASE.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    thinking = llm_thinking_payload()
    if thinking is not None:
        body["thinking"] = thinking
    if json_mode and llm_supports_json_mode():
        body["response_format"] = {"type": "json_object"}

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SEC) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        if json_mode and exc.code == 400 and "response_format" in detail.lower():
            return _chat_completion(messages, json_mode=False)
        raise RuntimeError(f"LLM API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API 连接失败: {exc}") from exc

    choices = parsed.get("choices") or []
    if not choices:
        raise RuntimeError("LLM API 无 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("LLM API 返回空 content")
    return str(content)


def split_sentence_llm_with_groups(
    text: str, *, max_attempts: int = 3
) -> tuple[List[str], List[int], List[SegmentVisualMeta | None]]:
    """Call LLM to split copy into semantic pipeline segments + outer group ids."""
    original = text.strip()
    if not original:
        return [], [], []

    comma_count, sentence_ends, unit_budget = _estimate_split_budget(original)
    enumerable = _count_enumerable_subject_clauses(original)
    min_units = max(sentence_ends + 1, enumerable + 2)
    user_prompt = (
        "请用内联标记法拆分以下口播文案。严格遵守 system 规则与示例；"
        "必须一字不差复述原文，仅在镜头单元末尾插入 [V:...] 或 [NV]。"
        f"全文约 {comma_count} 个逗号/顿号、{sentence_ends} 个句末标点、"
        f"约 {enumerable} 处多主体枚举；"
        f"预计插入 {min_units}~{unit_budget} 个 [V:...] 标记（"
        "多主体排比句禁止整句仅 1 个标记，必须明显少于逗号数）。"
        "输出前自检：去掉所有标记后与原文逐字一致；"
        "无续写残句独段、无整句一段的多主体枚举、固定对举（一边/第一组）不切。\n\n"
        f"{original}"
    )
    messages = [
        {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    last_err: Exception | None = None
    retry_hint = ""
    for attempt in range(1, max_attempts + 1):
        try:
            if retry_hint:
                messages = [
                    {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt + retry_hint},
                ]
            content = _chat_completion(messages, json_mode=False)
            validate_marker_fidelity(original, content)
            raw_segments, raw_meta = parse_llm_marker_output(content)
            segments, segment_groups, visual_meta = refine_segments_with_groups(
                raw_segments, raw_meta, source_text=original
            )
            validate_segments(original, segments)
            validate_visual_meta(segments, visual_meta)
            return segments, segment_groups, visual_meta
        except Exception as exc:
            last_err = exc
            if attempt < max_attempts:
                retry_hint = (
                    f"\n\n【纠正】上次失败：{exc}。"
                    "请一字不差复述原文，仅在每个镜头单元末尾插入 [V:...] 或 [NV]。"
                )
                continue
    print(
        f"[llm_split] WARNING: 语义校验连续 {max_attempts} 次失败，"
        f"将回退规则分句: {last_err}",
        file=sys.stderr,
    )
    raise last_err or RuntimeError("LLM 语义分句失败")


def split_sentence_llm(text: str, *, max_attempts: int = 3) -> List[str]:
    segments, _, _ = split_sentence_llm_with_groups(text, max_attempts=max_attempts)
    return segments
