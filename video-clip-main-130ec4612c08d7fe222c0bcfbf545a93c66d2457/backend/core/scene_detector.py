from dataclasses import dataclass
from typing import Callable, Optional

from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video


@dataclass
class DetectedScene:
    start: float
    end: float
    confidence: float = 1.0
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    summary: Optional[str] = None


ProgressCallback = Callable[[float, str], None]

SUPPORTED_DETECTORS = ("content", "adaptive", "semantic")

# 硬切近距合并更保守，避免抹掉合法快切
MERGE_GAP_BY_DETECTOR = {
    "content": 0.2,
    "adaptive": 0.5,
    "semantic": 0.8,
}

# 画面/抗闪检测阶段的最小镜号（秒）。宜尽量小，避免与用户后处理合并阈值叠加导致漏检。
DETECT_MIN_SCENE_LEN_SEC = 0.5


class _ProgressSceneManager(SceneManager):
    """在逐帧检测时上报进度。"""

    def __init__(
        self,
        on_progress: Optional[ProgressCallback],
        video_duration: float,
        fps: float,
    ):
        super().__init__()
        self._on_progress = on_progress
        self._video_duration = video_duration
        self._fps = fps
        self._last_reported_second = -1.0

    def _process_frame(self, position, frame_im, callback=None):
        result = super()._process_frame(position, frame_im, callback)
        if self._on_progress and self._video_duration > 0:
            # PySceneDetect 0.6.x passes an integer frame number here;
            # older releases may pass FrameTimecode instead.
            if hasattr(position, "get_seconds"):
                current = float(position.get_seconds())
            elif hasattr(position, "seconds"):
                current = float(position.seconds)
            else:
                current = float(position) / self._fps
            if current - self._last_reported_second >= 2.0:
                pct = 10 + min(85, current / self._video_duration * 85)
                current_tc = _format_timecode(current)
                total_tc = _format_timecode(self._video_duration)
                self._on_progress(
                    pct,
                    f"正在检测分镜... 已分析至 {current_tc} / {total_tc}",
                )
                self._last_reported_second = current
        return result


def _format_timecode(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def _frame_num(timecode) -> int:
    if isinstance(timecode, (int, float)):
        return int(timecode)
    if hasattr(timecode, "frame_num"):
        return int(timecode.frame_num)
    return int(timecode.get_frames())


def _get_detector(
    detector: str,
    threshold: float,
    fps: float = 25.0,
    min_scene_len: float = 2.0,
):
    min_scene_frames = max(1, int(round(min_scene_len * fps)))

    if detector == "adaptive":
        # 前端「差异阈值」沿用 ContentDetector 的 1~100 刻度（越小越敏感）。
        # AdaptiveDetector 实际吃的是 adaptive_threshold（约 0.5~10）和 min_content_val。
        adaptive_threshold = max(0.5, min(12.0, float(threshold) / 12.0))
        min_content_val = max(5.0, float(threshold) * 0.5)
        return AdaptiveDetector(
            adaptive_threshold=adaptive_threshold,
            min_scene_len=min_scene_frames,
            min_content_val=min_content_val,
        )

    # Content（通用硬切）：threshold 越大 → 切点越少
    return ContentDetector(threshold=threshold, min_scene_len=min_scene_frames)


def detect_scenes(
    video_path: str,
    detector: str = "content",
    threshold: float = 35.0,
    min_scene_len: float = 2.0,
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[list[DetectedScene], float]:
    """返回 (分镜列表, 检测所用 fps)。分镜带精确帧号。"""
    if detector not in SUPPORTED_DETECTORS:
        raise ValueError(f"不支持的检测算法: {detector}")

    if detector == "semantic":
        from backend.core.semantic_detector import detect_semantic_scenes

        # threshold 在语义模式下可复用为抽帧 fps（0.2~5）；否则用服务端默认
        sample_fps = threshold if 0.2 <= threshold <= 5.0 else None
        return detect_semantic_scenes(
            video_path,
            min_scene_len=min_scene_len,
            sample_fps=sample_fps,
            on_progress=on_progress,
        )

    video = open_video(video_path)
    fps = float(video.frame_rate) if video.frame_rate else 25.0
    if video.duration:
        if hasattr(video.duration, "get_seconds"):
            duration = float(video.duration.get_seconds())
        elif hasattr(video.duration, "seconds"):
            duration = float(video.duration.seconds)
        else:
            duration = float(video.duration)
    else:
        duration = 0.0

    if on_progress:
        on_progress(10, "正在检测分镜... 初始化")

    scene_manager = _ProgressSceneManager(on_progress, duration, fps)
    scene_manager.add_detector(
        _get_detector(
            detector,
            threshold,
            fps=fps,
            min_scene_len=DETECT_MIN_SCENE_LEN_SEC,
        )
    )
    scene_manager.detect_scenes(video=video)
    # start_in_scene=True：无硬切时仍返回整段视频，避免空结果直接失败
    scene_list = scene_manager.get_scene_list(start_in_scene=True)

    if on_progress:
        on_progress(95, "正在整理分镜结果...")

    scenes: list[DetectedScene] = []
    for scene in scene_list:
        start_frame = _frame_num(scene[0])
        end_frame = _frame_num(scene[1])
        scenes.append(
            DetectedScene(
                start=round(start_frame / fps, 6),
                end=round(end_frame / fps, 6),
                start_frame=start_frame,
                end_frame=end_frame,
            )
        )

    return scenes, fps


def _merge_short_segments(
    segments: list[DetectedScene],
    min_scene_len: float,
    fps: float,
) -> list[DetectedScene]:
    """将过短分镜与后续相邻段合并，直到每段 >= min_scene_len。"""
    if not segments:
        return []

    min_frames = max(1, int(round(min_scene_len * fps)))
    segs = [
        DetectedScene(
            s.start,
            s.end,
            s.confidence,
            s.start_frame if s.start_frame is not None else int(round(s.start * fps)),
            s.end_frame if s.end_frame is not None else int(round(s.end * fps)),
            s.summary,
        )
        for s in segments
    ]

    merged: list[DetectedScene] = []
    i = 0
    while i < len(segs):
        start_f = segs[i].start_frame or 0
        end_f = segs[i].end_frame or 0
        confidence = segs[i].confidence
        summary = segs[i].summary

        j = i
        while (end_f - start_f) < min_frames and j + 1 < len(segs):
            j += 1
            nxt = segs[j]
            end_f = nxt.end_frame or end_f
            confidence = min(confidence, nxt.confidence)
            if nxt.summary:
                if summary and summary != nxt.summary:
                    summary = f"{summary} / {nxt.summary}"
                elif not summary:
                    summary = nxt.summary

        merged.append(
            DetectedScene(
                start=round(start_f / fps, 6),
                end=round(end_f / fps, 6),
                confidence=confidence,
                start_frame=start_f,
                end_frame=end_f,
                summary=summary,
            )
        )
        i = j + 1

    if len(merged) >= 2:
        last = merged[-1]
        last_len = (last.end_frame or 0) - (last.start_frame or 0)
        if last_len < min_frames:
            prev = merged[-2]
            tail_summary = prev.summary or last.summary
            if prev.summary and last.summary and prev.summary != last.summary:
                tail_summary = f"{prev.summary} / {last.summary}"
            merged[-2] = DetectedScene(
                start=prev.start,
                end=last.end,
                confidence=min(prev.confidence, last.confidence),
                start_frame=prev.start_frame,
                end_frame=last.end_frame,
                summary=tail_summary,
            )
            merged.pop()

    return merged


def postprocess_scenes(
    scenes: list[DetectedScene],
    min_scene_len: float = 1.0,
    merge_gap: Optional[float] = None,
    max_scene_len: float = 300.0,
    fps: float = 25.0,
    detector: str = "content",
) -> list[DetectedScene]:
    if not scenes:
        return []

    if merge_gap is None:
        merge_gap = MERGE_GAP_BY_DETECTOR.get(detector, 0.5)

    merge_gap_frames = max(0, int(round(merge_gap * fps)))

    # 优先用帧号做切点，半开区间
    cuts = [
        scenes[0].start_frame
        if scenes[0].start_frame is not None
        else int(round(scenes[0].start * fps))
    ]
    for scene in scenes:
        end_f = scene.end_frame if scene.end_frame is not None else int(round(scene.end * fps))
        if end_f > cuts[-1]:
            cuts.append(end_f)

    merged_cuts = [cuts[0]]
    for cut in cuts[1:]:
        if cut - merged_cuts[-1] < merge_gap_frames:
            continue
        merged_cuts.append(cut)

    segments: list[DetectedScene] = []
    for i in range(len(merged_cuts) - 1):
        start_f, end_f = merged_cuts[i], merged_cuts[i + 1]
        segments.append(
            DetectedScene(
                start=round(start_f / fps, 6),
                end=round(end_f / fps, 6),
                start_frame=start_f,
                end_frame=end_f,
            )
        )

    segments = _merge_short_segments(segments, min_scene_len, fps)

    result: list[DetectedScene] = []
    for seg in segments:
        duration = seg.end - seg.start
        confidence = 0.5 if duration > max_scene_len else 1.0
        result.append(
            DetectedScene(
                start=seg.start,
                end=seg.end,
                confidence=confidence,
                start_frame=seg.start_frame,
                end_frame=seg.end_frame,
                summary=seg.summary,
            )
        )

    return result
