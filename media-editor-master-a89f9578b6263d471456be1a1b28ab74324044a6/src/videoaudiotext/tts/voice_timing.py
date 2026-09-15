"""Per-voice speech rate for segment preview estimation."""

from __future__ import annotations

import re

from videoaudiotext.config import CHARS_PER_SEC_ESTIMATE

# 每秒可读汉字数（基准）；可按 Minimax 音色微调
VOICE_CHARS_PER_SEC: dict[str, float] = {
    "default": CHARS_PER_SEC_ESTIMATE,
    "Chinese (Mandarin)_Lyrical_Voice": 4.6,
    "Chinese (Mandarin)_HK_Flight_Attendant": 4.5,
    "Chinese (Mandarin)_Warm_Girl": 4.5,
    "Chinese (Mandarin)_Reliable_Executive": 4.8,
    "Chinese (Mandarin)_News_Anchor": 4.2,
    "Chinese (Mandarin)_Sweet_Lady": 4.5,
    "Chinese (Mandarin)_Male_Announcer": 4.7,
    "Chinese (Mandarin)_IntellectualGirl": 4.4,
    "Chinese (Mandarin)_Radio_Host": 4.3,
    "Chinese (Mandarin)_Gentle_Youth": 4.5,
}

_RATE_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")


def parse_rate_multiplier(rate: str) -> float:
    """Rate like +10% → 1.1 (语速越快，同样字数用时越短)."""
    text = (rate or "+0%").strip()
    m = _RATE_RE.match(text)
    if not m:
        return 1.0
    pct = float(m.group(1))
    return max(0.5, 1.0 + pct / 100.0)


def chars_per_sec_for_voice(voice: str, rate: str = "+0%") -> float:
    base = VOICE_CHARS_PER_SEC.get(voice.strip(), VOICE_CHARS_PER_SEC["default"])
    return base * parse_rate_multiplier(rate)
