from dataclasses import dataclass
from typing import Optional

from backend.models.schemas import SegmentInput


class SegmentValidationError(Exception):
    pass


@dataclass
class ValidatedSegment:
    start: float
    end: float
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    summary: Optional[str] = None


def round_time(value: float) -> float:
    return round(value, 3)


def validate_segments(
    segments: list[SegmentInput],
    video_duration: float,
) -> list[ValidatedSegment]:
    if not segments:
        raise SegmentValidationError("至少需要一个切片片段")

    validated: list[ValidatedSegment] = []
    sorted_segments = sorted(segments, key=lambda s: s.start)

    for seg in sorted_segments:
        start = round_time(seg.start)
        end = round_time(seg.end)

        if start >= end:
            raise SegmentValidationError(f"无效片段：起点 {start}s 必须小于终点 {end}s")
        if start < 0:
            raise SegmentValidationError("片段起点不能为负数")
        if end > video_duration + 0.001:
            raise SegmentValidationError(
                f"片段终点 {end}s 超出视频时长 {video_duration:.3f}s"
            )

        sf = seg.start_frame
        ef = seg.end_frame
        if sf is not None and ef is not None and ef <= sf:
            raise SegmentValidationError(f"无效帧区间：{sf} -> {ef}")

        validated.append(
            ValidatedSegment(
                start=start,
                end=end,
                start_frame=sf,
                end_frame=ef,
                summary=(seg.summary.strip() if seg.summary else None),
            )
        )

    return validated
