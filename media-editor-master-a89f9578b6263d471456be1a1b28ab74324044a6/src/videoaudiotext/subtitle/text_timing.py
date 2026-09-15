"""Phonetic weight estimation and semantic phrase splitting for timeline alignment."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

from videoaudiotext.config import (
    COMMA_AUDIO_PAUSE_MIN_SEC,
    COMMA_SHORT_OPENING_MAX_CHARS,
    SUBTITLE_CUE_MIN_DISPLAY_SEC,
    SUBTITLE_FRAGMENT_MERGE_MAX_CHARS,
    SUBTITLE_LEAD_IN_SEC,
    SUBTITLE_POOL_COMPRESSED_MIN,
    SUBTITLE_POOL_SURPLUS_THRESHOLD,
    SUBTITLE_POST_HOLD_SEC,
    SUBTITLE_PROGRESSIVE_MAX_PARTS,
    SUBTITLE_PROGRESSIVE_MIN_PART_CHARS,
    SUBTITLE_PROGRESSIVE_MIN_SEGMENT_CHARS,
    TIMELINE_JIEBA_SPLIT_CHARS,
)

# 逗号停顿权重；时间轴分步仅用全角逗号「，」（顿号「、」为句内并列，不单独成步）
_PUNCT_PAUSE_WEIGHT = 0.4
_PAUSE_PUNCT = "，、"
_PHRASE_COMMA = "，"
_COMMA_SPLIT_RE = re.compile(r"([，])")
_DUNHAO_SPLIT_RE = re.compile(r"([、])")
_DIGIT_RE = re.compile(r"\d+")
_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_VOWEL_GROUP_RE = re.compile(r"[aeiouAEIOU]+")
_STRIP_PUNCT_RE = re.compile(r"[\s，。！？、；：“”‘’（）(),.!?;]")
_GOOD_BREAK_AFTER = frozenset("，、；")

Boundary = Tuple[int, str, str, str, str]  # pos, left_w, right_w, left_flag, right_flag


def _core_len(text: str) -> int:
    return len(_STRIP_PUNCT_RE.sub("", text))


def _digits_to_chinese_count(raw: str) -> int:
    return max(1, len(raw))


def _english_word_weight(word: str) -> float:
    w = word.strip()
    if not w:
        return 0.0
    vowels = len(_VOWEL_GROUP_RE.findall(w))
    if vowels <= 0:
        vowels = 1
    base = 1.5 + min(2.0, vowels * 0.75)
    if len(w) >= 8:
        base += 0.5
    return max(2.0, min(4.5, max(2.0, len(w) / 2.5)))


def get_phonetic_weight(text: str) -> float:
    """估算文本「拟音权重」（等效汉字发音时长）。"""
    if not text or not text.strip():
        return 0.5

    weight = 0.0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _PAUSE_PUNCT:
            weight += _PUNCT_PAUSE_WEIGHT
            i += 1
            continue
        if ch.isdigit():
            m = _DIGIT_RE.match(text, i)
            if m:
                weight += _digits_to_chinese_count(m.group())
                i = m.end()
                continue
        if ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
            m = _EN_WORD_RE.match(text, i)
            if m:
                weight += _english_word_weight(m.group())
                i = m.end()
                continue
        if "\u4e00" <= ch <= "\u9fff":
            weight += 1.0
        elif ch not in " \t\n\r\u3000。！？；.!?;":
            weight += 0.5
        i += 1

    return max(0.5, weight)


def _is_particle_flag(flag: str) -> bool:
    if not flag:
        return False
    return flag in ("uj", "ul", "uz", "ug", "ud", "u") or (
        flag[0] == "u" and flag not in ("uv",)
    )


def _is_noun_flag(flag: str) -> bool:
    return bool(flag) and flag.startswith("n")


def _is_verb_flag(flag: str) -> bool:
    return bool(flag) and flag.startswith("v")


def _is_conj_flag(flag: str) -> bool:
    return bool(flag) and flag.startswith("c")


def _is_adverb_flag(flag: str) -> bool:
    return bool(flag) and flag.startswith("d")


def _is_prep_flag(flag: str) -> bool:
    return bool(flag) and flag.startswith("p")


def _posseg_boundaries(text: str) -> List[Boundary]:
    try:
        import jieba.posseg as pseg
    except ImportError:
        words = _jieba_words_fallback(text)
        if len(words) <= 1:
            return []
        pos = 0
        out: List[Boundary] = []
        for i, word in enumerate(words):
            pos += len(word)
            if 0 < pos < len(text):
                out.append(
                    (
                        pos,
                        words[i],
                        words[i + 1] if i + 1 < len(words) else "",
                        "",
                        "",
                    )
                )
        return out

    pairs = list(pseg.cut(text))
    if len(pairs) <= 1:
        return []

    pos = 0
    out: List[Boundary] = []
    for i, pair in enumerate(pairs):
        pos += len(pair.word)
        if 0 < pos < len(text):
            rw = pairs[i + 1] if i + 1 < len(pairs) else None
            out.append(
                (
                    pos,
                    pair.word,
                    rw.word if rw else "",
                    pair.flag,
                    rw.flag if rw else "",
                )
            )
    return out


def _jieba_words_fallback(text: str) -> List[str]:
    try:
        import jieba
    except ImportError:
        return [text]
    return list(jieba.cut(text))


def _score_split_boundary(
    text: str,
    pos: int,
    *,
    left_word: str = "",
    right_word: str = "",
    left_flag: str = "",
    right_flag: str = "",
) -> float:
    """
    切分/折行边界评分（仅结构信息，无领域词表）：
    - 几何居中与左右均衡
    - jieba 词性（助词/连词/介词/名→动）
    - 过短片段与单字残片
    """
    n = len(text)
    if pos <= 0 or pos >= n:
        return 1e9

    mid = n / 2.0
    left, right = text[:pos], text[pos:]
    lc, rc = _core_len(left), _core_len(right)
    score = abs(pos - mid) * 1.2 + abs(lc - rc) * 0.9

    left_strip = left.rstrip()
    if left_strip and left_strip[-1] in _GOOD_BREAK_AFTER:
        score -= 12

    if _is_particle_flag(left_flag):
        score += 24
    if _is_particle_flag(right_flag):
        score += 18

    if _is_conj_flag(right_flag) or _is_adverb_flag(right_flag):
        score -= 10

    if _is_conj_flag(left_flag) or _is_prep_flag(left_flag):
        score += 14

    if _is_noun_flag(left_flag) and (
        _is_verb_flag(right_flag) or (right_flag and right_flag[0] == "a")
    ):
        score -= 10

    if _is_noun_flag(left_flag) and _is_noun_flag(right_flag) and len(right_word) == 1:
        score += 26

    if (
        len(right_word) == 1
        and not _is_particle_flag(right_flag)
        and not _is_verb_flag(right_flag)
        and not _is_conj_flag(right_flag)
        and right_word not in _GOOD_BREAK_AFTER
    ):
        score += 16

    if (
        len(left_word) <= 3
        and len(right_word) == 1
        and not _is_particle_flag(left_flag)
        and not _is_particle_flag(right_flag)
    ):
        score += 18

    if lc < 4 or rc < 4:
        score += 40

    return score


def _best_boundary_pos(text: str) -> int | None:
    candidates = _posseg_boundaries(text)
    if not candidates:
        return None

    best_pos, best_score = None, 1e9
    for pos, left_w, right_w, left_f, right_f in candidates:
        sc = _score_split_boundary(
            text,
            pos,
            left_word=left_w,
            right_word=right_w,
            left_flag=left_f,
            right_flag=right_f,
        )
        if sc < best_score:
            best_score = sc
            best_pos = pos
    return best_pos


# 时间轴切分禁止左尾单字（动补/助词残片），与 _accept_wrap_split 互补
_TEMPORAL_SPLIT_FORBID_LEFT_TAIL = frozenset("刚着过了起出上下完好成到见开住得")
_TEMPORAL_SPLIT_FORBID_RIGHT_HEAD = frozenset("出上来去过完好成到见开住上下炉")


def _accept_temporal_split(
    left: str,
    right: str,
    *,
    left_word: str = "",
    right_word: str = "",
    left_flag: str = "",
    right_flag: str = "",
) -> bool:
    """段内 progressive / jieba 中点切分是否可接受（比折行更严）。"""
    if not _accept_wrap_split(
        left,
        right,
        left_word=left_word,
        right_word=right_word,
        left_flag=left_flag,
        right_flag=right_flag,
    ):
        return False

    ls = left.rstrip()
    rs = right.lstrip()
    if not ls or not rs:
        return False

    lt = ls[-1]
    rh = rs[0]
    if lt in _TEMPORAL_SPLIT_FORBID_LEFT_TAIL and rh in _TEMPORAL_SPLIT_FORBID_RIGHT_HEAD:
        return False

    # 「捧着刚|出炉」：左尾单字补语 + 右首动词/名物延续
    if (
        lt in _TEMPORAL_SPLIT_FORBID_LEFT_TAIL
        and _core_len(ls) >= 4
        and (_is_verb_flag(right_flag) or _is_noun_flag(right_flag))
    ):
        return False

    if _is_particle_flag(left_flag) and _core_len(left_word) <= 1:
        return False

    return True


def _jieba_split_midpoint(text: str) -> List[str]:
    if "，" in text:
        return [text]

    candidates = _posseg_boundaries(text)
    if not candidates:
        return [text]

    scored: list[tuple[float, str, str]] = []
    for pos, left_w, right_w, left_f, right_f in candidates:
        left, right = text[:pos].strip(), text[pos:].strip()
        if _core_len(left) < 4 or _core_len(right) < 4:
            continue
        if not _accept_temporal_split(
            left,
            right,
            left_word=left_w,
            right_word=right_w,
            left_flag=left_f,
            right_flag=right_f,
        ):
            continue
        sc = _score_split_boundary(
            text,
            pos,
            left_word=left_w,
            right_word=right_w,
            left_flag=left_f,
            right_flag=right_f,
        )
        scored.append((sc, left, right))

    if not scored:
        return [text]
    scored.sort(key=lambda item: item[0])
    _, left, right = scored[0]
    return [left, right]


def split_phrase_at_jieba_midpoint(text: str) -> tuple[str, str] | None:
    parts = _jieba_split_midpoint(text)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def effective_comma_pause_min_sec(speech_duration: float | None) -> float:
    """逗号静音阈值：基线 + 随段长略增，避免快口播误切/慢口播漏切。"""
    base = COMMA_AUDIO_PAUSE_MIN_SEC
    if speech_duration is None or speech_duration <= 0:
        return base
    scaled = max(0.12, float(speech_duration) * 0.025)
    return min(0.35, max(base, scaled))


def _accept_wrap_split(
    left: str,
    right: str,
    *,
    left_word: str = "",
    right_word: str = "",
    left_flag: str = "",
    right_flag: str = "",
) -> bool:
    """判断双行折行是否在词性上可接受（通用，无领域词表）。"""
    if not left or not right:
        return False

    rc = _core_len(right)

    if right.lstrip().startswith("的"):
        return False

    from videoaudiotext.subtitle.breaks import split_crosses_compound

    if split_crosses_compound(left, right):
        return False

    # 「一|整日」等量词+时间词
    if left.rstrip().endswith("一") and right.lstrip().startswith("日"):
        return False

    # 数量词 + 名词：如「一座|城市」
    if left_flag.startswith("m") and _is_noun_flag(right_flag):
        return False

    # 名词 + 短动词开头：动补/谓语被拆
    if (
        (_is_noun_flag(left_flag) or left_flag.startswith("m"))
        and _is_verb_flag(right_flag)
        and len(right_word) <= 3
        and rc <= 5
    ):
        return False

    # 连续短名词：jieba 常将动补/谓语误标为名词（如「代代|吃惯」「馆|错落」）
    if (
        _is_noun_flag(left_flag)
        and _is_noun_flag(right_flag)
        and len(left_word) <= 3
        and len(right_word) <= 3
        and rc <= 6
    ):
        return False

    # 动词 + 短动词/补语：如「吃|惯」
    if (
        _is_verb_flag(left_flag)
        and _is_verb_flag(right_flag)
        and len(right_word) <= 2
    ):
        return False

    if (
        len(left_word) == 1
        and left_word == "一"
        and right_word.startswith("日")
    ):
        return False

    # 含逗号时只允许在逗号后折行，禁止 jieba 在子句内部切词
    if "，" in left + right and not left.rstrip().endswith("，"):
        return False

    if _is_particle_flag(left_flag) or _is_particle_flag(right_flag):
        return False

    return True


def wrap_display_two_lines(text: str, max_line: int = 12) -> str | None:
    """
    双行展示折行。边界不佳时返回 None，由调用方保持单行。
    含「，」的文本不在此处理（由 subtitle 层逗号折行规则负责）。
    """
    text = text.strip()
    if not text or len(text) <= max_line:
        return text if text else None
    if "，" in text:
        return None

    pos = _best_boundary_pos(text)
    if pos is None:
        return None

    left, right = text[:pos].strip(), text[pos:].strip()
    if not left or not right:
        return None
    if len(left) > max_line + 3 or len(right) > max_line + 3:
        return None
    if _core_len(left) < 3 or _core_len(right) < 3:
        return None

    boundaries = _posseg_boundaries(text)
    left_f = right_f = ""
    right_w = ""
    for p, lw, rw, lf, rf in boundaries:
        if p == pos:
            left_f, right_f, right_w = lf, rf, rw
            break

    if not _accept_wrap_split(
        left,
        right,
        left_word=left_f,
        right_word=right_w,
        left_flag=left_f,
        right_flag=right_f,
    ):
        return None

    return f"{left}\n{right}"


def _split_comma_keep_punct(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []

    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span

    phrases: List[str] = []
    current = ""
    for i, ch in enumerate(text):
        if ch == _PHRASE_COMMA and not position_inside_protected_span(text, i):
            current += ch
            if current.strip():
                phrases.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        phrases.append(current.strip())

    return [p for p in phrases if p]


def split_comma_phrases(text: str) -> List[str]:
    """逗号短语列表（去尾标点），供检索/计数；时间轴切分见 split_segment_phrases。"""
    parts = _split_comma_keep_punct(text)
    cleaned = [re.sub(r"[，、；]+$", "", p).strip() for p in parts]
    cleaned = [p for p in cleaned if p]
    if len(cleaned) <= 1:
        return [text.strip()] if text.strip() else []
    return cleaned


def _opening_unit_core_len(chunk: str) -> int:
    return _core_len(chunk.strip().rstrip("，"))


def _is_short_opening_unit(chunk: str) -> bool:
    t = chunk.strip()
    if not t.endswith("，"):
        return False
    core = _opening_unit_core_len(t)
    return 0 < core <= COMMA_SHORT_OPENING_MAX_CHARS


def _is_all_ultra_short_parallel(chunks: List[str]) -> bool:
    """特例 A：连续超短排比，每段≤4字 → 全部逗号/顿号正常切。"""
    if len(chunks) <= 1:
        return False
    for c in chunks:
        core = _core_len(c.strip().rstrip("，。！？；、"))
        if core <= 0 or core > COMMA_SHORT_OPENING_MAX_CHARS:
            return False
    return True


def _split_dunhao_keep_punct(text: str) -> List[str]:
    text = text.strip()
    if not text or "、" not in text:
        return [text] if text else []

    from videoaudiotext.subtitle.protected_spans import position_inside_protected_span

    phrases: List[str] = []
    current = ""
    for i, ch in enumerate(text):
        current += ch
        if ch == "、" and not position_inside_protected_span(text, i):
            if current.strip():
                phrases.append(current.strip())
            current = ""
    if current.strip():
        phrases.append(current.strip())
    return [p for p in phrases if p]


def _dunhao_timeline_chunks_if_enabled(text: str) -> List[str] | None:
    from videoaudiotext.config.subtitle import dunhao_progressive_enabled

    if not dunhao_progressive_enabled() or "、" not in text or "，" in text:
        return None
    chunks = _split_dunhao_keep_punct(text)
    if len(chunks) <= 1:
        return None
    if not _is_all_ultra_short_parallel(chunks):
        return None
    return chunks


def _merge_comma_chunks_preview(chunks: List[str]) -> List[str]:
    """
    预览/无音频：短句起头首逗号保连体，其后逗号正常切。
    长句起头（>4字）与全短排比不受首逗号保护。
    """
    if len(chunks) <= 1:
        return chunks
    from videoaudiotext.subtitle.quotes import all_punctuation_clauses_fit_screen

    if all_punctuation_clauses_fit_screen(chunks):
        return chunks
    if _is_all_ultra_short_parallel(chunks):
        return chunks
    if _is_short_opening_unit(chunks[0]) and len(chunks) >= 2:
        return [chunks[0] + chunks[1]] + chunks[2:]
    return chunks


def _split_oversized_phrase(text: str, max_chars: int) -> List[str]:
    """超长短语：含逗号时只在逗号处切分，禁止 jieba 在子句内断词。"""
    t = text.strip()
    if not t or _core_len(t) <= max_chars:
        return [t] if t else []

    if "，" in t:
        pieces = _split_comma_keep_punct(t)
        if len(pieces) <= 1:
            return [t]

        out: List[str] = []
        buf = ""
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            candidate = buf + piece if buf else piece
            if _core_len(candidate) <= max_chars:
                buf = candidate
            else:
                if buf:
                    out.append(buf)
                if _core_len(piece) > max_chars:
                    out.extend(_split_oversized_phrase(piece, max_chars))
                    buf = ""
                else:
                    buf = piece
        if buf:
            out.append(buf)
        return [p for p in out if p.strip()]

    # 无逗号超长块：保持单条，由字幕层双行折行
    return [t]


def _should_force_merge_short_opening(
    chunk_index: int,
    merge_start_index: int,
    chunk: str,
) -> bool:
    """段首短引导语（细看，/放眼望去，）与后句连体，不因 TTS 换气误切。"""
    if chunk_index != merge_start_index:
        return False
    return _is_short_opening_unit(chunk) or _is_opening_guide_phrase(chunk, 0)


def progressive_min_part_chars() -> int:
    return max(4, SUBTITLE_PROGRESSIVE_MIN_PART_CHARS)


def progressive_max_parts() -> int:
    return max(2, SUBTITLE_PROGRESSIVE_MAX_PARTS)


def _cap_progressive_comma_chunks(chunks: List[str], max_parts: int) -> List[str]:
    if len(chunks) <= max_parts:
        return chunks
    head = chunks[: max_parts - 1]
    tail = "".join(chunks[max_parts - 1 :])
    return head + [tail]


def _comma_boundary_chunks_fit_screen(left: str, right: str) -> bool:
    """逗号两侧子句各自不超软/像素上限 → 保留逗号分步，不因 min_part 合并。"""
    from videoaudiotext.config.subtitle import (
        progressive_pixel_max_chars,
        progressive_soft_max_chars,
    )
    from videoaudiotext.subtitle.display import _core_char_len

    if not left.rstrip().endswith("，"):
        return False
    soft = progressive_soft_max_chars()
    pixel = progressive_pixel_max_chars()
    for part in (left, right):
        n = _core_char_len(part.strip())
        if n > soft or n > pixel:
            return False
    return True


def _progressive_comma_part_len(chunk: str) -> int:
    from videoaudiotext.subtitle.display import _core_char_len

    return _core_char_len(chunk.strip().rstrip("，"))


def _progressive_display_phrase(text: str) -> str:
    from videoaudiotext.subtitle.quotes import _progressive_clause_display

    return _progressive_clause_display(text)


def _enforce_progressive_part_mins(chunks: List[str], min_part: int) -> List[str]:
    if len(chunks) <= 1:
        return chunks
    work = list(chunks)
    while len(work) > 1:
        merged = False
        for i in range(len(work) - 1):
            if _core_len(work[i]) < min_part or _core_len(work[i + 1]) < min_part:
                if _comma_boundary_chunks_fit_screen(work[i], work[i + 1]):
                    continue
                work[i : i + 2] = [work[i] + work[i + 1]]
                merged = True
                break
        if not merged:
            break
    return work


def _finalize_progressive_comma_chunks(chunks: List[str]) -> List[str]:
    """段内分步收尾：去碎句、每段最多 max_parts 条。"""
    if len(chunks) <= 1:
        return chunks
    if _is_all_ultra_short_parallel(chunks):
        return chunks
    min_part = progressive_min_part_chars()
    work = _enforce_progressive_part_mins(chunks, min_part)
    if len(work) <= 1:
        return work
    return _cap_progressive_comma_chunks(work, progressive_max_parts())


def _comma_chunks_for_eligibility(text: str) -> List[str]:
    raw = _split_comma_keep_punct(text.strip())
    if len(raw) <= 1:
        return raw
    return _finalize_progressive_comma_chunks(_merge_comma_chunks_preview(raw))


def progressive_split_eligible(text: str, *, max_line: int | None = None) -> bool:
    """段内时间轴切分：超软/硬上限且可切成 ≥2 条有意义短语。"""
    from videoaudiotext.config.subtitle import (
        dunhao_progressive_enabled,
        progressive_soft_max_chars,
        progressive_text_needs_split,
    )

    t = text.strip()
    if not t:
        return False

    soft = max_line if max_line is not None else progressive_soft_max_chars()
    if not progressive_text_needs_split(t, max_line=soft):
        return False

    if "，" in t:
        raw = _split_comma_keep_punct(t)
        if _is_all_ultra_short_parallel(raw):
            return len(raw) >= 2
        from videoaudiotext.subtitle.quotes import all_punctuation_clauses_fit_screen

        if len(raw) >= 2 and all_punctuation_clauses_fit_screen(raw):
            return True
        finalized = _comma_chunks_for_eligibility(t)
        if len(finalized) < 2:
            return False
        return all(_progressive_comma_part_len(c) >= 4 for c in finalized)

    if dunhao_progressive_enabled() and "、" in t:
        dchunks = _split_dunhao_keep_punct(t)
        return _is_all_ultra_short_parallel(dchunks) and len(dchunks) >= 2
    return False


def _merge_comma_chunks_audio(
    chunks: List[str],
    segment_text: str,
    wav_path: Path | str | None,
    speech_duration: float | None,
    *,
    min_pause: float,
    max_chars: int | None = None,
) -> List[str]:
    """成片：逗号处静音≥阈值才切 Cue，否则合并为同条（仅屏内折行）。"""
    if max_chars is None:
        max_chars = TIMELINE_JIEBA_SPLIT_CHARS
    if len(chunks) <= 1:
        return chunks
    if not wav_path or speech_duration is None or speech_duration <= 0:
        return _merge_comma_chunks_preview(chunks)

    from videoaudiotext.subtitle.audio_pause import comma_has_audio_pause

    path = Path(wav_path)
    if not path.is_file():
        return _merge_comma_chunks_preview(chunks)

    merged: List[str] = []
    i = 0
    while i < len(chunks):
        current = chunks[i]
        j = i
        while j + 1 < len(chunks):
            nxt = chunks[j + 1]
            if _is_mid_comma_interjection(nxt):
                break
            force_merge = _should_force_merge_short_opening(j, i, current)
            min_part = progressive_min_part_chars()
            can_split = (
                _core_len(current) >= min_part and _core_len(nxt) >= min_part
            )
            if (
                not force_merge
                and can_split
                and comma_has_audio_pause(
                    path,
                    segment_text,
                    current,
                    float(speech_duration),
                    min_pause=min_pause,
                )
            ):
                break
            candidate = current + nxt
            if _core_len(candidate) > max_chars:
                break
            j += 1
            current = candidate
        merged.append(current)
        i = j + 1
    merged = _detach_mid_interjections_from_merged_chunks(merged)
    return _finalize_progressive_comma_chunks(merged)


def _apply_comma_timeline_chunks(
    text: str,
    raw_chunks: List[str],
    *,
    wav_path: Path | str | None = None,
    speech_duration: float | None = None,
) -> List[str]:
    """2.1 逗号切分：音频停顿优先，无音频走文本兜底。"""
    if len(raw_chunks) <= 1:
        return raw_chunks if raw_chunks else [text.strip()]
    if wav_path and speech_duration and Path(wav_path).is_file():
        chunks = _merge_comma_chunks_audio(
            raw_chunks,
            text,
            wav_path,
            speech_duration,
            min_pause=effective_comma_pause_min_sec(speech_duration),
        )
    else:
        chunks = _merge_comma_chunks_preview(raw_chunks)
        chunks = _finalize_progressive_comma_chunks(chunks)
    return chunks


def _chunks_to_phrase_dicts(chunks: List[str]) -> List[dict]:
    out: List[dict] = []
    for gi, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if chunk:
            out.append({"text": chunk, "comma_group": gi})
    return out


def _post_period_opener_core_len(chunk: str) -> int:
    """Core length of the clause after the last 「。」 in a comma chunk."""
    chunk = chunk.strip()
    if "。" not in chunk:
        return 0
    after = chunk.rsplit("。", 1)[-1].strip().rstrip("，")
    return _core_len(after) if after else 0


def _is_post_period_short_opener(chunk: str) -> bool:
    """句末句号后的短引导子句（如「基因。而今天的佛山，」）应与后句连体。"""
    from videoaudiotext.config import COMMA_SHORT_OPENING_MAX_CHARS

    chunk = chunk.strip()
    if "。" not in chunk or not chunk.endswith("，"):
        return False
    core = _post_period_opener_core_len(chunk)
    return 0 < core <= COMMA_SHORT_OPENING_MAX_CHARS + 2


def _merge_post_period_short_opener_chunks(chunks: List[str], *, max_chars: int) -> List[str]:
    """Merge a short post-period opener chunk with the following comma clause."""
    if len(chunks) < 2:
        return chunks
    out: List[str] = []
    i = 0
    while i < len(chunks):
        cur = chunks[i].strip()
        if (
            i + 1 < len(chunks)
            and _is_post_period_short_opener(cur)
            and _core_len(cur + chunks[i + 1]) <= max_chars
        ):
            out.append(cur + chunks[i + 1].strip())
            i += 2
            continue
        out.append(chunks[i])
        i += 1
    return out


def _expand_phrase_at_sub_punct(chunk: str, max_chars: int) -> List[str]:
    """Split at em-dash or balanced dunhao when each part fits the phrase budget."""
    chunk = chunk.strip()
    if not chunk:
        return []

    if "——" in chunk:
        parts: List[str] = []
        rest = chunk
        while "——" in rest:
            idx = rest.index("——")
            piece = rest[:idx].strip()
            rest = rest[idx + 2 :].strip()
            if piece:
                parts.append(piece)
        if rest.strip():
            parts.append(rest)
        if len(parts) > 1:
            return parts

    if "、" in chunk:
        from videoaudiotext.config.subtitle import scaled_subtitle_merge_max_chars

        dchunks = _split_dunhao_keep_punct(chunk)
        if len(dchunks) > 1:
            part_limit = max(max_chars, scaled_subtitle_merge_max_chars())
            cores = [
                _core_len(c.strip().rstrip("，。！？；.!?"))
                for c in dchunks
            ]
            allow_parallel = _is_all_ultra_short_parallel(dchunks)
            allow_leading_item = (
                cores[0] >= progressive_min_part_chars()
                and cores[0] <= COMMA_SHORT_OPENING_MAX_CHARS + 1
                and cores[1] >= progressive_min_part_chars()
            )
            if all(c <= part_limit for c in cores) and (
                allow_parallel
                or allow_leading_item
                or (
                    "，" not in chunk
                    and all(c >= progressive_min_part_chars() for c in cores)
                )
            ):
                return dchunks

    return [chunk]


def _merge_short_comma_opener_chunks(chunks: List[str], *, max_chars: int) -> List[str]:
    """Merge a short comma-ending opener with the following clause for steadier pacing."""
    if len(chunks) < 2:
        return chunks
    out: List[str] = []
    i = 0
    while i < len(chunks):
        cur = chunks[i].strip()
        if (
            i + 1 < len(chunks)
            and cur.endswith("，")
            and not _is_mid_comma_interjection(cur)
            and _opening_unit_core_len(cur) <= COMMA_SHORT_OPENING_MAX_CHARS
        ):
            nxt = chunks[i + 1].strip()
            combined = cur + nxt
            if _core_len(combined) <= max_chars:
                out.append(combined)
                i += 2
                continue
        out.append(chunks[i])
        i += 1
    return out


def split_segment_phrases(
    text: str,
    *,
    max_chars: int | None = None,
    wav_path: Path | str | None = None,
    speech_duration: float | None = None,
    force: bool = False,
) -> List[dict]:
    from videoaudiotext.config.subtitle import (
        progressive_soft_max_chars,
        progressive_text_needs_split,
    )

    soft_max = progressive_soft_max_chars()
    if max_chars is None:
        max_chars = soft_max

    text = text.strip()
    if not text:
        return []

    if not progressive_text_needs_split(text, max_line=soft_max):
        return [{"text": text, "comma_group": 0}]

    if not progressive_split_eligible(text, max_line=soft_max):
        return [{"text": text, "comma_group": 0}]

    raw_chunks = _split_comma_keep_punct(text)
    if not raw_chunks:
        raw_chunks = [text]

    from videoaudiotext.subtitle.quotes import all_punctuation_clauses_fit_screen

    if len(raw_chunks) >= 2 and all_punctuation_clauses_fit_screen(raw_chunks):
        return _merge_short_fragments(
            [
                {
                    "text": _progressive_display_phrase(c.strip()),
                    "comma_group": gi,
                }
                for gi, c in enumerate(raw_chunks)
                if c.strip()
            ]
        )

    raw_chunks = _merge_short_comma_opener_chunks(raw_chunks, max_chars=max_chars)
    raw_chunks = _merge_post_period_short_opener_chunks(raw_chunks, max_chars=max_chars)

    if len(raw_chunks) <= 1 and "，" not in text:
        expanded = _expand_phrase_at_sub_punct(text, max_chars)
        if len(expanded) > 1:
            return _merge_short_fragments(_chunks_to_phrase_dicts(expanded))
        dchunks = _dunhao_timeline_chunks_if_enabled(text)
        if dchunks:
            return _merge_short_fragments(_chunks_to_phrase_dicts(dchunks))
        # 无逗号长句：时间轴不切步，仅屏内双行折行（避免 jieba 误切动补/介宾）
        return _merge_short_fragments([{"text": text, "comma_group": 0}])

    comma_chunks = _apply_comma_timeline_chunks(
        text,
        raw_chunks,
        wav_path=wav_path,
        speech_duration=speech_duration,
    )

    result: List[dict] = []
    for gi, chunk in enumerate(comma_chunks):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sub in _expand_phrase_at_sub_punct(chunk, max_chars):
            sub = sub.strip()
            if sub:
                result.append(
                    {
                        "text": _progressive_display_phrase(sub),
                        "comma_group": gi,
                    }
                )

    merged = _merge_short_fragments(result if result else [{"text": text, "comma_group": 0}])
    return merged if merged else [{"text": text, "comma_group": 0}]


def _is_mid_comma_interjection(text: str) -> bool:
    """逗号之间的单字语气词（如「哇，」）→ 并入后条，不挂在前条末尾。"""
    t = text.strip()
    if not t.endswith("，"):
        return False
    if re.search(r"[！!？?]", t):
        return False
    body = t.rstrip("，").strip()
    return _core_len(body) == 1


def _detach_mid_interjections_from_merged_chunks(chunks: List[str]) -> List[str]:
    """音频无停顿时可能误并「…，哇，」；拆回独立语气词块以便后并入下条。"""
    out: List[str] = []
    for chunk in chunks:
        parts = _split_comma_keep_punct(chunk)
        if len(parts) <= 1:
            out.append(chunk)
            continue
        buf: List[str] = []
        for part in parts:
            if buf and _is_mid_comma_interjection(part):
                out.append("".join(buf))
                buf = [part]
            else:
                buf.append(part)
        if buf:
            out.append("".join(buf))
    return out


def _should_merge_short_comma_lead_forward(
    text: str,
    next_text: str,
    *,
    max_combined: int | None = None,
) -> bool:
    """短逗号子句（如「琳琅满目，」）与后接感叹句合并为同条 Cue，避免单独闪一下。"""
    if max_combined is None:
        max_combined = TIMELINE_JIEBA_SPLIT_CHARS
    t = text.strip()
    nxt = next_text.strip()
    if not t.endswith("，") or not nxt:
        return False
    if _is_mid_comma_interjection(t):
        return False
    body = t.rstrip("，").strip()
    if _core_len(body) > COMMA_SHORT_OPENING_MAX_CHARS:
        return False
    if not re.search(r"[！!？?]", nxt):
        return False
    return _core_len(t + nxt) <= max_combined


def _should_merge_short_fragment(text: str, *, max_chars: int | None = None) -> bool:
    """句末短感叹（如「太诱人啦！」「哇！」）并入前条；「哇，」等走 _is_mid_comma_interjection。"""
    if max_chars is None:
        max_chars = SUBTITLE_FRAGMENT_MERGE_MAX_CHARS
    if _is_mid_comma_interjection(text):
        return False
    t = text.strip()
    core = _core_len(t)
    if core <= 0:
        return False
    if not re.search(r"[！!？?]", t):
        return False
    if core <= max_chars:
        return True
    return core <= 4


def _merge_short_fragments(phrases: List[dict]) -> List[dict]:
    if len(phrases) <= 1:
        return phrases

    out: List[dict] = []
    i = 0
    while i < len(phrases):
        cur = phrases[i]
        text = cur["text"]

        if i + 1 < len(phrases) and _is_mid_comma_interjection(text):
            nxt = phrases[i + 1]
            out.append(
                {
                    **cur,
                    "text": text + nxt["text"],
                    "comma_group": nxt.get("comma_group", cur.get("comma_group", 0)),
                }
            )
            i += 2
            continue

        if i + 1 < len(phrases) and _should_merge_short_comma_lead_forward(
            text, phrases[i + 1]["text"]
        ):
            nxt = phrases[i + 1]
            out.append(
                {
                    **cur,
                    "text": text + nxt["text"],
                    "comma_group": nxt.get("comma_group", cur.get("comma_group", 0)),
                }
            )
            i += 2
            continue

        if out and _should_merge_short_fragment(text):
            prev = out[-1]
            out[-1] = {
                **prev,
                "text": prev["text"] + text,
            }
        else:
            out.append(cur)
        i += 1
    return out


def _surplus_available(
    dur: float,
    *,
    surplus_threshold: float,
    compressed_min: float,
) -> float:
    """长条可出让时长：>threshold 部分，且压缩后不低于 compressed_min。"""
    if dur <= surplus_threshold:
        return 0.0
    return min(dur - surplus_threshold, dur - compressed_min)


def _is_opening_guide_phrase(text: str, index: int) -> bool:
    """段首语气引导短句（如「细看，」），池不够时才允许与后条合并。"""
    if index != 0:
        return False
    t = text.strip()
    if not t.endswith("，"):
        return False
    core = _core_len(t)
    return 0 < core <= 5


def _merge_opening_guide_phrases(phrases: List[dict]) -> List[dict]:
    if len(phrases) < 2 or not _is_opening_guide_phrase(phrases[0]["text"], 0):
        return phrases
    merged = {
        "text": phrases[0]["text"] + phrases[1]["text"],
        "comma_group": phrases[0].get("comma_group", 0),
    }
    return [merged] + phrases[2:]


def _normalize_durations_to_total(durs: List[float], speech_end: float) -> List[float]:
    if not durs:
        return []
    total = sum(durs)
    if total <= 1e-9:
        return [speech_end / len(durs)] * len(durs)
    if abs(total - speech_end) <= 1e-6:
        return list(durs)
    scale = speech_end / total
    return [d * scale for d in durs]


def _durations_to_slices(durs: List[float], speech_end: float) -> List[Tuple[float, float]]:
    durs = _normalize_durations_to_total(durs, speech_end)
    slices: List[Tuple[float, float]] = []
    t = 0.0
    for i, d in enumerate(durs):
        end = speech_end if i == len(durs) - 1 else t + d
        slices.append((t, end))
        t = end
    return slices


def _rebalance_segment_duration_pool(
    durs: List[float],
    speech_end: float,
    *,
    min_dur: float,
    surplus_threshold: float,
    compressed_min: float,
) -> List[float]:
    """
    同 Segment 时长池分时：总占用 = speech_end；仅从 >threshold 长条收时补短条。
    切分点位不变，只伸缩各条 duration。
    """
    n = len(durs)
    if n == 0:
        return []
    if n == 1:
        return [speech_end]

    durs = _normalize_durations_to_total([max(1e-6, d) for d in durs], speech_end)

    for _ in range(n * 32):
        changed = False
        deficit_indices = [i for i, d in enumerate(durs) if d < min_dur - 1e-9]
        if not deficit_indices:
            break
        deficit_indices.sort(key=lambda i: durs[i])

        for recv_i in deficit_indices:
            need = min_dur - durs[recv_i]
            if need <= 1e-9:
                continue
            donors = [
                (i, _surplus_available(durs[i], surplus_threshold=surplus_threshold, compressed_min=compressed_min))
                for i in range(n)
                if i != recv_i
            ]
            donors = [(i, avail) for i, avail in donors if avail > 1e-9]
            donors.sort(key=lambda x: -x[1])
            for donor_i, avail in donors:
                give = min(need, avail)
                if give <= 1e-9:
                    continue
                durs[recv_i] += give
                durs[donor_i] -= give
                need -= give
                changed = True
                if need <= 1e-9:
                    break
        if not changed:
            break

    return _normalize_durations_to_total(durs, speech_end)


def _apply_segment_duration_pool(
    slices: List[Tuple[float, float]],
    speech_end: float,
    phrases: List[dict],
    *,
    min_dur: float,
    surplus_threshold: float,
    compressed_min: float,
) -> Tuple[List[Tuple[float, float]], List[dict]]:
    """时长池调剂；池仍不足时仅合并段首引导短句与后条。"""
    work_phrases = list(phrases)
    for _ in range(max(2, len(work_phrases))):
        durs = [max(1e-6, e - s) for s, e in slices]
        durs = _rebalance_segment_duration_pool(
            durs,
            speech_end,
            min_dur=min_dur,
            surplus_threshold=surplus_threshold,
            compressed_min=compressed_min,
        )
        if not (
            len(work_phrases) >= 2
            and durs[0] < min_dur - 1e-9
            and _is_opening_guide_phrase(work_phrases[0]["text"], 0)
        ):
            return _durations_to_slices(durs, speech_end), work_phrases

        merged = _merge_opening_guide_phrases(work_phrases)
        if len(merged) == len(work_phrases):
            return _durations_to_slices(durs, speech_end), work_phrases
        work_phrases = merged
        weights = [get_phonetic_weight(p["text"]) for p in work_phrases]
        slices = distribute_intervals(speech_end, weights, min_slice=0.08)

    durs = [max(1e-6, e - s) for s, e in slices]
    durs = _rebalance_segment_duration_pool(
        durs,
        speech_end,
        min_dur=min_dur,
        surplus_threshold=surplus_threshold,
        compressed_min=compressed_min,
    )
    return _durations_to_slices(durs, speech_end), work_phrases


def split_segment_into_steps(
    text: str,
    total_duration: float,
    *,
    post_hold: float | None = None,
    lead_in: float | None = None,
    min_display: float | None = None,
    min_slice: float = 0.08,
    wav_path: Path | str | None = None,
    force: bool = False,
    max_chars: int | None = None,
) -> List[dict]:
    if post_hold is None:
        post_hold = SUBTITLE_POST_HOLD_SEC
    if lead_in is None:
        lead_in = SUBTITLE_LEAD_IN_SEC
    if min_display is None:
        min_display = SUBTITLE_CUE_MIN_DISPLAY_SEC

    phrases = split_segment_phrases(
        text,
        max_chars=max_chars,
        wav_path=wav_path,
        speech_duration=total_duration,
        force=force,
    )
    if not phrases:
        return []

    speech_dur = max(min_slice * len(phrases), total_duration)
    weights = [get_phonetic_weight(p["text"]) for p in phrases]
    slices = distribute_intervals(speech_dur, weights, min_slice=min_slice)
    slices, phrases = _apply_segment_duration_pool(
        slices,
        speech_dur,
        phrases,
        min_dur=min_display,
        surplus_threshold=SUBTITLE_POOL_SURPLUS_THRESHOLD,
        compressed_min=SUBTITLE_POOL_COMPRESSED_MIN,
    )
    weights = [get_phonetic_weight(p["text"]) for p in phrases]

    if slices:
        s0, s1 = slices[0]
        slices[0] = (s0 - lead_in, s1)
        ls, le = slices[-1]
        slices[-1] = (ls, le + post_hold)

    steps: List[dict] = []
    for i, (phrase, (s0, s1)) in enumerate(zip(phrases, slices)):
        steps.append(
            {
                "part_index": i + 1,
                "text": phrase["text"],
                "comma_group": phrase["comma_group"],
                "weight_phonetic": round(weights[i], 2),
                "start_time": round(s0, 4),
                "end_time": round(s1, 4),
                "duration": round(max(min_slice, s1 - s0), 4),
            }
        )
    return steps


def distribute_intervals(
    total_duration: float,
    weights: List[float],
    *,
    min_slice: float = 0.08,
) -> List[Tuple[float, float]]:
    if not weights:
        return []
    if len(weights) == 1:
        return [(0.0, max(min_slice, total_duration))]

    total_w = sum(max(0.1, w) for w in weights)
    dur = max(min_slice * len(weights), total_duration)
    out: List[Tuple[float, float]] = []
    elapsed = 0.0
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            out.append((elapsed, dur))
        else:
            frac = max(0.1, w) / total_w
            end = elapsed + dur * frac
            end = max(elapsed + min_slice, end)
            out.append((elapsed, end))
            elapsed = end
    return out
