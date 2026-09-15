"""Text splitting, LLM, and pre-split settings."""

from __future__ import annotations

import os

TEXT_SPLIT_MODE = os.environ.get("TEXT_SPLIT_MODE", "llm").strip().lower()
_ark_key = os.environ.get("ARK_API_KEY", "").strip()
LLM_API_KEY = (
    os.environ.get("LLM_API_KEY", "").strip()
    or os.environ.get("OPENAI_API_KEY", "").strip()
    or _ark_key
)
if os.environ.get("LLM_API_BASE", "").strip():
    _llm_api_base_default = os.environ.get("LLM_API_BASE", "").strip()
elif _ark_key:
    _llm_api_base_default = "https://ark.cn-beijing.volces.com/api/v3"
else:
    _llm_api_base_default = "https://api.openai.com/v1"
LLM_API_BASE = os.environ.get("LLM_API_BASE", _llm_api_base_default).strip()
LLM_MODEL = (
    os.environ.get("LLM_MODEL", "").strip()
    or os.environ.get("DOUBAO_MODEL", "").strip()
    or "gpt-4o-mini"
)
LLM_TIMEOUT_SEC = float(os.environ.get("LLM_TIMEOUT_SEC", "60"))
LLM_THINKING = os.environ.get("LLM_THINKING", "disabled").strip().lower()


def llm_prompt_chunk_size() -> int:
    """AI 提示词分块批量：每块最多几句；0 或 ≥ 总句数时整片一次 LLM。默认 4。"""
    raw = os.environ.get("LLM_PROMPT_CHUNK_SIZE", "4").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 4


def llm_prompt_chunk_workers() -> int:
    """单 media_type 内并行块数上限。默认 4（与 image/video 并行合计约 8 路 LLM）。"""
    raw = os.environ.get("LLM_PROMPT_CHUNK_WORKERS", "4").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def llm_prompt_max_attempts() -> int:
    """单块批量提示词 LLM 最大重试次数（含格式纠错轮）。默认 4。"""
    raw = os.environ.get("LLM_PROMPT_MAX_ATTEMPTS", "4").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def llm_prompt_max_concurrency() -> int:
    """提示词 LLM 全进程并发上限（Semaphore）；8 与 chunk=4/4 峰值 LLM 路数对齐。默认 8。"""
    raw = os.environ.get("LLM_PROMPT_MAX_CONCURRENCY", "8").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def llm_split_enabled() -> bool:
    mode = os.environ.get("TEXT_SPLIT_MODE", TEXT_SPLIT_MODE).strip().lower()
    if mode == "rule":
        return False
    if mode in ("llm", "auto"):
        api_key = (
            os.environ.get("LLM_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("ARK_API_KEY", "").strip()
            or LLM_API_KEY
        )
        return bool(api_key)
    return False


def llm_thinking_payload() -> dict[str, str] | None:
    """
    火山方舟深度思考开关。分句任务默认 disabled（seed 模型否则常耗 10–40s）。
    可选：enabled / disabled / auto；设为空字符串则不传 thinking 字段。
    """
    mode = LLM_THINKING
    if not mode or mode == "none":
        return None
    if mode in ("enabled", "disabled", "auto"):
        return {"type": mode}
    return {"type": "disabled"}


def llm_supports_json_mode() -> bool:
    """doubao-seed 等模型不支持 response_format，直接跳过可避免一次 400。"""
    if "volces.com" in LLM_API_BASE or "volcengine" in LLM_API_BASE:
        name = LLM_MODEL.lower()
        if "seed" in name or "thinking" in name:
            return False
    return True


def text_split_mode_label() -> str:
    if llm_split_enabled():
        return f"LLM 语义分句（{LLM_MODEL}）"
    if TEXT_SPLIT_MODE == "llm" and not LLM_API_KEY:
        return "规则分句（未配置 LLM_API_KEY）"
    return "规则分句（。！？；）"


def pre_split_long_segments_enabled() -> bool:
    """
    是否在 TTS 前对超长含逗号句做源头预切分。
    默认关闭；成片流程仅使用 LLM 语义分句结果，不再做预切分。
    PRE_SPLIT_LONG_SEGMENTS=1 可强制开启（调试/兼容旧流程）。
    """
    mode = os.environ.get("PRE_SPLIT_LONG_SEGMENTS", "off").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    return False


def pre_split_max_chars() -> int:
    """超长句拦截线：默认与单行字幕上限一致（竖屏约 12 字），可通过 PRE_SPLIT_MAX_CHARS 覆盖。"""
    from videoaudiotext.config.subtitle import scaled_subtitle_cue_max_chars

    explicit = os.environ.get("PRE_SPLIT_MAX_CHARS")
    if explicit:
        return max(6, int(explicit))
    return scaled_subtitle_cue_max_chars()


def pre_split_short_comma_trim_chars() -> int:
    """预切分后短句（≤此字数）去掉末尾全角逗号，避免屏上「你瞧啊，」排版。"""
    from videoaudiotext.config.subtitle import scaled_comma_short_opening_max_chars

    explicit = os.environ.get("PRE_SPLIT_SHORT_COMMA_TRIM")
    if explicit:
        return max(2, int(explicit))
    return max(3, int(round(scaled_comma_short_opening_max_chars())))


def pre_split_emotional_keep_max_chars() -> int:
    """强情绪短尾（≤此字且以！？结尾）整段不切，防闪条；默认 8 字（如「太棒啦！」）。"""
    explicit = os.environ.get("PRE_SPLIT_EMOTIONAL_KEEP_MAX")
    if explicit:
        return max(4, int(explicit))
    return 8


def pre_split_tail_merge_max_chars() -> int:
    """切分后尾句 ≤ 此字数则并回上一段（如「太棒啦！」）。"""
    explicit = os.environ.get("PRE_SPLIT_TAIL_MERGE_MAX")
    if explicit:
        return max(2, int(explicit))
    return 5


def pre_split_flash_incoming_max_chars() -> int:
    """外层列表级极短情绪尾句（≤此字）并入上一段，默认 4 字。"""
    explicit = os.environ.get("PRE_SPLIT_FLASH_INCOMING_MAX")
    if explicit:
        return max(2, int(explicit))
    return 4


def pre_split_min_part_chars() -> int:
    """标点切分得分：任一侧 ≤ 此字数则 score=-inf（不依赖词表）。"""
    from videoaudiotext.config.subtitle import PRE_SPLIT_MIN_PART_CHARS

    explicit = os.environ.get("PRE_SPLIT_MIN_PART_CHARS")
    if explicit:
        return max(1, int(explicit))
    return PRE_SPLIT_MIN_PART_CHARS


def pre_split_score_base() -> int:
    from videoaudiotext.config.subtitle import PRE_SPLIT_SCORE_BASE

    explicit = os.environ.get("PRE_SPLIT_SCORE_BASE")
    if explicit:
        return max(10, int(explicit))
    return PRE_SPLIT_SCORE_BASE


def pre_split_parallel_max_chars() -> int:
    from videoaudiotext.config.subtitle import PRE_SPLIT_PARALLEL_MAX_CHARS

    explicit = os.environ.get("PRE_SPLIT_PARALLEL_MAX_CHARS")
    if explicit:
        return max(2, int(explicit))
    return PRE_SPLIT_PARALLEL_MAX_CHARS


def pre_split_min_est_duration_sec() -> float:
    from videoaudiotext.config.subtitle import PRE_SPLIT_MIN_EST_DURATION_SEC

    explicit = os.environ.get("PRE_SPLIT_MIN_EST_DURATION_SEC")
    if explicit:
        return max(0.5, float(explicit))
    return PRE_SPLIT_MIN_EST_DURATION_SEC


def pre_split_time_merge_max_chars() -> int:
    from videoaudiotext.config.subtitle import PRE_SPLIT_TIME_MERGE_MAX_CHARS

    explicit = os.environ.get("PRE_SPLIT_TIME_MERGE_MAX_CHARS")
    if explicit:
        return max(8, int(explicit))
    return PRE_SPLIT_TIME_MERGE_MAX_CHARS


def segment_merge_enabled() -> bool:
    """
    是否对相邻短句做 TTS 探测合并（merge_short_segments）。
    LLM 语义分句：大模型输出原样送 TTS，绝不合并（与字幕分步设置无关）。
    """
    from videoaudiotext.config.text_split import llm_split_enabled

    if llm_split_enabled():
        return False
    mode = os.environ.get("SEGMENT_MERGE", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return True
