"""Publish platform presets — resolution and subtitle hints."""

from __future__ import annotations

import os
from typing import Mapping

_PRESETS: dict[str, dict[str, str | int]] = {
    "douyin": {
        "resolution": "1080x1920",
        "width": 1080,
        "height": 1920,
        "label": "抖音/视频号竖屏 1080×1920",
    },
    "xiaohongshu": {
        "resolution": "1080x1920",
        "width": 1080,
        "height": 1920,
        "label": "小红书竖屏 1080×1920",
    },
    "bilibili": {
        "resolution": "1920x1080",
        "width": 1920,
        "height": 1080,
        "label": "B站横屏 1920×1080",
    },
    "youtube_shorts": {
        "resolution": "1080x1920",
        "width": 1080,
        "height": 1920,
        "label": "YouTube Shorts 1080×1920",
    },
}


def publish_preset_names() -> list[str]:
    return list(_PRESETS.keys())


def publish_preset_ui_choices() -> list[tuple[str, str]]:
    """Gradio 下拉：(显示名, preset key)；空字符串表示不套用平台预设。"""
    choices: list[tuple[str, str]] = [("手动选择分辨率", "")]
    for key in publish_preset_names():
        preset = _PRESETS[key]
        choices.append((str(preset["label"]), key))
    return choices


def resolution_for_publish_preset(name: str | None) -> str | None:
    """返回 preset 对应的分辨率 mode（如 1080x1920），未知/空则 None。"""
    if not name or not str(name).strip():
        return None
    preset = get_publish_preset(name)
    return str(preset["resolution"])


def get_publish_preset(name: str) -> dict[str, str | int]:
    key = name.strip().lower()
    preset = _PRESETS.get(key)
    if not preset:
        known = ", ".join(_PRESETS)
        raise ValueError(f"未知 preset「{name}」，可选：{known}")
    return dict(preset)


def apply_publish_preset(name: str | None) -> Mapping[str, str | int]:
    """写入环境变量并返回 preset 描述（CLI --preset）。"""
    if not name:
        return {}
    preset = get_publish_preset(name)
    os.environ["OUTPUT_WIDTH"] = str(preset["width"])
    os.environ["OUTPUT_HEIGHT"] = str(preset["height"])
    return preset
