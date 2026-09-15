"""Non-9:16 media → full canvas with gaussian blur background (方案一)."""

from __future__ import annotations

from videoaudiotext.config import get_output_height, get_output_width


def blur_bg_fill_enabled() -> bool:
    mode = __import__("os").environ.get("BLUR_BG_FILL", "1").strip().lower()
    return mode not in ("0", "false", "no", "off")


def blur_bg_sigma() -> float:
    explicit = __import__("os").environ.get("BLUR_BG_SIGMA")
    if explicit:
        return max(1.0, float(explicit))
    return 20.0


def aspect_ratio_tolerance() -> float:
    explicit = __import__("os").environ.get("BLUR_BG_ASPECT_TOLERANCE")
    if explicit:
        return max(0.01, float(explicit))
    return 0.05


def needs_blur_background(src_w: int, src_h: int) -> bool:
    """素材宽高比与成片画布不一致时启用毛玻璃铺底。"""
    if not blur_bg_fill_enabled():
        return False
    if src_w <= 0 or src_h <= 0:
        return False
    tw, th = get_output_width(), get_output_height()
    src_ar = src_w / src_h
    tgt_ar = tw / th
    return abs(src_ar - tgt_ar) > aspect_ratio_tolerance()


def scale_filter_plain() -> str:
    """原逻辑：decrease + 黑边 pad。"""
    w, h = get_output_width(), get_output_height()
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def strict_canvas_pad_filter() -> str:
    """末级断言：无论上游输出尺寸，强制刷回目标画布。"""
    w, h = get_output_width(), get_output_height()
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def finalize_video_filter(base_vf: str) -> str:
    """在滤镜链末尾追加严格画布断言（可 STRICT_CANVAS_ASSERTION=0 关闭）。"""
    from videoaudiotext.config import strict_canvas_assertion_enabled

    if not strict_canvas_assertion_enabled():
        return base_vf
    suffix = strict_canvas_pad_filter()
    if not base_vf:
        return suffix
    return f"{base_vf},{suffix}"


def get_universal_ffmpeg_filter(
    material_w: int,
    material_h: int,
    target_w: int | None = None,
    target_h: int | None = None,
) -> str:
    """模块三公开别名：分辨率感知毛玻璃/直铺滤镜。"""
    return build_ffmpeg_video_filter(material_w, material_h, target_w, target_h)


def build_ffmpeg_video_filter(
    material_w: int,
    material_h: int,
    target_w: int | None = None,
    target_h: int | None = None,
) -> str:
    """通用画面版式自适应滤镜（闸门三）；target 默认取成片画布 config。"""
    _ = target_w, target_h  # 画布由 get_output_* 统一
    return finalize_video_filter(video_filter_for_dimensions(material_w, material_h))


def video_filter_for_dimensions(src_w: int, src_h: int) -> str:
    """同比例直铺；比例不一致 → 毛玻璃铺底 + 前景等比缩小居中（不放大）。"""
    if not needs_blur_background(src_w, src_h):
        return scale_filter_plain()

    w, h = get_output_width(), get_output_height()
    sigma = blur_bg_sigma()
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma={sigma}[bg_blurred];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fg_scaled];"
        f"[bg_blurred][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )
