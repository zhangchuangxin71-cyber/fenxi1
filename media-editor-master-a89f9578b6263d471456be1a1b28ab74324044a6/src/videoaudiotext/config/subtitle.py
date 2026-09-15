"""Subtitle layout, scaling, and display settings."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from videoaudiotext.config.output import (
    _SUBTITLE_REF_HEIGHT,
    _SUBTITLE_REF_WIDTH,
    get_output_height,
    get_output_width,
)
from videoaudiotext.config.paths import ROOT


def _resolve_fonts_dir() -> Path:
    """硬字幕 fontsdir；Docker bind mount 时设 FONTS_DIR=/app/fonts。"""
    raw = os.environ.get("FONTS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return ROOT / "fonts"


FONTS_DIR = _resolve_fonts_dir()

# 手工维护的常用别名（小写键 → ASS family 名）；fonts/ 扫描结果会合并覆盖/补充
_SUBTITLE_FONT_ALIASES_STATIC: dict[str, str] = {
    "siyuanheiti": "思源黑体",
    "source han sans sc": "思源黑体",
    "sourcehansanssc": "思源黑体",
    "思源": "思源黑体",
    "思源黑体": "思源黑体",
    "思源黑体 cn": "思源黑体 CN Bold",
    "迷茫体": "新愚公迷茫体",
    "迷茫": "新愚公迷茫体",
    "拼搏体": "新愚公拼搏体",
    "拼搏": "新愚公拼搏体",
    "霞鹜文楷": "LXGW WenKai Medium",
    "文楷": "LXGW WenKai Medium",
    "lxgw wenkai": "LXGW WenKai Medium",
    "星汉等宽": "Milky Han Term CN Heavy",
    "milky": "Milky Han Term CN Heavy",
    "沐瑶": "Muyao-Softbrush",
    "手写体": "Muyao-Softbrush",
    "沐瑶软笔手写体": "Muyao-Softbrush",
    "像素": "IPix",
    "ipix": "IPix",
    "中文像素字体": "IPix",
    "扁桃体": "字魂扁桃体",
    "字魂扁桃体": "字魂扁桃体",
    "竹石体": "YRDZST-Medium",
    "杨任东竹石体": "YRDZST-Medium",
    "fandolhei": "FandolHei",
    "fandolkai": "FandolKai",
    "fandolsong": "FandolSong",
}

_FONT_ALIASES_CACHE: dict[str, str] | None = None


def _alias_keys_from_filename(stem: str) -> set[str]:
    keys: set[str] = {stem.strip()}
    if "（" in stem:
        keys.add(stem.split("（", 1)[0].strip())
    if "(" in stem:
        keys.add(stem.split("(", 1)[0].strip())
    for prefix in ("新愚公", "字魂", "_思源黑体"):
        if stem.startswith(prefix) and len(stem) > len(prefix):
            keys.add(stem[len(prefix) :].strip())
    for suffix in ("-Medium", "-Bold", "-Regular", " Medium", " Bold"):
        if stem.endswith(suffix):
            keys.add(stem[: -len(suffix)].strip())
    return {k for k in keys if k}


def _build_subtitle_font_alias_map() -> dict[str, str]:
    merged = {k.lower(): v for k, v in _SUBTITLE_FONT_ALIASES_STATIC.items()}
    for path in _iter_font_files():
        canonical = _libass_name_for_font_file(path)
        if not canonical:
            continue
        merged[canonical.lower()] = canonical
        for key in _alias_keys_from_filename(path.stem):
            merged.setdefault(key.lower(), canonical)
    return merged


def _subtitle_font_alias_map() -> dict[str, str]:
    global _FONT_ALIASES_CACHE
    if _FONT_ALIASES_CACHE is None:
        _FONT_ALIASES_CACHE = _build_subtitle_font_alias_map()
    return _FONT_ALIASES_CACHE


def invalidate_subtitle_font_alias_cache() -> None:
    global _FONT_ALIASES_CACHE
    _FONT_ALIASES_CACHE = None


def subtitle_font_alias_entries() -> list[dict[str, str]]:
    """返回手工维护的常用别名（不含文件名自动推导），供调试。"""
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, str]] = []
    for alias, font_name in sorted(_SUBTITLE_FONT_ALIASES_STATIC.items(), key=lambda x: (x[1], x[0])):
        if alias.lower() == font_name.lower():
            continue
        pair = (alias, font_name)
        if pair in seen:
            continue
        seen.add(pair)
        entries.append({"alias": alias, "font_name": font_name})
    return entries


def _is_cjk_text(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _preferred_font_picker_value(canonical: str, filename_stem: str) -> str:
    """为前端下拉框挑选最短的可用中文别名；无则退回 canonical。"""
    candidates: list[str] = []
    for alias, target in _SUBTITLE_FONT_ALIASES_STATIC.items():
        if target == canonical:
            candidates.append(alias)
    for key in _alias_keys_from_filename(filename_stem):
        if key != canonical:
            candidates.append(key)
    cjk = [c for c in candidates if _is_cjk_text(c)]
    if cjk:
        with_ti = [c for c in cjk if c.endswith("体")]
        pool = with_ti if with_ti else cjk
        return min(pool, key=len)
    if _is_cjk_text(canonical):
        return canonical
    return canonical


def subtitle_font_picker_choices() -> list[dict[str, str]]:
    """前端字体下拉：name 用于展示并提交，ass_font_name 为解析后的 ASS 字体名。"""
    choices: list[dict[str, str]] = []
    for path in _iter_font_files():
        canonical = _libass_name_for_font_file(path)
        if not canonical:
            continue
        name = _preferred_font_picker_value(canonical, path.stem)
        choices.append(
            {
                "name": name,
                "ass_font_name": canonical,
            }
        )
    default = SUBTITLE_FONT_NAME if SUBTITLE_FONT_NAME else "思源黑体"

    def _sort_key(item: dict[str, str]) -> tuple:
        return (item["ass_font_name"] != default, item["name"].lower())

    return sorted(choices, key=_sort_key)


def _resolve_subtitle_font_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return "思源黑体"
    am = _subtitle_font_alias_map()
    key = raw.lower()
    if key in am:
        return _finalize_libass_font_name(am[key])
    canonical_names = set(am.values())
    if key in {n.lower() for n in canonical_names}:
        for n in canonical_names:
            if n.lower() == key:
                return _finalize_libass_font_name(n)
    suffix_hits = [
        n for n in canonical_names if n == raw or n.endswith(raw) or raw in n
    ]
    if len(suffix_hits) == 1:
        return _finalize_libass_font_name(suffix_hits[0])
    return _finalize_libass_font_name(raw)


# fonts/ 各文件在 Docker 无 fontconfig 时 libass fontsdir 实测可匹配的 ASS 名
_LIBASS_FONT_FILE_CANONICAL: dict[str, str] = {
    "FandolHei-Regular.otf": "FandolHei",
    "FandolKai-Regular.otf": "FandolKai",
    "FandolSong-Bold.otf": "FandolSong-Bold",
    "SourceHanSansSC-Bold.otf": "思源黑体",
    "_思源黑体SourceHanSansCN-Bold.otf": "思源黑体 CN Bold",
    "中文像素字体(IPix).ttf": "IPix",
    "字魂扁桃体.ttf": "字魂扁桃体",
    "新愚公拼搏体.ttf": "新愚公拼搏体",
    "新愚公迷茫体.ttf": "新愚公迷茫体",
    "星汉等宽(milky-term-cn-heavyitalic).ttf": "Milky Han Term CN Heavy",
    "杨任东竹石体-Medium.ttf": "YRDZST-Medium",
    "沐瑶软笔手写体(Muyao-Softbrush).ttf": "Muyao-Softbrush",
    "王汉宗中行书繁.ttf": "王漢宗中行書繁",
    "王汉宗波卡体-空阴.ttf": "王漢宗波卡體一空陰",
    "王汉宗超黑体俏皮动物.ttf": "王漢宗超黑體俏皮動物一",
    "王汉宗颜楷体繁.ttf": "王漢宗顏楷體繁",
    "霞鹜文楷（LXGWWenKai-Medium）.ttf": "LXGW WenKai Medium",
}

# 旧版/短族名 → libass fontsdir 可匹配名（前端缓存 ass_font_name 时兜底）
_LIBASS_NAME_UPGRADES: dict[str, str] = {
    "杨任东竹石体": "YRDZST-Medium",
    "杨任东竹石体-medium": "YRDZST-Medium",
    "yrdzst": "YRDZST-Medium",
    "yrdzst medium": "YRDZST-Medium",
    "霞鹜文楷": "LXGW WenKai Medium",
    "霞鹜文楷 中粗": "LXGW WenKai Medium",
    "lxgw wenkai": "LXGW WenKai Medium",
    "lxgwwenkai-medium": "LXGW WenKai Medium",
    "milky han term cn": "Milky Han Term CN Heavy",
    "思源黑体 cn": "思源黑体 CN Bold",
    "source han sans cn": "思源黑体 CN Bold",
    "source han sans cn bold": "思源黑体 CN Bold",
    "source han sans sc bold": "思源黑体",
    "xin yugong zhuangjia song": "新愚公拼搏体",
    "xinyugongzhuangjiasong": "新愚公拼搏体",
}

_GENERIC_LIBASS_NAMES = frozenset(
    {
        "regular",
        "bold",
        "italic",
        "medium",
        "light",
        "semibold",
        "常规体",
        "<字体子系>",
    }
)


def _finalize_libass_font_name(name: str) -> str:
    """最后一道兜底：把仍可能匹配失败的短族名升级为实测可用的完整名。"""
    raw = (name or "").strip()
    if not raw:
        return "思源黑体"
    upgraded = _LIBASS_NAME_UPGRADES.get(raw.lower())
    if upgraded:
        return upgraded
    if raw.lower() in _GENERIC_LIBASS_NAMES:
        return "思源黑体"
    if " bold bold" in raw.lower():
        return raw.replace(" Bold Bold", " Bold").replace(" bold bold", " bold")
    return raw


def _iter_font_files() -> list[Path]:
    if not FONTS_DIR.is_dir():
        return []
    files = sorted(FONTS_DIR.glob("*.[ot]tf"), key=lambda p: p.name.lower())
    if files:
        return files
    return sorted(FONTS_DIR.rglob("*.[ot]tf"), key=lambda p: str(p).lower())


def _libass_name_for_font_file(path: Path) -> str:
    """返回单个字体文件在 libass fontsdir 模式下应写入 ASS 的 Fontname。"""
    override = _LIBASS_FONT_FILE_CANONICAL.get(path.name)
    if override:
        return override
    picked = _pick_ass_font_name(_family_names_from_font_file(path))
    return _finalize_libass_font_name(picked) if picked else ""


def _family_names_from_font_file(path: Path) -> list[str]:
    names: list[str] = []
    try:
        out = subprocess.run(
            ["fc-scan", "--format", "%{family}\n", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            names = [
                name.strip()
                for name in out.stdout.strip().split(",")
                if name.strip()
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(path)
        seen: set[str] = set()
        for rec in font["name"].names:
            if rec.nameID not in (1, 4):
                continue
            name = rec.toUnicode().strip()
            if not name or name in seen:
                continue
            low = name.lower()
            if low in _GENERIC_LIBASS_NAMES:
                continue
            if " bold bold" in low:
                continue
            seen.add(name)
            names.append(name)
    except Exception:
        pass
    if names:
        return names
    return names


_ASS_FONT_WEIGHT_MARKERS = (
    "Medium",
    "Bold",
    "Heavy",
    "Light",
    "Regular",
    "Italic",
    "Semibold",
    "中粗",
    "粗体",
    " CN Bold",
    "Bold Italic",
    "Heavy Italic",
)


def _pick_ass_font_name(families: list[str]) -> str:
    """
    选取 libass fontsdir 可匹配的 ASS Fontname。

    Docker 内无 fontconfig 时，libass 只认字体 name 表中带字重的完整 family 名
    （如「霞鹜文楷 中粗」「Milky Han Term CN Heavy」），短族名会回退到仅含拉丁的系统字体，
    表现为英文正常、中文方框。
    """
    usable = [
        name
        for name in families
        if name.strip() and name.strip().lower() not in _GENERIC_LIBASS_NAMES
        and " bold bold" not in name.lower()
    ]
    if not usable:
        return ""
    if len(usable) == 1:
        return usable[0]
    weighted = [
        name
        for name in usable
        if any(marker in name for marker in _ASS_FONT_WEIGHT_MARKERS)
    ]
    pool = weighted if weighted else usable
    cjk_pool = [name for name in pool if _is_cjk_text(name)]
    if cjk_pool:
        return max(cjk_pool, key=len)
    return max(pool, key=len)


@dataclass(frozen=True)
class SubtitleFontChoice:
    name: str
    label: str
    filename: str


def discover_subtitle_fonts() -> list[SubtitleFontChoice]:
    """扫描 fonts/ 下可用字幕字体（按 ASS/libass family 名去重）。"""
    seen: dict[str, SubtitleFontChoice] = {}
    for path in _iter_font_files():
        name = _libass_name_for_font_file(path)
        if not name or name in seen:
            continue
        seen[name] = SubtitleFontChoice(
            name=name,
            label=f"{name} · {path.name}",
            filename=path.name,
        )
    default = SUBTITLE_FONT_NAME
    return sorted(
        seen.values(),
        key=lambda c: (c.name != default, c.name.lower()),
    )


def subtitle_font_dropdown_choices() -> list[tuple[str, str]]:
    discovered = discover_subtitle_fonts()
    if not discovered:
        default = SUBTITLE_FONT_NAME
        return [(default, default)]
    return [(c.label, c.name) for c in discovered]


def default_subtitle_font_dropdown_value() -> str:
    discovered = discover_subtitle_fonts()
    names = {c.name for c in discovered}
    default = SUBTITLE_FONT_NAME
    if default in names:
        return default
    for name in names:
        if name.lower() == default.lower():
            return name
    if discovered:
        return discovered[0].name
    return default


def subtitle_font_setup_warning() -> str:
    """fonts/ 无字体文件时提示（硬字幕可能 fallback 到系统字体）。"""
    if not FONTS_DIR.is_dir():
        return "⚠️ 未找到 fonts/ 目录，硬字幕将使用系统字体。"
    if _iter_font_files():
        return ""
    return "⚠️ fonts/ 为空，建议放入思源黑体（Source Han Sans SC）以确保字幕样式一致。"


SUBTITLE_MODE = "hard"
SUBTITLE_FONT_NAME = _resolve_subtitle_font_name(
    os.environ.get("SUBTITLE_FONT_NAME", "siyuanheiti")
)

_ACTIVE_SUBTITLE_FONT_NAME: str | None = None


def get_subtitle_font_name() -> str:
    if _ACTIVE_SUBTITLE_FONT_NAME:
        return _resolve_subtitle_font_name(_ACTIVE_SUBTITLE_FONT_NAME)
    return SUBTITLE_FONT_NAME


def set_active_subtitle_font_name(name: str | None) -> None:
    """UI/成片运行时字幕字体（None 表示恢复环境变量默认）。"""
    global _ACTIVE_SUBTITLE_FONT_NAME
    if name is None or not str(name).strip():
        _ACTIVE_SUBTITLE_FONT_NAME = None
        return
    _ACTIVE_SUBTITLE_FONT_NAME = _resolve_subtitle_font_name(str(name).strip())
SUBTITLE_SCALE_X = 80
SUBTITLE_SCALE_Y = 80
SUBTITLE_MARGIN_BOTTOM = 64
SUBTITLE_MARGIN_LR = 150
SUBTITLE_WRAP_STYLE = 2
SUBTITLE_SCREEN_MAX_CHARS = 16
SUBTITLE_LINE_MAX_CHARS = 16
SUBTITLE_LANDSCAPE_MAX_CHARS = int(
    os.environ.get("SUBTITLE_LANDSCAPE_MAX_CHARS", "24")
)
SUBTITLE_SQUARE_MAX_CHARS = int(os.environ.get("SUBTITLE_SQUARE_MAX_CHARS", "16"))
SUBTITLE_PORTRAIT_HEIGHT_DIVISOR = float(
    os.environ.get("SUBTITLE_PORTRAIT_HEIGHT_DIVISOR", "24")
)
SUBTITLE_LANDSCAPE_HEIGHT_DIVISOR = float(
    os.environ.get("SUBTITLE_LANDSCAPE_HEIGHT_DIVISOR", "15")
)
SUBTITLE_LANDSCAPE_FONT_MIN = int(os.environ.get("SUBTITLE_LANDSCAPE_FONT_MIN", "66"))
SUBTITLE_SQUARE_HEIGHT_DIVISOR = float(
    os.environ.get("SUBTITLE_SQUARE_HEIGHT_DIVISOR", "19")
)
SUBTITLE_SQUARE_FONT_MIN = int(os.environ.get("SUBTITLE_SQUARE_FONT_MIN", "52"))
SUBTITLE_MAX_LINES = 2
SUBTITLE_MAX_WIDTH_RATIO = float(os.environ.get("SUBTITLE_MAX_WIDTH_RATIO", "0.8"))
SUBTITLE_UNIFORM_FONT = os.environ.get("SUBTITLE_UNIFORM_FONT", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MAX_LINES_PER_CUE = os.environ.get("MAX_LINES_PER_CUE", "").strip()
SUBTITLE_LEAD_IN_SEC = float(os.environ.get("SUBTITLE_LEAD_IN_SEC", "0.07"))
SUBTITLE_POST_HOLD_SEC = float(os.environ.get("SUBTITLE_POST_HOLD_SEC", "0.12"))
SUBTITLE_CUE_MIN_DISPLAY_SEC = float(
    os.environ.get("SUBTITLE_CUE_MIN_DISPLAY_SEC", "0.7")
)
SUBTITLE_POOL_SURPLUS_THRESHOLD = float(
    os.environ.get("SUBTITLE_POOL_SURPLUS_THRESHOLD", "1.5")
)
SUBTITLE_POOL_COMPRESSED_MIN = float(
    os.environ.get("SUBTITLE_POOL_COMPRESSED_MIN", "1.2")
)
COMMA_SHORT_OPENING_MAX_CHARS = int(
    os.environ.get("COMMA_SHORT_OPENING_MAX_CHARS", "4")
)
COMMA_AUDIO_PAUSE_MIN_SEC = float(
    os.environ.get("COMMA_AUDIO_PAUSE_MIN_SEC", "0.15")
)
SUBTITLE_PROGRESSIVE_MIN_SEGMENT_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_MIN_SEGMENT_CHARS", "8")
)
SUBTITLE_PROGRESSIVE_MIN_PART_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_MIN_PART_CHARS", "8")
)
SUBTITLE_PROGRESSIVE_MIN_SCREEN_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_MIN_SCREEN_CHARS", "2")
)
SUBTITLE_PROGRESSIVE_MAX_PARTS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_MAX_PARTS", "2")
)
SUBTITLE_FRAGMENT_MERGE_MAX_CHARS = int(
    os.environ.get("SUBTITLE_FRAGMENT_MERGE_MAX_CHARS", "2")
)
SUBTITLE_END_TRIM_SEC = float(os.environ.get("SUBTITLE_END_TRIM_SEC", "0"))
SUBTITLE_MIN_CUE_SEC = 0.35
SUBTITLE_PROGRESSIVE = True
SUBTITLE_CHUNK_MAX_CHARS = 10
SUBTITLE_PROGRESSIVE_MIN_CHARS = 0
SUBTITLE_CUE_MAX_CHARS = int(os.environ.get("SUBTITLE_CUE_MAX_CHARS", "12"))
SUBTITLE_ASS_MAX_CHARS_PER_LINE = int(
    os.environ.get("SUBTITLE_ASS_MAX_CHARS_PER_LINE", "10")
)
SUBTITLE_CUE_WRAP_MAX_CHARS = int(
    os.environ.get("SUBTITLE_CUE_WRAP_MAX_CHARS", "16")
)
MAX_ALLOW_SINGLE_LINE = int(os.environ.get("MAX_ALLOW_SINGLE_LINE", "15"))
PRE_SPLIT_MIN_PART_CHARS = int(os.environ.get("PRE_SPLIT_MIN_PART_CHARS", "4"))
PRE_SPLIT_SCORE_BASE = int(os.environ.get("PRE_SPLIT_SCORE_BASE", "100"))
PRE_SPLIT_PARALLEL_MAX_CHARS = int(os.environ.get("PRE_SPLIT_PARALLEL_MAX_CHARS", "5"))
PRE_SPLIT_MIN_EST_DURATION_SEC = float(
    os.environ.get("PRE_SPLIT_MIN_EST_DURATION_SEC", "1.5")
)
PRE_SPLIT_TIME_MERGE_MAX_CHARS = int(
    os.environ.get("PRE_SPLIT_TIME_MERGE_MAX_CHARS", "15")
)
TIMELINE_JIEBA_SPLIT_CHARS = int(
    os.environ.get("TIMELINE_JIEBA_SPLIT_CHARS", "16")
)
SUBTITLE_CUE_MIN_CHARS = int(os.environ.get("SUBTITLE_CUE_MIN_CHARS", "7"))
SUBTITLE_CUE_MAX_DURATION = float(os.environ.get("SUBTITLE_CUE_MAX_DURATION", "3.0"))
SUBTITLE_CUE_MIN_DURATION = float(os.environ.get("SUBTITLE_CUE_MIN_DURATION", "1.2"))
SUBTITLE_SPLIT_MIN_CHARS = int(os.environ.get("SUBTITLE_SPLIT_MIN_CHARS", "10"))
SUBTITLE_CUE_WRAPPED_MAX_DURATION = float(
    os.environ.get("SUBTITLE_CUE_WRAPPED_MAX_DURATION", "3.35")
)
SUBTITLE_PARALLEL_MAX_DURATION = float(os.environ.get("SUBTITLE_PARALLEL_MAX_DURATION", "2.0"))
SUBTITLE_CUE_SPLIT_GAP_SEC = float(os.environ.get("SUBTITLE_CUE_SPLIT_GAP_SEC", "0.04"))
SUBTITLE_CPS_MAX = float(os.environ.get("SUBTITLE_CPS_MAX", "6.0"))
SUBTITLE_CPS_ENFORCE = os.environ.get("SUBTITLE_CPS_ENFORCE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
SUBTITLE_BREATH_MIN_SEC = float(os.environ.get("SUBTITLE_BREATH_MIN_SEC", "0.5"))
SUBTITLE_LINE_MAX_DIFF = int(os.environ.get("SUBTITLE_LINE_MAX_DIFF", "6"))
SUBTITLE_CONSTRAINTS_ENABLED = os.environ.get("SUBTITLE_CONSTRAINTS_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
SUBTITLE_MERGE_ENABLED = os.environ.get("SUBTITLE_MERGE_ENABLED", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
SUBTITLE_MERGE_MIN_DURATION = float(
    os.environ.get("SUBTITLE_MERGE_MIN_DURATION", str(SUBTITLE_CUE_MIN_DURATION))
)
SUBTITLE_MERGE_MIN_CHARS = int(
    os.environ.get("SUBTITLE_MERGE_MIN_CHARS", str(SUBTITLE_CUE_MIN_CHARS))
)
SUBTITLE_MERGE_MAX_CHARS = int(
    os.environ.get("SUBTITLE_MERGE_MAX_CHARS", str(SUBTITLE_CUE_MAX_CHARS))
)
CONTENT_SAFE_SUBTITLES = os.environ.get("CONTENT_SAFE_SUBTITLES", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
SUBTITLE_UNIFIED_CANVAS_Y = os.environ.get("SUBTITLE_UNIFIED_CANVAS_Y", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}


def scaled_subtitle_margin_bottom() -> int:
    return max(24, int(SUBTITLE_MARGIN_BOTTOM * get_output_height() / _SUBTITLE_REF_HEIGHT))


def scaled_subtitle_margin_lr() -> int:
    return scaled_subtitle_frame_lr_margin()


def subtitle_max_width_ratio() -> float:
    """字幕区最大占画布宽度比例（默认 0.8）。"""
    explicit = os.environ.get("SUBTITLE_MAX_WIDTH_RATIO")
    ratio = float(explicit) if explicit else SUBTITLE_MAX_WIDTH_RATIO
    return max(0.5, min(0.95, ratio))


def scaled_subtitle_frame_lr_margin() -> int:
    """左右留白（像素），使字幕渲染宽度 ≤ 画布宽 × subtitle_max_width_ratio()。"""
    frame_w = get_output_width()
    side = int(round(frame_w * (1.0 - subtitle_max_width_ratio()) / 2.0))
    return max(16, side)


def _subtitle_effective_width_scale() -> float:
    """折行字数按「画布宽 × 最大宽度比」缩放（仅用于无 margin 约束的旧路径）。"""
    return get_output_width() / _SUBTITLE_REF_WIDTH * subtitle_max_width_ratio()


def scaled_subtitle_renderable_width_px() -> int:
    """ASS 字幕可用宽度（像素）；左右 margin 已将渲染限制在 max_width_ratio 内。"""
    return max(64, get_output_width() - 2 * scaled_subtitle_frame_lr_margin())


_ACTIVE_SUBTITLE_FONT_SCALE = 1.0


def subtitle_font_scale_max() -> float:
    """字号倍率上限（默认 15；可通过环境变量 SUBTITLE_FONT_SCALE_MAX 覆盖）。"""
    raw = os.environ.get("SUBTITLE_FONT_SCALE_MAX", "15").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 15.0


def set_active_subtitle_font_scale(scale: float | None) -> None:
    """UI/成片运行时字幕字号倍率（1.0=自动计算值，不写入环境变量）。"""
    global _ACTIVE_SUBTITLE_FONT_SCALE
    if scale is None:
        _ACTIVE_SUBTITLE_FONT_SCALE = 1.0
        return
    cap = subtitle_font_scale_max()
    _ACTIVE_SUBTITLE_FONT_SCALE = max(0.5, min(cap, float(scale)))


def get_subtitle_font_scale() -> float:
    return _ACTIVE_SUBTITLE_FONT_SCALE


def layout_subtitle_ass_font_size() -> int:
    """折行/渲染共用基准字号（不含 UI 倍率 font_scale）。"""
    explicit = os.environ.get("SUBTITLE_ASS_FONT_SIZE")
    if explicit:
        base = int(explicit)
    else:
        base = auto_subtitle_ass_font_size()
    return max(12, base)


_ACTIVE_SUBTITLE_Y_OFFSET = 0


def clamp_subtitle_y_offset(offset: int) -> int:
    """限制字幕纵向偏移（像素）：正=上移，负=下移。"""
    h = get_output_height()
    max_up = int(h * 0.45)
    max_down = int(h * 0.20)
    return max(-max_down, min(max_up, int(offset)))


def set_active_subtitle_y_offset(offset: int | None) -> None:
    """UI/成片运行时字幕纵向偏移（像素，正=上移）。"""
    global _ACTIVE_SUBTITLE_Y_OFFSET
    if offset is None:
        _ACTIVE_SUBTITLE_Y_OFFSET = 0
        return
    _ACTIVE_SUBTITLE_Y_OFFSET = clamp_subtitle_y_offset(int(offset))


def get_subtitle_y_offset() -> int:
    return _ACTIVE_SUBTITLE_Y_OFFSET


def subtitle_y_offset_step() -> int:
    return max(16, int(get_output_height() * 0.025))


def clamp_y_offset_steps(steps: int) -> int:
    """API 档位限制：正=上移，负=下移。"""
    step_px = subtitle_y_offset_step()
    if step_px <= 0:
        return 0
    h = get_output_height()
    max_up = int(h * 0.45) // step_px
    max_down = int(h * 0.20) // step_px
    return max(-max_down, min(max_up, int(steps)))


def y_offset_steps_to_pixels(steps: int) -> int:
    """Flow B API：档位 → 像素（供 set_active_subtitle_y_offset 使用）。"""
    return clamp_subtitle_y_offset(clamp_y_offset_steps(steps) * subtitle_y_offset_step())


def resolve_style_y_offset_to_pixels(value: int | None) -> int | None:
    """读取 render_style 中的 y_offset：新格式为档位；|v|≥24 视为旧版像素。"""
    if value is None:
        return None
    v = int(value)
    if abs(v) >= 24:
        return clamp_subtitle_y_offset(v)
    return y_offset_steps_to_pixels(v)


def subtitle_y_offset_steps_label(steps: int) -> str:
    s = clamp_y_offset_steps(int(steps))
    if s == 0:
        return "默认"
    if s > 0:
        return f"上移 {s} 档"
    return f"下移 {abs(s)} 档"


def apply_subtitle_y_to_cy(cy: int) -> int:
    """将运行时纵向偏移应用到 ASS \\pos 的 Y 坐标（限制在画面内）。"""
    frame_h = get_output_height()
    adjusted = int(cy) - get_subtitle_y_offset()
    min_y = int(frame_h * 0.05)
    max_y = frame_h - max(scaled_subtitle_margin_bottom(), int(frame_h * 0.03))
    return max(min_y, min(max_y, adjusted))


def subtitle_y_offset_label(offset: int | None = None) -> str:
    off = get_subtitle_y_offset() if offset is None else int(offset)
    if off == 0:
        return "默认"
    if off > 0:
        return f"上移 {off}px"
    return f"下移 {abs(off)}px"


def auto_subtitle_ass_font_size() -> int:
    """按画布尺寸自动估算 ASS 字号（不含 UI 倍率与环境变量覆盖）。"""
    h = get_output_height()
    w = get_output_width()
    if w > h:
        return max(
            SUBTITLE_LANDSCAPE_FONT_MIN,
            round(h / SUBTITLE_LANDSCAPE_HEIGHT_DIVISOR),
        )
    if w == h:
        return max(
            SUBTITLE_SQUARE_FONT_MIN,
            round(h / SUBTITLE_SQUARE_HEIGHT_DIVISOR),
        )
    return max(28, round(h / SUBTITLE_PORTRAIT_HEIGHT_DIVISOR))


def scaled_subtitle_ass_font_size() -> int:
    explicit = os.environ.get("SUBTITLE_ASS_FONT_SIZE")
    if explicit:
        base = int(explicit)
    else:
        base = auto_subtitle_ass_font_size()
    return max(12, int(round(base * get_subtitle_font_scale())))


def subtitle_match_pipeline_segments() -> bool:
    """
    字幕 cue 是否与成片单元一一对应（不做段内逗号 progressive 切分）。
    默认 auto：LLM 分句时一段一条；0 允许段内逗号分步；1 强制一段一条。
    """
    from videoaudiotext.config.text_split import llm_split_enabled

    mode = os.environ.get("SUBTITLE_PIPELINE_SEGMENTS", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return llm_split_enabled()


def scaled_subtitle_ass_font_size_for_text(char_count: int) -> int:
    """
    ASS 烧录字号。SUBTITLE_UNIFORM_FONT=1（默认）时全片统一字号，超长句靠双行折行而非 \\fs 微缩。
    """
    base = scaled_subtitle_ass_font_size()
    if SUBTITLE_UNIFORM_FONT:
        return base
    if not subtitle_match_pipeline_segments():
        return base
    hard = scaled_subtitle_cue_max_chars()
    soft = max_allow_single_line_chars()
    n = max(0, int(char_count))
    if n <= hard:
        return base
    if n <= soft:
        scaled = int(round(base * hard / n))
        floor = max(24, int(round(base * 0.85)))
        return max(floor, scaled)
    return base


def subtitle_multiline_fs_boost_enabled() -> bool:
    """双行字幕略放大 \\fs，使每行视觉接近单行 cue（默认开）。"""
    raw = os.environ.get("SUBTITLE_MULTILINE_FS_BOOST", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return SUBTITLE_UNIFORM_FONT


def subtitle_multiline_fs_scale() -> float:
    """双行相对 Style 字号的放大倍率（默认 1.15）。"""
    try:
        value = float(os.environ.get("SUBTITLE_MULTILINE_FS_SCALE", "1.15"))
    except ValueError:
        value = 1.15
    return max(1.0, min(1.4, value))


def ass_font_size_for_line_count(line_count: int) -> int | None:
    """
    按折行数返回 ASS \\fs；单行返回 None（沿用 Style 统一字号）。
    双行及以上返回放大后的字号。
    """
    if line_count <= 1 or not subtitle_multiline_fs_boost_enabled():
        return None
    base = scaled_subtitle_ass_font_size()
    boosted = int(round(base * subtitle_multiline_fs_scale()))
    cap = int(round(base * 1.35))
    return min(max(base + 1, boosted), cap)


def layout_wrap_ass_font_size() -> int:
    """折行布局有效字号：含 font_scale；双行 cue 时含 multiline \\fs boost。"""
    base = scaled_subtitle_ass_font_size()
    if subtitle_max_lines_per_cue() >= 2:
        boosted = ass_font_size_for_line_count(2)
        if boosted is not None:
            return boosted
    return base


def scaled_subtitle_glyph_width_px() -> float:
    """折行布局用单字宽度（含 font_scale 与双行 fs boost）。"""
    return layout_wrap_ass_font_size() * SUBTITLE_SCALE_X / 100.0


def scaled_subtitle_chars_per_line_from_pixels(*, safety: float = 0.92) -> int:
    """由可用宽度与有效渲染字号反推单行字数（含 font_scale）。"""
    glyph = scaled_subtitle_glyph_width_px()
    if glyph <= 0:
        return SUBTITLE_CUE_MAX_CHARS
    budget = scaled_subtitle_renderable_width_px() * safety
    n = int(budget / glyph)
    while n > 4 and n * glyph > budget:
        n -= 1
    return max(4, n)


def scaled_subtitle_progressive_glyph_width_px() -> float:
    """渐进屏显单字 advance 估算（字号×ScaleX×校准，与 ASS 渲染宽度对齐）。"""
    base = scaled_subtitle_ass_font_size() * SUBTITLE_SCALE_X / 100.0
    factor = max(0.5, min(1.25, SUBTITLE_PROGRESSIVE_GLYPH_WIDTH_FACTOR))
    return base * factor


def progressive_ass_outline_pad_px() -> int:
    """ASS Outline 左右各占用的像素（默认 Outline=2）。"""
    return max(0, SUBTITLE_ASS_OUTLINE_PX) * 2


def scaled_subtitle_progressive_max_chars(*, safety: float = 0.92) -> int:
    """渐进模式单行字数（略宽于双行折行估算，仍含 font_scale）。"""
    glyph = scaled_subtitle_progressive_glyph_width_px()
    if glyph <= 0:
        return SUBTITLE_CUE_MAX_CHARS
    budget = scaled_subtitle_renderable_width_px() * safety
    n = int(budget / glyph)
    while n > 4 and n * glyph > budget:
        n -= 1
    return max(4, n)


SUBTITLE_PROGRESSIVE_SOFT_REF_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_SOFT_REF_CHARS", "14")
)
SUBTITLE_PROGRESSIVE_SOFT_MIN_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_SOFT_MIN_CHARS", "12")
)
SUBTITLE_PROGRESSIVE_SOFT_MAX_CHARS = int(
    os.environ.get("SUBTITLE_PROGRESSIVE_SOFT_MAX_CHARS", "14")
)
SUBTITLE_PROGRESSIVE_HARD_MAX_WIDTH_RATIO = float(
    os.environ.get("SUBTITLE_PROGRESSIVE_HARD_MAX_WIDTH_RATIO", "0.90")
)
SUBTITLE_PROGRESSIVE_GLYPH_WIDTH_FACTOR = float(
    os.environ.get("SUBTITLE_PROGRESSIVE_GLYPH_WIDTH_FACTOR", "1.0")
)
SUBTITLE_ASS_OUTLINE_PX = int(os.environ.get("SUBTITLE_ASS_OUTLINE_PX", "2"))


def _progressive_width_scale(*, canvas_w: int | None = None) -> float:
    w = canvas_w if canvas_w is not None else get_output_width()
    return max(0.25, w / _SUBTITLE_REF_WIDTH)


def progressive_soft_max_chars(*, canvas_w: int | None = None) -> int:
    """第一优先：软字数上限（1080 宽基准 14 字，随画布宽度同比缩放）。"""
    scale = _progressive_width_scale(canvas_w=canvas_w)
    n = int(round(SUBTITLE_PROGRESSIVE_SOFT_REF_CHARS * scale))
    lo = max(4, int(round(SUBTITLE_PROGRESSIVE_SOFT_MIN_CHARS * scale)))
    hi = max(lo, int(round(SUBTITLE_PROGRESSIVE_SOFT_MAX_CHARS * scale)))
    return max(lo, min(hi, n))


def progressive_hard_max_width_px(
    *,
    canvas_w: int | None = None,
    content_rect=None,
) -> int:
    """第二优先：全画布或有效画面宽 × ratio（默认 90%），扣除 ASS 描边占位。"""
    w = canvas_w if canvas_w is not None else get_output_width()
    ratio = max(0.5, min(0.98, SUBTITLE_PROGRESSIVE_HARD_MAX_WIDTH_RATIO))
    if (
        content_rect is not None
        and getattr(content_rect, "width", 0) > 0
        and int(content_rect.width) < w - 2
    ):
        raw = max(64, int(round(int(content_rect.width) * ratio)))
    else:
        raw = max(64, int(round(w * ratio)))
    return max(32, raw - progressive_ass_outline_pad_px())


def progressive_pixel_max_chars(
    *,
    canvas_w: int | None = None,
    content_rect=None,
) -> int:
    """第二优先：90% 像素宽折算单行字数（随分辨率与 font_scale 自动缩放）。"""
    hard_px = progressive_hard_max_width_px(canvas_w=canvas_w, content_rect=content_rect)
    glyph = scaled_subtitle_progressive_glyph_width_px()
    if glyph <= 0:
        return 4
    by_px = int(hard_px / glyph)
    while by_px > 1 and by_px * glyph > hard_px:
        by_px -= 1
    return max(1, by_px)


def progressive_limits_for_canvas(
    *,
    canvas_w: int | None = None,
    canvas_h: int | None = None,
    font_scale: float | None = None,
) -> dict[str, int]:
    """调试/测试：返回当前画布与字号下的软上限与像素上限。"""
    from videoaudiotext.config.output import set_active_output_dimensions

    prev_scale = get_subtitle_font_scale()
    if canvas_w is not None and canvas_h is not None:
        set_active_output_dimensions(canvas_w, canvas_h)
    if font_scale is not None:
        set_active_subtitle_font_scale(font_scale)
    try:
        limits = progressive_layout_limits()
        return {
            "soft": int(limits["soft_chars"]),
            "pixel": int(limits["pixel_chars"]),
            "pack": int(limits["pack_chars"]),
            "hard_px": int(limits["hard_px"]),
        }
    finally:
        set_active_subtitle_font_scale(prev_scale)


def progressive_effective_max_chars(
    *,
    canvas_w: int | None = None,
    max_line: int | None = None,
    content_rect=None,
) -> int:
    """装箱/折行硬上限：90% 像素字数与软上限取 min。"""
    limits = progressive_layout_limits(content_rect=content_rect)
    pack = int(limits["pack_chars"])
    if max_line is not None:
        return max(1, min(int(max_line), pack))
    if canvas_w is not None:
        soft = progressive_soft_max_chars(canvas_w=canvas_w)
        pixel = progressive_pixel_max_chars(canvas_w=canvas_w, content_rect=content_rect)
        return max(1, min(int(soft), int(pixel)))
    return pack


def progressive_orphan_merge_line_limit(
    max_line: int | None = None,
    *,
    content_rect=None,
) -> int:
    """orphan 合并/勿并回判断：软字数与像素字数取 min（随 font_scale 收紧）。"""
    soft = max_line if max_line is not None else progressive_soft_max_chars()
    pixel = progressive_pixel_max_chars(content_rect=content_rect)
    return max(1, min(int(soft), int(pixel)))


def progressive_min_screen_chars() -> int:
    """渐进屏显每行最少可见字数（默认 2，禁止单字成行）。"""
    return max(2, SUBTITLE_PROGRESSIVE_MIN_SCREEN_CHARS)


def progressive_layout_limits(
    *,
    content_rect=None,
) -> dict[str, float | int]:
    """
    渐进屏显布局参数：先由分辨率、字号倍率、content_rect 算出 90% 画布硬宽，
    再折算单行字数上限；装箱时以 min(软上限, 像素字数) 为硬约束。
    """
    hard_px = progressive_hard_max_width_px(content_rect=content_rect)
    glyph_px = scaled_subtitle_progressive_glyph_width_px()
    pixel_chars = progressive_pixel_max_chars(content_rect=content_rect)
    soft_chars = progressive_soft_max_chars()
    pack_chars = max(1, min(int(soft_chars), int(pixel_chars)))
    return {
        "hard_px": hard_px,
        "glyph_px": glyph_px,
        "soft_chars": soft_chars,
        "pixel_chars": pixel_chars,
        "pack_chars": pack_chars,
        "width_ratio": SUBTITLE_PROGRESSIVE_HARD_MAX_WIDTH_RATIO,
    }


def progressive_line_fits_hard_width(
    text: str,
    *,
    content_rect=None,
) -> bool:
    """单行屏显是否在 90% 画布硬宽内（按字号×字宽估算）。"""
    from videoaudiotext.subtitle.display import _normalize_subtitle_text

    plain = _normalize_subtitle_text(str(text or "").replace("\n", "").strip())
    if not plain:
        return True
    hard_px = progressive_hard_max_width_px(content_rect=content_rect)
    return estimate_progressive_text_width_px(plain) <= hard_px + 0.5


def progressive_pack_line_limit(
    text: str,
    *,
    soft_max: int | None = None,
    content_rect=None,
) -> int:
    """装箱字数硬上限：90% 像素宽折算字数与软上限取 min（不再先软后补像素）。"""
    _ = text  # 保留签名，便于按句微调
    soft = soft_max if soft_max is not None else progressive_soft_max_chars()
    pixel = progressive_pixel_max_chars(content_rect=content_rect)
    return max(1, min(int(soft), int(pixel)))


def scaled_subtitle_progressive_frame_lr_margin() -> int:
    """渐进硬字幕左右留白：与 90% 画布宽一致（各约 5% 边距）。"""
    frame_w = get_output_width()
    ratio = max(0.5, min(0.98, SUBTITLE_PROGRESSIVE_HARD_MAX_WIDTH_RATIO))
    side = int(round(frame_w * (1.0 - ratio) / 2.0))
    return max(16, side)


def estimate_progressive_text_width_px(text: str) -> float:
    """估算渐进单行字幕渲染宽度（含 font_scale；标点计宽）。"""
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    plain = _normalize_subtitle_text(str(text or "").replace("\n", ""))
    if not plain:
        return 0.0
    return _core_char_len(plain) * scaled_subtitle_progressive_glyph_width_px()


def progressive_text_needs_split(
    text: str,
    *,
    max_line: int | None = None,
    content_rect=None,
) -> bool:
    """整段是否需要拆行：超软字数或超 90% 画布硬宽。"""
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    plain = _normalize_subtitle_text(str(text or "").replace("\n", "").strip())
    if not plain:
        return False
    limits = progressive_layout_limits(content_rect=content_rect)
    pack = int(limits["pack_chars"])
    soft = max_line if max_line is not None else int(limits["soft_chars"])
    effective = max(1, min(int(soft), pack))
    if _core_char_len(plain) > effective:
        return True
    return not progressive_line_fits_hard_width(plain, content_rect=content_rect)


def progressive_screen_line_needs_split(
    text: str,
    *,
    content_rect=None,
) -> bool:
    """单行屏显是否超 90% 画布硬宽（或超像素字数折算上限）。"""
    from videoaudiotext.subtitle.display import _core_char_len, _normalize_subtitle_text

    plain = _normalize_subtitle_text(str(text or "").replace("\n", "").strip())
    if not plain:
        return False
    pixel = progressive_pixel_max_chars(content_rect=content_rect)
    if _core_char_len(plain) > pixel:
        return True
    return not progressive_line_fits_hard_width(plain, content_rect=content_rect)


def scaled_subtitle_srt_font_size() -> int:
    """SRT 硬烧 force_style 字号，与 ASS Style 同源。"""
    return scaled_subtitle_ass_font_size()


def _subtitle_width_scale() -> float:
    """屏显拆行以画面宽度为基准（竖屏 1080 字宽 ≈ 12 字/行）。"""
    return get_output_width() / _SUBTITLE_REF_WIDTH


def scaled_subtitle_cue_max_chars() -> int:
    """单行字数：按 margin 内可用像素与字号估算（不再对 80% 宽度二次缩放）。"""
    return scaled_subtitle_chars_per_line_from_pixels()


def scaled_subtitle_ass_max_chars_per_line() -> int:
    """ASS 单行硬上限，与屏显折行一致。"""
    return scaled_subtitle_cue_max_chars()


def scaled_subtitle_cue_wrap_max_chars() -> int:
    return max(12, int(round(SUBTITLE_CUE_WRAP_MAX_CHARS * _subtitle_width_scale())))


def max_allow_single_line_chars() -> int:
    """无标点句放行上限：默认 14 字，不低于硬切分线 scaled_subtitle_cue_max_chars()。"""
    explicit = os.environ.get("MAX_ALLOW_SINGLE_LINE")
    if explicit:
        return max(scaled_subtitle_cue_max_chars(), int(explicit))
    return max(scaled_subtitle_cue_max_chars(), MAX_ALLOW_SINGLE_LINE)


def subtitle_max_lines_per_cue() -> int:
    """每条字幕 cue 最大行数（标准化默认 2 行，可通过 MAX_LINES_PER_CUE 覆盖）。"""
    if MAX_LINES_PER_CUE:
        return min(SUBTITLE_MAX_LINES, max(1, int(MAX_LINES_PER_CUE)))
    return SUBTITLE_MAX_LINES


def scaled_subtitle_landscape_max_chars(content_width: int) -> int:
    """横屏按画布宽度缩放单行字数（1920 宽参考 ≈24 字）。"""
    return max(
        10,
        int(round(SUBTITLE_LANDSCAPE_MAX_CHARS * content_width / 1920)),
    )


def scaled_subtitle_square_max_chars(content_width: int) -> int:
    """方屏按宽度缩放单行字数（1080 参考 ≈16 字）。"""
    return max(
        8,
        int(round(SUBTITLE_SQUARE_MAX_CHARS * content_width / 1080)),
    )


def scaled_subtitle_line_max_chars() -> int:
    return max(10, int(round(SUBTITLE_LINE_MAX_CHARS * _subtitle_width_scale())))


def scaled_subtitle_screen_max_chars() -> int:
    return max(12, int(round(SUBTITLE_SCREEN_MAX_CHARS * _subtitle_width_scale())))


def scaled_subtitle_line_max_diff() -> int:
    return max(4, int(round(SUBTITLE_LINE_MAX_DIFF * _subtitle_width_scale())))


def scaled_comma_short_opening_max_chars() -> int:
    return max(3, int(round(COMMA_SHORT_OPENING_MAX_CHARS * _subtitle_width_scale())))


def scaled_subtitle_merge_max_chars() -> int:
    return max(8, int(round(SUBTITLE_MERGE_MAX_CHARS * _subtitle_width_scale())))


def dunhao_progressive_enabled() -> bool:
    """超短顿号排比（A、B、C）允许段内分步字幕。"""
    return os.environ.get("SUBTITLE_DUNHAO_PROGRESSIVE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def subtitle_strip_trailing_punct() -> bool:
    """屏显字幕去掉行末，。等（大字流）。"""
    return os.environ.get("SUBTITLE_STRIP_TRAILING_PUNCT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def subtitle_preserve_tts_timing() -> bool:
    """LLM 模式：字幕起止与 TTS 段时长严格一致，不做 breath 拉长或 ASR 改轴。"""
    return subtitle_match_pipeline_segments()
