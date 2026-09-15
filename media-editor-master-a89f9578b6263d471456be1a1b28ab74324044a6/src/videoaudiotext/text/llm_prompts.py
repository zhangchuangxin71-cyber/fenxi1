"""LLM prompt expansion for AI image / video generation from pipeline segments."""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Sequence

from videoaudiotext.config.text_split import (
    llm_prompt_chunk_size,
    llm_prompt_chunk_workers,
    llm_prompt_max_attempts,
    llm_prompt_max_concurrency,
)
from videoaudiotext.text.llm_split import _chat_completion

_prompt_llm_sem: threading.Semaphore | None = None
_prompt_llm_sem_limit = 0

IMAGE_NEGATIVE = (
    "五官扭曲, 手部畸形, 手指残缺错乱, 肢体穿模重叠, 画面模糊, 低分辨率, "
    "噪点严重, 水印文字, 多余杂物, 色彩脏污, 过曝死黑, 崩坏人体, "
    "杂乱背景, 诡异变形, 重影, 锯齿边缘"
)

VIDEO_NEGATIVE = (
    "手部畸形, 手指错乱, 五官扭曲, 脸部变形, 肢体穿模穿插, 画面闪烁抖动, "
    "动作僵硬卡顿, 人脸模糊重影, 低画质噪点, 暗部死黑, 高光过曝, 水印文字, "
    "多余路人杂物, 色彩脏污, 诡异表情, 画面撕裂失真"
)

# 慢节奏题材（古城/美食/纪录片等）默认电影参数，批量时写入 film_look 一次复用
DEFAULT_FILM_LOOK = (
    "35mm胶片镜头, 轻微胶片颗粒, 浅景深虚化, 柔和阴影, 低对比度暖色调"
)

# 视频每段正向末尾统一追加（防动作崩坏；由后处理注入，LLM 正文勿重复）
VIDEO_STABILITY_SUFFIX = (
    "丝滑连贯动态, 画面稳定无闪烁, 物体纹理清晰, 轻微自然动态"
)

_SLOW_STYLE_KEYWORDS = frozenset(
    {"电影", "古风", "国风", "古城", "街巷", "纪录片", "纪实", "胶片", "烟火"}
)

# 未传 global_style 时从池中随机选取（split API 默认行为）
DEFAULT_GLOBAL_STYLES: tuple[str, ...] = (
    "电影质感，深夜烟火气，暖色调纪录片",
    "纪实风格，匠人特写，暖色室内光",
    "古风国风，水墨意境，柔和自然光",
    "古城街巷，黄昏暖光，纪录片氛围",
    "清新田园，明亮自然光，浅景深",
    "胶片纪实，35mm质感，低饱和暖色",
    "现代都市，冷色调霓虹，简洁构图",
    "科技未来感，蓝紫色调，干净画面",
)


def pick_default_global_style() -> str:
    return random.choice(DEFAULT_GLOBAL_STYLES)

_FAST_CAMERA_FORBIDDEN = re.compile(
    r"快移|快切|快推|快拉|快速|急速|环绕(?=.*(?:全景|长|慢)?)|360"
)

# 从段内 positive 剥离 film_look（全局 film_look 字段已承载，段内禁止重复）
_FILM_LOOK_FRAGMENT = re.compile(
    r"[，,]\s*(?:35mm胶片镜头|35mm镜头|轻微胶片颗粒|浅景深(?:虚化)?|"
    r"柔和阴影|低对比度暖色调)[^，,]*",
    re.IGNORECASE,
)

# 剥离段内重复的「电影质感/超高清」等空泛套话（保留画幅与稳定后缀）
_QUALITY_BOILERPLATE = re.compile(
    r"[，,]\s*(?:超高清|高清|8K|细节丰富|细腻材质|干净画面|无颗粒|电影质感)[^，,]*",
    re.IGNORECASE,
)

_STABILITY_PARTS = tuple(VIDEO_STABILITY_SUFFIX.replace(" ", "").split(","))


def _is_slow_pace_style(global_style: str) -> bool:
    text = (global_style or "").strip()
    return any(kw in text for kw in _SLOW_STYLE_KEYWORDS)


_IMAGE_SYSTEM_CORE = """你是专业 AI 绘图提示词工程师。根据口播分镜句生成可直接投喂文生图模型的提示词。

【输入】
- segment_text：分句原文（保真，不得改写语义）
- search_query：检索锚点词（必须体现在正向 prompt 中）
- global_style：全片锁定画风

【正向 prompt 结构 · 短句逗号分隔 · 每段只写本镜差异】
写作顺序固定：景别/构图 → 主体（差异化）→ 场景与道具 → 光影 → 微动
- 用短句逗号连接，单段 4~6 个短语；禁止一条超长定语堆叠

1. 主体：细化人物年龄、穿搭、神态、道具；人群须区分角色（本地人/游客/手艺人/食客），禁止千人一面
2. 场景：空间环境 + 1~2 个与分句语义贴合的具象道具（如美食句须有蒸笼/竹篮/热气；街巷句可有青石板/木格窗/纸灯笼/旧招牌）
3. 光影：具体光源与色调，禁止只写「暖光」；胶片参数由全局 film_look 承载，段内禁止重复 35mm/颗粒/浅景深
4. 构图：特写/近景/中景/全景 + 三分/中心构图

【保真红线】
- 只扩写可见画面，不新增原文没有的人物、地点、时代、事件
- 道具/氛围元素须与 segment_text 或 search_query 语义相容，禁止无关堆砌
- search_query 关键词必须出现

【禁止重复 · 极重要】
- 段内禁止写 film_look 胶片参数（批量模式由 global film_look 统一承载）
- 段内禁止空泛套话「电影质感/超高清/8K」
- 稳定类后缀（丝滑连贯动态等）由后处理注入，段内勿写

【负面 prompt】
单段模式输出完整反向词；批量模式 per-segment 的 negative_prompt 留空字符串。"""

VIDEO_SYSTEM_CORE = """你是专业 AI 文生视频提示词工程师，熟悉阿里云 Wan2.2 提示词规范。

【输入】
- segment_text / search_query / global_style / duration_sec（可选）

【正向 prompt 顺序 · 运镜前置 · 短句逗号分隔 · 每段只写本镜差异】
写作顺序固定：景别+运镜 → 主体（差异化）→ 场景+道具 → 运动 → 光影
- 优先写场景主体与运镜，再写环境光影/道具；用 4~7 个短短语逗号连接，禁止超长单句

1. 运镜：每段仅 1 种主运镜，须写清速度（慢/极慢/微动）；用通用词：缓慢下移、慢推、慢拉、慢摇、慢移、固定机位；禁止「缓降」等非通用词
2. 主体：人物年龄、穿搭、身份区分；禁止泛化「行人身形微动神态松弛」
3. 场景：分句提到美食/风味/小吃，画面 MUST 含对应食物或器具；提到街巷+美味，须前景美食+远景街巷层次
4. 景别-主体匹配：锅内/碗中/案板上的食物特写，景别用近景或特写，禁止中景/全景（避免画面空泛）
5. 动态：固定机位须有微动（蒸汽升腾、衣角轻摆、光影流动、人群缓行）；短段（≤3s）只用微动/慢镜
6. 光影：具体光源+色调；胶片参数由 global film_look 承载，段内禁止重复

【运镜节奏 · 慢调题材强制】
当 global_style 含电影/古风/国风/古城/纪录片/烟火等慢节奏调性时：
- 只允许：固定机位、慢推、慢拉、慢摇、慢移、微推、缓慢下移
- 严禁：快移、快切、快推、快速横移、特写快切、缓降（改用缓慢下移）
- 竖屏场景严禁长时间环绕/360° orbit；全景用缓慢横摇或慢拉代替环绕

【镜头-文案贴合】
- 抽象总结句（如「风景滋养风味」）仍须落到可见主体：前景美食/器具 + 背景风景
- 静态中景须安排至少 1 项缓动元素，避免完全静止

【保真红线】
- 不新增原文未出现的人物/地点/事件；search_query 关键词必须出现
- 正向 prompt 单段不超过 450 字（Wan2.2 上限约 800 字符）

【禁止重复 · 极重要】
- 段内禁止写 film_look 胶片参数与稳定类后缀（均由后处理 / 全局字段注入）
- 段内禁止空泛套话「电影质感，高清，稳定无闪烁」整段复制
- 稳定后缀由后处理统一追加，LLM 正文专注本镜差异内容

【负面 prompt】
单段模式输出完整反向词；批量模式 per-segment 的 negative_prompt 留空字符串。"""

IMAGE_SYSTEM_PROMPT = (
    _IMAGE_SYSTEM_CORE
    + f"""

【单段输出格式】
只输出纯 JSON：
{{"positive_prompt":"...","negative_prompt":"..."}}
negative_prompt 使用标准库：{IMAGE_NEGATIVE}
"""
)

VIDEO_SYSTEM_PROMPT = (
    VIDEO_SYSTEM_CORE
    + f"""

【单段输出格式】
只输出纯 JSON：
{{"positive_prompt":"...","negative_prompt":"..."}}
negative_prompt 使用标准库：{VIDEO_NEGATIVE}
"""
)

BATCH_SUFFIX = f"""
【批量模式 · 全片一次输出】
用户给出多段分镜 JSON 列表。你必须：
1. 全片输出 **一份** global_negative_prompt（使用标准库，不要改写）
2. 全片输出 **一份** film_look（落地电影参数，如：{DEFAULT_FILM_LOOK}），**禁止**写入各段 positive_prompt
3. prompts 数组每项 **只含本镜差异 positive_prompt**（negative_prompt 固定为 ""）
4. 各段 positive **禁止** 包含：film_look 参数、稳定类后缀（丝滑连贯动态等）——均由后处理统一追加
5. 相邻段运镜须有变化，全部慢节奏；禁止连续两段相同运镜
6. 全片光影色调一致；各段主体/道具/人群须差异化
7. 古城/街巷/美食题材：各段至少 1 个不重复氛围道具
8. 短句逗号结构：景别运镜放最前，主体其次，光影道具放后

只输出纯 JSON：
{{
  "global_negative_prompt": "...",
  "film_look": "...",
  "prompts": [
    {{"positive_prompt": "...", "negative_prompt": ""}},
    ...
  ]
}}
prompts 长度必须与输入段数一致。
global_negative_prompt 标准库（视频）：{VIDEO_NEGATIVE}
global_negative_prompt 标准库（图片）：{IMAGE_NEGATIVE}
"""


@dataclass(frozen=True)
class GenerationPrompt:
    positive_prompt: str
    negative_prompt: str


@dataclass(frozen=True)
class PromptBatchResult:
    """批量生成结果：全局反向词 + 电影参数 + 各段正向。"""

    global_negative_prompt: str
    film_look: str
    prompts: list[GenerationPrompt]


def prompt_system_version_hash() -> str:
    blob = IMAGE_SYSTEM_PROMPT + VIDEO_SYSTEM_PROMPT + BATCH_SUFFIX
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def aspect_ratio_label(
    resolution_mode: str,
    custom_width: float = 1080,
    custom_height: float = 1920,
) -> str:
    mode = (resolution_mode or "").strip().lower()
    if mode in {"1080x1920", "720x1280", "1080×1920"}:
        return "9:16竖屏"
    if mode in {"1920x1080", "1280x720", "1920×1080"}:
        return "16:9横屏"
    if mode == "custom":
        w, h = float(custom_width or 1080), float(custom_height or 1920)
        if h > w:
            return "9:16竖屏"
        if w > h:
            return "16:9横屏"
        return "1:1方屏"
    return "9:16竖屏"


def _extract_json_text(content: str) -> str:
    text = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def _resolve_max_attempts(max_attempts: int | None) -> int:
    if max_attempts is not None:
        return max(1, max_attempts)
    return llm_prompt_max_attempts()


def _get_prompt_llm_semaphore() -> threading.Semaphore:
    global _prompt_llm_sem, _prompt_llm_sem_limit
    limit = llm_prompt_max_concurrency()
    if _prompt_llm_sem is None or _prompt_llm_sem_limit != limit:
        _prompt_llm_sem = threading.Semaphore(limit)
        _prompt_llm_sem_limit = limit
    return _prompt_llm_sem


def _chat_completion_prompt(messages: list[dict[str, str]], *, json_mode: bool = True) -> str:
    with _get_prompt_llm_semaphore():
        return _chat_completion(messages, json_mode=json_mode)


def _looks_like_prompt_item(item: Any) -> bool:
    return isinstance(item, dict) and bool(str(item.get("positive_prompt") or "").strip())


def _extract_prompts_raw(data: Any) -> list[Any] | None:
    if isinstance(data, list):
        return data if data else None
    if not isinstance(data, dict):
        return None
    for key in ("prompts", "results", "items", "segments"):
        raw = data.get(key)
        if isinstance(raw, list) and raw:
            return raw
    pos = str(data.get("positive_prompt") or "").strip()
    if pos:
        return [
            {
                "positive_prompt": pos,
                "negative_prompt": str(data.get("negative_prompt") or "").strip(),
            }
        ]
    return None


def _default_negative(media_type: str) -> str:
    return VIDEO_NEGATIVE if (media_type or "").strip().lower() == "video" else IMAGE_NEGATIVE


def _normalize_camera_terms(text: str) -> str:
    """运镜术语归一：小众词 → 模型易识别通用词。"""
    replacements = (
        ("近景缓降", "近景缓慢下移"),
        ("中景缓降", "中景缓慢下移"),
        ("特写缓降", "特写缓慢下移"),
        ("缓降镜头", "缓慢下移"),
        ("缓降", "缓慢下移"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _strip_inline_film_look(text: str) -> str:
    while _FILM_LOOK_FRAGMENT.search(text):
        text = _FILM_LOOK_FRAGMENT.sub("", text)
    return text


def _strip_stability_duplicates(text: str) -> str:
    for part in _STABILITY_PARTS:
        text = re.sub(rf"[，,]\s*{re.escape(part)}[^，,]*", "", text)
    return text


def _finalize_positive_prompt(
    positive: str,
    *,
    media_type: str,
    global_style: str,
) -> str:
    """后处理：剥离全局重复项，归一运镜，统一追加稳定后缀。"""
    text = (positive or "").strip()
    if not text:
        return text

    text = _strip_inline_film_look(text)
    text = _QUALITY_BOILERPLATE.sub("", text)
    text = _strip_stability_duplicates(text)
    text = _normalize_camera_terms(text)

    if _is_slow_pace_style(global_style) and _FAST_CAMERA_FORBIDDEN.search(text):
        text = _FAST_CAMERA_FORBIDDEN.sub("慢摇", text)
        text = text.replace("环绕", "缓慢横摇")

    text = re.sub(r"[，,]{2,}", "，", text).strip("，")

    kind = (media_type or "image").strip().lower()
    if kind == "video" and "丝滑连贯动态" not in text.replace(" ", ""):
        text = f"{text}，{VIDEO_STABILITY_SUFFIX}"

    return re.sub(r"[，,]{2,}", "，", text).strip("，")


def _sanitize_positive_prompt(
    positive: str,
    *,
    media_type: str = "video",
    global_style: str,
    segment_index: int = 1,
    total_segments: int = 1,
    is_batch: bool = False,
) -> str:
    """兼容旧调用名；统一走 _finalize_positive_prompt。"""
    del segment_index, total_segments, is_batch
    return _finalize_positive_prompt(
        positive,
        media_type=media_type,
        global_style=global_style,
    )


def parse_prompt_response(content: str, *, media_type: str = "image") -> GenerationPrompt:
    data: Any = json.loads(_extract_json_text(content))
    if not isinstance(data, dict):
        raise ValueError("提示词 JSON 根对象无效")
    pos = str(data.get("positive_prompt") or "").strip()
    neg = str(data.get("negative_prompt") or "").strip()
    if not pos:
        raise ValueError("缺少 positive_prompt")
    if not neg:
        neg = _default_negative(media_type)
    return GenerationPrompt(positive_prompt=pos, negative_prompt=neg)


def parse_batch_prompt_response(
    content: str,
    *,
    media_type: str = "image",
    global_style: str = "电影质感",
    expected_count: int | None = None,
) -> PromptBatchResult:
    data: Any = json.loads(_extract_json_text(content))
    if not isinstance(data, dict):
        raise ValueError("批量提示词 JSON 无效")

    global_neg = str(
        data.get("global_negative_prompt") or data.get("negative_prompt") or ""
    ).strip()
    if not global_neg:
        global_neg = _default_negative(media_type)

    film_look = str(data.get("film_look") or DEFAULT_FILM_LOOK).strip()

    raw = _extract_prompts_raw(data)
    if not raw:
        raise ValueError("缺少 prompts 数组")

    positives: list[GenerationPrompt] = []
    total = expected_count or len(raw)
    for i, item in enumerate(raw):
        if not _looks_like_prompt_item(item):
            raise ValueError("prompts 项格式无效")
        pos = str(item.get("positive_prompt") or "").strip()
        if not pos:
            raise ValueError(f"第 {i + 1} 段 positive_prompt 为空")
        pos = _sanitize_positive_prompt(
            pos,
            media_type=media_type,
            global_style=global_style,
            segment_index=i + 1,
            total_segments=total,
            is_batch=True,
        )
        positives.append(GenerationPrompt(positive_prompt=pos, negative_prompt=""))

    if expected_count is not None and len(positives) != expected_count:
        raise ValueError(
            f"返回 {len(positives)} 条提示词，与 {expected_count} 段分句不一致"
        )

    return PromptBatchResult(
        global_negative_prompt=global_neg,
        film_look=film_look,
        prompts=positives,
    )


def merge_batch_prompt_results(results: Sequence[PromptBatchResult]) -> PromptBatchResult:
    if not results:
        raise ValueError("无批量提示词结果可合并")
    if len(results) == 1:
        return results[0]
    prompts: list[GenerationPrompt] = []
    for batch in results:
        prompts.extend(batch.prompts)
    first = results[0]
    return PromptBatchResult(
        global_negative_prompt=first.global_negative_prompt,
        film_look=first.film_look,
        prompts=prompts,
    )


def _build_user_payload(
    segment_text: str,
    *,
    search_query: str | None,
    global_style: str,
    duration_sec: float | None = None,
    segment_index: int | None = None,
    total_segments: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "segment_text": segment_text.strip(),
        "search_query": (search_query or "").strip() or None,
        "global_style": (global_style or "电影质感").strip(),
    }
    if duration_sec is not None and duration_sec > 0:
        payload["duration_sec"] = round(float(duration_sec), 2)
    if segment_index is not None:
        payload["segment_index"] = segment_index
    if total_segments is not None:
        payload["total_segments"] = total_segments
    if _is_slow_pace_style(global_style or ""):
        payload["pace"] = "slow_cinematic"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def generate_segment_prompt(
    segment_text: str,
    *,
    media_type: str = "image",
    search_query: str | None = None,
    global_style: str = "电影质感",
    duration_sec: float | None = None,
    max_attempts: int = 2,
) -> GenerationPrompt:
    if not (segment_text or "").strip():
        raise ValueError("分句内容为空")
    kind = (media_type or "image").strip().lower()
    system = VIDEO_SYSTEM_PROMPT if kind == "video" else IMAGE_SYSTEM_PROMPT
    user = _build_user_payload(
        segment_text,
        search_query=search_query,
        global_style=global_style,
        duration_sec=duration_sec if kind == "video" else None,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            content = _chat_completion(messages, json_mode=True)
            pr = parse_prompt_response(content, media_type=kind)
            pos = _sanitize_positive_prompt(
                pr.positive_prompt,
                media_type=kind,
                global_style=global_style,
                segment_index=1,
                total_segments=1,
                is_batch=False,
            )
            return GenerationPrompt(positive_prompt=pos, negative_prompt=pr.negative_prompt)
        except Exception as exc:
            last_err = exc
            if attempt < max_attempts:
                continue
    raise RuntimeError(f"提示词生成失败: {last_err}") from last_err


def _batch_retry_user_message(*, expected_count: int, error: Exception) -> str:
    return (
        f"上次输出无法解析：{error}。"
        f"请严格只输出纯 JSON，根对象必须包含 global_negative_prompt、film_look、"
        f"prompts（数组，长度必须等于 {expected_count}，每项含 positive_prompt 与 negative_prompt）。"
        "禁止省略 prompts 或使用单段 positive_prompt 替代。"
    )


def generate_batch_prompts(
    segments: Sequence[str],
    *,
    media_type: str = "image",
    search_queries: Sequence[str | None] | None = None,
    global_style: str = "电影质感",
    durations_sec: Sequence[float | None] | None = None,
    max_attempts: int | None = None,
    index_offset: int = 0,
    total_segments: int | None = None,
) -> PromptBatchResult:
    if not segments:
        raise ValueError("无分句可生成")
    kind = (media_type or "image").strip().lower()
    system = (VIDEO_SYSTEM_PROMPT if kind == "video" else IMAGE_SYSTEM_PROMPT) + BATCH_SUFFIX
    n = len(segments)
    total = total_segments if total_segments is not None else n
    items: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        sq = None
        if search_queries is not None and i < len(search_queries):
            sq = search_queries[i]
        dur = None
        if durations_sec is not None and i < len(durations_sec):
            dur = durations_sec[i]
        item: dict[str, Any] = {
            "index": index_offset + i + 1,
            "total": total,
            "segment_text": str(seg).strip(),
            "search_query": (sq or "").strip() or None,
        }
        if kind == "video" and dur is not None and dur > 0:
            item["duration_sec"] = round(float(dur), 2)
        items.append(item)
    user = json.dumps(
        {
            "global_style": (global_style or "电影质感").strip(),
            "pace": "slow_cinematic" if _is_slow_pace_style(global_style) else "normal",
            "segments": items,
        },
        ensure_ascii=False,
        indent=2,
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    attempts = _resolve_max_attempts(max_attempts)
    last_err: Exception | None = None
    last_content = ""
    for attempt in range(1, attempts + 1):
        try:
            last_content = _chat_completion_prompt(messages, json_mode=True)
            return parse_batch_prompt_response(
                last_content,
                media_type=kind,
                global_style=global_style,
                expected_count=n,
            )
        except Exception as exc:
            last_err = exc
            if attempt < attempts:
                messages = [
                    *messages,
                    {"role": "assistant", "content": last_content or "{}"},
                    {
                        "role": "user",
                        "content": _batch_retry_user_message(expected_count=n, error=exc),
                    },
                ]
                continue
    raise RuntimeError(f"批量提示词生成失败: {last_err}") from last_err


def generate_batch_prompts_chunked(
    segments: Sequence[str],
    *,
    media_type: str = "image",
    search_queries: Sequence[str | None] | None = None,
    global_style: str = "电影质感",
    durations_sec: Sequence[float | None] | None = None,
    max_attempts: int | None = None,
    chunk_size: int | None = None,
    chunk_workers: int | None = None,
) -> PromptBatchResult:
    """分块批量生成：句数超过 chunk_size 时按块并行 LLM，块内仍走批量模式。"""
    n = len(segments)
    if not n:
        raise ValueError("无分句可生成")

    size = llm_prompt_chunk_size() if chunk_size is None else max(0, chunk_size)
    if size <= 0 or n <= size:
        return generate_batch_prompts(
            segments,
            media_type=media_type,
            search_queries=search_queries,
            global_style=global_style,
            durations_sec=durations_sec,
            max_attempts=max_attempts,
        )

    chunk_ranges = [(start, min(start + size, n)) for start in range(0, n, size)]
    attempts = _resolve_max_attempts(max_attempts)

    def _run_chunk(start: int, end: int, *, extra_attempts: int = 0) -> PromptBatchResult:
        sq = search_queries[start:end] if search_queries is not None else None
        dur = durations_sec[start:end] if durations_sec is not None else None
        return generate_batch_prompts(
            segments[start:end],
            media_type=media_type,
            search_queries=sq,
            global_style=global_style,
            durations_sec=dur,
            max_attempts=attempts + extra_attempts,
            index_offset=start,
            total_segments=n,
        )

    workers = llm_prompt_chunk_workers() if chunk_workers is None else max(1, chunk_workers)

    def _run_all(parallel_workers: int) -> list[PromptBatchResult]:
        batches: list[PromptBatchResult | None] = [None] * len(chunk_ranges)
        failed: list[tuple[int, int, int]] = []

        if len(chunk_ranges) == 1 or parallel_workers <= 1:
            for idx, (start, end) in enumerate(chunk_ranges):
                try:
                    batches[idx] = _run_chunk(start, end)
                except Exception:
                    failed.append((idx, start, end))
        else:
            with ThreadPoolExecutor(
                max_workers=min(parallel_workers, len(chunk_ranges)),
                thread_name_prefix="ai-prompt-chunk",
            ) as pool:
                future_map = {
                    pool.submit(_run_chunk, start, end): idx
                    for idx, (start, end) in enumerate(chunk_ranges)
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        batches[idx] = future.result()
                    except Exception:
                        start, end = chunk_ranges[idx]
                        failed.append((idx, start, end))

        for idx, start, end in failed:
            batches[idx] = _run_chunk(start, end, extra_attempts=2)

        if any(batch is None for batch in batches):
            raise RuntimeError("分块提示词生成失败：存在未完成的块")
        return [batch for batch in batches if batch is not None]

    try:
        return merge_batch_prompt_results(_run_all(workers))
    except Exception:
        if workers <= 1:
            raise
        return merge_batch_prompt_results(_run_all(1))


def generate_image_video_prompts_parallel(
    segments: Sequence[str],
    *,
    search_queries: Sequence[str | None] | None = None,
    global_style: str = "电影质感",
    durations_sec: Sequence[float | None] | None = None,
    max_attempts: int | None = None,
    chunk_size: int | None = None,
    chunk_workers: int | None = None,
) -> tuple[PromptBatchResult, PromptBatchResult]:
    """image / video 分块批量并行：各 media_type 内按 chunk 并行 LLM，两类之间也并行。"""
    if not segments:
        raise ValueError("无分句可生成")

    def _run_one(media_type: str, *, workers: int | None) -> PromptBatchResult:
        return generate_batch_prompts_chunked(
            segments,
            media_type=media_type,
            search_queries=search_queries,
            global_style=global_style,
            durations_sec=durations_sec,
            max_attempts=max_attempts,
            chunk_size=chunk_size,
            chunk_workers=workers,
        )

    def _run_both(workers: int | None) -> tuple[PromptBatchResult, PromptBatchResult]:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai-prompt") as pool:
            image_future = pool.submit(_run_one, "image", workers=workers)
            video_future = pool.submit(_run_one, "video", workers=workers)
            return image_future.result(), video_future.result()

    workers = chunk_workers if chunk_workers is not None else llm_prompt_chunk_workers()
    try:
        return _run_both(workers)
    except Exception:
        if workers <= 1:
            raise
        return _run_both(1)


def format_single_prompt_markdown(
    segment_text: str,
    prompt: GenerationPrompt,
    *,
    index: int = 1,
    media_type: str = "image",
    show_negative: bool = True,
) -> str:
    kind = "视频" if (media_type or "").strip().lower() == "video" else "图片"
    snippet = segment_text.strip().replace("\n", " ")
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"
    body = (
        f"### 第 {index} 段 · {kind}提示词\n"
        f"**分句**：{snippet}\n\n"
        f"**正向**\n\n{prompt.positive_prompt}\n"
    )
    if show_negative and prompt.negative_prompt:
        body += f"\n**反向**\n\n{prompt.negative_prompt}"
    return body


def format_all_prompts_markdown(
    segments: Sequence[str],
    batch: PromptBatchResult,
    *,
    media_type: str = "image",
    global_style: str = "",
) -> str:
    kind = "视频" if (media_type or "").strip().lower() == "video" else "图片"
    lines = [
        f"## AI 生{kind}提示词（共 {len(batch.prompts)} 段）",
    ]
    if global_style:
        lines.append(f"**全片风格**：{global_style}")
    if batch.film_look:
        lines.append(f"**全片电影参数**：{batch.film_look}")
    lines.extend(
        [
            "",
            "### 全片反向提示词（共用，勿每段重复）",
            "",
            batch.global_negative_prompt,
            "",
            "---",
            "",
        ]
    )
    for i, (seg, pr) in enumerate(zip(segments, batch.prompts), 1):
        lines.append(
            format_single_prompt_markdown(
                seg, pr, index=i, media_type=media_type, show_negative=False
            )
        )
        lines.append("\n---\n")
    return "\n".join(lines).rstrip("\n-")
