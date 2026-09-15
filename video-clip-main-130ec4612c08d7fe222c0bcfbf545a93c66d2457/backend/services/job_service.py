"""切片任务业务：创建、检测、切割、轮询、发布。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from backend.api.errors import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    CODE_INVALID_SEGMENT_IDS,
    CODE_JOB_NOT_FOUND,
    CODE_JOB_NOT_READY,
    CODE_NOT_FOUND,
    CODE_OSS_DISABLED,
    CODE_QUEUE_UNAVAILABLE,
    CODE_VIDEO_NOT_FOUND,
    raise_api_error,
)
from backend.api.v1.schemas import (
    ApiAction,
    Job,
    JobMode,
    JobStatus,
    JobSubmitted,
    PublishedSegmentItem,
    PublishResponse,
    Segment,
    SegmentInput,
    SegmentSource,
)
from backend.api.v1.urls import absolutize, segment_media_url, segment_preview_url
from backend.models.schemas import SegmentInput as InternalSegmentInput
from backend.models.schemas import TaskMode, TaskStatus
from backend.storage import oss_client
from backend.storage.database import CapacityError, db
from backend.storage.file_manager import generate_id
from backend.utils.naming import segment_download_filename
from backend.workers.enqueue import (
    QueueUnavailableError,
    enqueue_auto_cut,
    enqueue_auto_detect,
    enqueue_manual,
)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _video_filename(video_id: str | None) -> str | None:
    if not video_id:
        return None
    video = db.get_video(video_id)
    return video.get("filename") if video else None


def _segment_download_name(seg: dict, source_filename: str | None = None) -> str:
    if source_filename is None:
        task = db.get_task(seg["task_id"])
        source_filename = _video_filename(task["video_id"]) if task else None
    return segment_download_filename(
        source_filename,
        seg["task_id"],
        int(seg["index_num"]),
        float(seg["start_time"]),
        float(seg["end_time"]),
    )


def _safe_oss_url(key: str) -> str | None:
    try:
        return oss_client.preview_url(key)
    except oss_client.OssError:
        return None


def _to_internal_segments(
    segments: list[SegmentInput] | None,
) -> list[InternalSegmentInput] | None:
    if segments is None:
        return None
    return [InternalSegmentInput(start=s.start, end=s.end) for s in segments]


def _segment_to_model(seg: dict, *, base: str = "", source_filename: str | None = None) -> Segment:
    path = seg.get("output_path")
    has_file = bool(path and Path(path).exists())
    oss_key = seg.get("oss_key") or seg.get("staging_oss_key")
    staging_key = seg.get("staging_oss_key")
    media_key = oss_key or staging_key
    has_oss = bool(media_key and oss_client.is_enabled())
    sid = seg["id"]
    preview_url = thumb_url = None
    if has_file or has_oss:
        # 切割完成后直接从 clips OSS 对象预览
        preview_url = segment_preview_url(sid, base=base, oss_key=media_key if has_oss else None)
        thumb_url = segment_media_url(sid, "thumb", base=base)
    # 未发布不返回 download_url；发布后优先 CDN，否则同源代理下载
    download_url = None
    if has_oss:
        # Keep the API action URL so CDN failures can fall back to signed OSS.
        download_url = segment_media_url(sid, "download", base=base)
    return Segment(
        segment_id=sid,
        job_id=seg["task_id"],
        index=seg["index_num"],
        start_time=seg["start_time"],
        end_time=seg["end_time"],
        start_frame=seg.get("start_frame"),
        end_frame=seg.get("end_frame"),
        oss_key=oss_key,
        oss_url=_safe_oss_url(oss_key) if oss_key and has_oss else None,
        download_url=download_url,
        preview_url=preview_url,
        thumb_url=thumb_url,
        download_filename=_segment_download_name(seg, source_filename),
        source=SegmentSource(seg["source"]),
        confidence=seg.get("confidence"),
        summary=seg.get("summary"),
        status=seg.get("status", "pending"),
    )


def _job_to_model(task: dict, *, base: str = "", include_segments: bool = True) -> Job:
    segments: list[Segment] = []
    if include_segments:
        source_filename = _video_filename(task.get("video_id"))
        segments = [
            _segment_to_model(s, base=base, source_filename=source_filename)
            for s in db.get_segments(task["id"])
        ]
    status = JobStatus(task["status"])
    job_id = task["id"]
    done = status == JobStatus.DONE
    return Job(
        job_id=job_id,
        video_id=task["video_id"],
        mode=JobMode(task["mode"]),
        status=status,
        progress=task.get("progress", 0),
        message=task.get("message", ""),
        created_at=_parse_datetime(task["created_at"]),
        segments=segments,
        publish_action=(
            ApiAction(
                method="POST",
                href=absolutize(f"/api/v1/jobs/{job_id}/publish", base) or "",
            )
            if done
            else None
        ),
    )


def _submitted(task: dict) -> JobSubmitted:
    return JobSubmitted(
        job_id=task["id"],
        video_id=task["video_id"],
        mode=JobMode(task["mode"]),
        status=JobStatus(task["status"]),
    )


async def create_manual(
    *,
    video_id: str,
    segments: list[SegmentInput],
) -> JobSubmitted:
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    job_id = generate_id()
    try:
        db.create_task(job_id, video_id, TaskMode.MANUAL)
    except CapacityError as exc:
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    db.update_task(
        job_id,
        status=TaskStatus.PENDING,
        message="已入队，等待后台处理...",
    )
    try:
        enqueue_manual(job_id, _to_internal_segments(segments) or [])
    except QueueUnavailableError as exc:
        db.update_task(job_id, status=TaskStatus.FAILED, message=str(exc))
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    task = db.get_task(job_id)
    return _submitted(task)


async def create_auto(
    *,
    video_id: str,
    detector: str = "content",
    threshold: float = 35.0,
    min_scene_len: float = 2.0,
    sample_fps: float | None = None,
) -> JobSubmitted:
    from backend.config import ARK_API_KEY
    from backend.core.scene_detector import SUPPORTED_DETECTORS

    if detector not in SUPPORTED_DETECTORS:
        raise_api_error(400, CODE_BAD_REQUEST, f"不支持的检测算法: {detector}")
    if detector == "semantic" and not ARK_API_KEY:
        raise_api_error(
            400,
            CODE_BAD_REQUEST,
            "语义分镜未配置 ARK_API_KEY，请设置火山方舟 API Key 后重试",
        )
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    job_id = generate_id()
    try:
        db.create_task(job_id, video_id, TaskMode.AUTO)
    except CapacityError as exc:
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    db.update_task(
        job_id,
        status=TaskStatus.PENDING,
        message="已入队，等待后台处理...",
    )
    try:
        enqueue_auto_detect(
            job_id,
            detector,
            threshold,
            min_scene_len,
            False,
            sample_fps,
        )
    except QueueUnavailableError as exc:
        db.update_task(job_id, status=TaskStatus.FAILED, message=str(exc))
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    task = db.get_task(job_id)
    return _submitted(task)


async def get_job(job_id: str, *, base: str = "") -> Job:
    task = db.get_task(job_id)
    if not task:
        raise_api_error(404, CODE_JOB_NOT_FOUND, "任务不存在")
    return _job_to_model(task, base=base)


async def list_jobs_by_video(video_id: str, *, base: str = "") -> list[Job]:
    video = db.get_video(video_id)
    if not video:
        raise_api_error(404, CODE_VIDEO_NOT_FOUND, "视频不存在")
    return [_job_to_model(t, base=base) for t in db.get_tasks_by_video(video_id)]


async def start_cut(
    job_id: str,
    segments: list[SegmentInput] | None = None,
) -> JobSubmitted:
    """仅自动 Job：preview 后切割，或失败/完成后重切。手动请再调 POST /jobs/manual。"""
    task = db.get_task(job_id)
    if not task:
        raise_api_error(404, CODE_JOB_NOT_FOUND, "任务不存在")
    mode = task["mode"]
    if mode == TaskMode.MANUAL.value:
        raise_api_error(
            400,
            CODE_BAD_REQUEST,
            "手动任务不支持 /cut；请再次调用 POST /jobs/manual 创建新 Job",
        )
    if mode != TaskMode.AUTO.value:
        raise_api_error(400, CODE_BAD_REQUEST, f"不支持的任务模式: {mode}")

    internal = _to_internal_segments(segments)
    allow = {
        TaskStatus.PREVIEW.value,
        TaskStatus.FAILED.value,
        TaskStatus.DONE.value,
    }
    if task["status"] not in allow:
        raise_api_error(
            400,
            CODE_JOB_NOT_READY,
            f"任务当前状态不可 cut（当前：{task['status']}）。"
            "请先完成分镜检测（preview）；切割完成或失败后可再 cut。",
        )
    retrying = task["status"] in (TaskStatus.FAILED.value, TaskStatus.DONE.value)
    claimed = db.claim_task_status(
        job_id,
        from_statuses=list(allow),
        to_status=TaskStatus.PROCESSING,
        progress=0,
        message="重新切割..." if retrying else "开始切割...",
    )
    if not claimed:
        raise_api_error(
            409,
            CODE_CONFLICT,
            "任务状态已变化，请刷新后重试（可能已有切割任务在进行）",
        )
    try:
        enqueue_auto_cut(job_id, internal)
    except QueueUnavailableError as exc:
        db.update_task(job_id, status=TaskStatus.FAILED, message=str(exc))
        raise_api_error(503, CODE_QUEUE_UNAVAILABLE, str(exc))
    return _submitted(db.get_task(job_id))


async def publish(
    job_id: str,
    segment_ids: list[str] | None = None,
    oss_key: str | None = None,
) -> PublishResponse:
    if not oss_client.is_enabled():
        raise_api_error(
            503,
            CODE_OSS_DISABLED,
            "OSS 未启用：请配置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / "
            "OSS_ENDPOINT / OSS_BUCKET 后重试",
        )
    task = db.get_task(job_id)
    if not task:
        raise_api_error(404, CODE_JOB_NOT_FOUND, "任务不存在")
    if task["status"] != TaskStatus.DONE.value:
        raise_api_error(400, CODE_JOB_NOT_READY, "任务尚未完成，无法返回切片对象列表")
    segments = db.get_segments(job_id)
    if not segments:
        raise_api_error(404, CODE_NOT_FOUND, "没有可返回的切片对象")

    if segment_ids:
        seg_map = {seg["id"]: seg for seg in segments}
        missing = [sid for sid in segment_ids if sid not in seg_map]
        if missing:
            raise_api_error(400, CODE_INVALID_SEGMENT_IDS, "包含无效的片段 ID")
        targets = [seg_map[sid] for sid in segment_ids]
    else:
        targets = segments

    def object_item(seg: dict) -> PublishedSegmentItem:
        key = seg.get("oss_key") or seg.get("staging_oss_key")
        if not key:
            raise_api_error(404, CODE_NOT_FOUND, f"片段 {seg['id']} 尚未生成 OSS 对象")
        return PublishedSegmentItem(
            segment_id=seg["id"],
            index=int(seg["index_num"]),
            oss_key=str(key),
            oss_url=_safe_oss_url(str(key)),
        )

    all_segments = db.get_segments(job_id)
    all_objects = [object_item(seg) for seg in all_segments]
    selected_objects = [object_item(seg) for seg in targets]
    return PublishResponse(all=all_objects, selected=selected_objects)
