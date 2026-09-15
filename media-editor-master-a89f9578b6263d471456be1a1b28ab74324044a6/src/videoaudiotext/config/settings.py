"""Pipeline configuration — re-exports domain modules for backward compatibility."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from videoaudiotext.config._helpers import env_bool
from videoaudiotext.config.env import load_dotenv

load_dotenv()

from videoaudiotext.config.output import *  # noqa: F403,F401,E402
from videoaudiotext.config.paths import PROJECT_ROOT, ROOT
from videoaudiotext.config.retrieval import *  # noqa: F403,F401,E402
from videoaudiotext.config.subtitle import *  # noqa: F403,F401,E402
from videoaudiotext.config.text_split import *  # noqa: F403,F401,E402

# BGM / 混音
BGM_DIR = ROOT / "data" / "audio"
DEFAULT_VOICE_VOLUME = max(0.0, float(os.environ.get("DEFAULT_VOICE_VOLUME", "1.0")))
DEFAULT_BGM_VOLUME = max(0.0, float(os.environ.get("DEFAULT_BGM_VOLUME", "0.15")))
_DEFAULT_BGM_FILENAME = "古风影视游戏配乐 - 柔情婉转 - 荷池浅影_爱给网_aigei_com.mp3"


def _resolve_default_bgm_path() -> Path | None:
    """默认 BGM 文件；可用 DEFAULT_BGM_PATH 覆盖，设为空则禁用。"""
    env = os.environ.get("DEFAULT_BGM_PATH")
    if env is not None:
        raw = env.strip()
        if not raw or raw.lower() in {"0", "false", "no", "off", "none"}:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        resolved = path.resolve()
        return resolved if resolved.is_file() else None
    candidate = (BGM_DIR / _DEFAULT_BGM_FILENAME).resolve()
    return candidate if candidate.is_file() else None


DEFAULT_BGM_PATH = _resolve_default_bgm_path()

BGM_DUCKING_ENABLED = os.environ.get("BGM_DUCKING", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
BGM_DUCKING_RATIO = float(os.environ.get("BGM_DUCKING_RATIO", "4.0"))
BGM_DUCKING_THRESHOLD = float(os.environ.get("BGM_DUCKING_THRESHOLD", "0.025"))

# 成片路径
TEXT_FILE = ROOT / "text.txt"
AUDIO_DIR = ROOT / "audio"
SOURCE_MEDIA_DIR = ROOT / "source_media"
CLIP_OFFSETS_JSON = SOURCE_MEDIA_DIR / "clip_offsets.json"
CLIP_PARTS_JSON = SOURCE_MEDIA_DIR / "clip_parts.json"
SEGMENT_TIMELINE_JSON = SOURCE_MEDIA_DIR / "segment_timeline.json"
SHOT_TREE_JSON = SOURCE_MEDIA_DIR / "shot_tree.json"
MEDIA_CHOICES_JSON = SOURCE_MEDIA_DIR / "media_choices.json"
PIPELINE_PLAN_JSON = SOURCE_MEDIA_DIR / "pipeline_plan.json"
SHOT_TREE_ENABLED = os.environ.get("SHOT_TREE_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
CLIP_DIR = ROOT / "clip"
TEMP_DIR = ROOT / "temp"

_cleanup_all = os.environ.get("CLEANUP_TEMP_CLIP")
if _cleanup_all is not None:
    _cleanup_enabled = _cleanup_all.strip().lower() in {"1", "true", "yes", "on"}
    CLEANUP_TEMP_CLIP_AFTER_RUN = _cleanup_enabled
    CLEANUP_TEMP_CLIP_ON_FAILURE = _cleanup_enabled
    CLEANUP_SOURCE_MEDIA_CACHE = _cleanup_enabled
    CLEANUP_SOURCE_MEDIA_NUMBERED = _cleanup_enabled and env_bool(
        "CLEANUP_SOURCE_MEDIA_NUMBERED", "1"
    )
    CLEANUP_COVER = _cleanup_enabled and env_bool("CLEANUP_COVER", "1")
else:
    CLEANUP_TEMP_CLIP_AFTER_RUN = env_bool("CLEANUP_TEMP_CLIP_AFTER_RUN", "1")
    CLEANUP_TEMP_CLIP_ON_FAILURE = env_bool("CLEANUP_TEMP_CLIP_ON_FAILURE", "1")
    CLEANUP_SOURCE_MEDIA_CACHE = env_bool("CLEANUP_SOURCE_MEDIA_CACHE", "1")
    CLEANUP_SOURCE_MEDIA_NUMBERED = env_bool("CLEANUP_SOURCE_MEDIA_NUMBERED", "1")
    CLEANUP_COVER = env_bool("CLEANUP_COVER", "1")

FILELIST = ROOT / "filelist.txt"
SUBTITLE_SRT = ROOT / "subtitle.srt"
SUBTITLE_ASS = ROOT / "subtitle.ass"
NO_SUB_MP4 = ROOT / "no_sub.mp4"
OUTPUT_MP4 = ROOT / "output.mp4"
COVER_JPG = ROOT / "cover.jpg"
COVER_AT_SEC = float(os.environ.get("COVER_AT_SEC", "0.5"))


def export_cover_enabled() -> bool:
    return os.environ.get("EXPORT_COVER", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )

# 段间歇息
GAP_ENABLED = os.environ.get("GAP_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
GAP_BIG_SENTENCE_SEC = float(os.environ.get("GAP_BIG_SENTENCE_SEC", "0.35"))
GAP_SMALL_SENTENCE_SEC = float(os.environ.get("GAP_SMALL_SENTENCE_SEC", "0.15"))
SEGMENT_TAIL_PAD_SEC = GAP_BIG_SENTENCE_SEC
GAP_MIN_SEC = GAP_SMALL_SENTENCE_SEC
GAP_MAX_SEC = GAP_BIG_SENTENCE_SEC

# 主音轨淡化
AUDIO_CROSSFADE_ENABLED = os.environ.get(
    "AUDIO_CROSSFADE_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")
AUDIO_CROSSFADE_SEC = float(os.environ.get("AUDIO_CROSSFADE_SEC", "0.08"))
AUDIO_FADE_IN_SEC = float(os.environ.get("AUDIO_FADE_IN_SEC", "0.3"))
AUDIO_FADE_OUT_SEC = float(os.environ.get("AUDIO_FADE_OUT_SEC", "0.2"))
AUDIO_EDGE_MICRO_SEC = float(os.environ.get("AUDIO_EDGE_MICRO_SEC", "0.01"))
AUDIO_CROSSFADE_CURVE = os.environ.get("AUDIO_CROSSFADE_CURVE", "qsin").strip() or "qsin"

AUDIO_FADE_STRENGTH_OFF = "off"
AUDIO_FADE_STRENGTH_LIGHT = "light"
AUDIO_FADE_STRENGTH_MEDIUM = "medium"
AUDIO_FADE_STRENGTH_STRONG = "strong"

AUDIO_FADE_STRENGTH_LABELS = {
    AUDIO_FADE_STRENGTH_OFF: "关闭",
    AUDIO_FADE_STRENGTH_LIGHT: "轻",
    AUDIO_FADE_STRENGTH_MEDIUM: "中",
    AUDIO_FADE_STRENGTH_STRONG: "强",
}


@dataclass(frozen=True)
class AudioFadeParams:
    crossfade_sec: float
    fade_in_sec: float
    fade_out_sec: float
    edge_micro_sec: float

    def log_summary(self) -> str:
        return (
            f"in={self.fade_in_sec:.2f}s/x={self.crossfade_sec:.2f}s/"
            f"out={self.fade_out_sec:.2f}s"
        )


AUDIO_FADE_PRESETS: dict[str, AudioFadeParams] = {
    AUDIO_FADE_STRENGTH_LIGHT: AudioFadeParams(0.08, 0.30, 0.20, 0.01),
    AUDIO_FADE_STRENGTH_MEDIUM: AudioFadeParams(0.12, 0.45, 0.35, 0.015),
    AUDIO_FADE_STRENGTH_STRONG: AudioFadeParams(0.15, 0.50, 0.40, 0.02),
}


def audio_crossfade_enabled() -> bool:
    return AUDIO_CROSSFADE_ENABLED


def default_audio_fade_strength() -> str:
    return (
        AUDIO_FADE_STRENGTH_LIGHT
        if audio_crossfade_enabled()
        else AUDIO_FADE_STRENGTH_OFF
    )


def normalize_audio_fade_strength(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return default_audio_fade_strength()
    label_to_key = {v: k for k, v in AUDIO_FADE_STRENGTH_LABELS.items()}
    if raw in label_to_key:
        return label_to_key[raw]
    aliases = {
        "off": AUDIO_FADE_STRENGTH_OFF,
        "0": AUDIO_FADE_STRENGTH_OFF,
        "false": AUDIO_FADE_STRENGTH_OFF,
        "light": AUDIO_FADE_STRENGTH_LIGHT,
        "medium": AUDIO_FADE_STRENGTH_MEDIUM,
        "strong": AUDIO_FADE_STRENGTH_STRONG,
    }
    if raw in aliases:
        return aliases[raw]
    if raw in AUDIO_FADE_PRESETS or raw == AUDIO_FADE_STRENGTH_OFF:
        return raw
    return default_audio_fade_strength()


def audio_fade_params_for_strength(strength: str | None) -> AudioFadeParams | None:
    key = normalize_audio_fade_strength(strength)
    if key == AUDIO_FADE_STRENGTH_OFF:
        return None
    return AUDIO_FADE_PRESETS.get(key)


def audio_fade_strength_label(strength: str | None) -> str:
    key = normalize_audio_fade_strength(strength)
    return AUDIO_FADE_STRENGTH_LABELS.get(key, key)


# Segment merge（仅 segment_merge_enabled() 时生效）
MIN_SEGMENT_SEC = 3.0
MAX_SEGMENT_CHARS = 18
MAX_SEGMENT_SEC = 10.0
CHARS_PER_SEC_ESTIMATE = 4.5
AUDIO_PROBE_DIR = AUDIO_DIR / "_probe"
