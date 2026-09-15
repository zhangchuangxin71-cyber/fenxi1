import logging
import time
import traceback

from backend.core.cutter import cut_segments_to_oss
from backend.core.scene_detector import detect_scenes, postprocess_scenes
from backend.core.validator import validate_segments
from backend.models.schemas import SegmentInput, SegmentSource, TaskStatus
from backend.storage.database import db
from backend.storage.file_manager import generate_id
from backend.storage.workspace import (
    cleanup_on_task_failure,
    source_media_ref,
    temporary_detection_proxy,
)


logger = logging.getLogger(__name__)


def _idempotent_begin(
    task_id: str,
    *,
    claim_from: list[str],
    claim_to: TaskStatus,
    continue_if: list[str] | None = None,
) -> bool:
    """
    任务入口幂等：
    - 已 done → 跳过（True 表示应继续执行，False 表示直接 return）
    - CAS 抢占 claim_from → claim_to
    - 若已被置为 continue_if（如崩溃重投递仍在 processing）→ 继续执行
    """
    task = db.get_task(task_id)
    if not task:
        raise RuntimeError(
            f"任务 {task_id} 在内存账本中不存在（服务重启后旧 ID 会失效）"
        )
    status = task["status"]
    if status == TaskStatus.DONE.value:
        logger.info("idempotent skip done task_id=%s", task_id)
        return False

    claimed = db.claim_task_status(
        task_id,
        from_statuses=claim_from,
        to_status=claim_to,
        progress=0 if claim_to == TaskStatus.PROCESSING else None,
        message=None,
    )
    if claimed:
        return True

    task = db.get_task(task_id)
    status = (task or {}).get("status")
    if status == TaskStatus.DONE.value:
        logger.info("idempotent skip done-after-claim task_id=%s", task_id)
        return False
    allow = set(continue_if or [])
    allow.add(claim_to.value)
    if status in allow:
        logger.info(
            "idempotent continue in-progress task_id=%s status=%s",
            task_id,
            status,
        )
        return True
    logger.warning(
        "idempotent skip unexpected status task_id=%s status=%s",
        task_id,
        status,
    )
    return False


def _fail_missing_source(task_id: str, video: dict | None, video_path) -> bool:
    """源片不可用时写失败并返回 True（调用方应 return）。"""
    if video and video_path:
        return False
    if not video:
        msg = "视频不存在"
    else:
        msg = "源片不可访问（请检查 OSS 对象、网络与凭证）"
    db.update_task(task_id, status=TaskStatus.FAILED, message=msg)
    cleanup_on_task_failure(task_id)
    return True


def _invalidate_published_segments(task_id: str) -> None:
    """重切前清理旧引用；clips 预存对象不因重切立即删除。"""
    db.clear_segment_oss_keys(task_id)
    db.clear_segment_staging_keys(task_id)


def _segments_to_cut_list(segments: list[dict], fps: float) -> list[tuple]:
    items = []
    for seg in segments:
        sf = seg.get("start_frame")
        ef = seg.get("end_frame")
        if sf is None or ef is None:
            sf = max(0, round(seg["start_time"] * fps))
            ef = max(sf + 1, round(seg["end_time"] * fps))
        items.append((seg["index_num"], seg["start_time"], seg["end_time"], int(sf), int(ef)))
    return items


def _clips_prefix_for_video(video: dict | None) -> str | None:
    source_key = str((video or {}).get("oss_key") or "").strip()
    if not source_key:
        return None
    from backend.storage import oss_client

    return oss_client.clips_prefix(source_key)


def _format_cut_errors(stats: dict) -> str:
    errors = stats.get("errors") or {}
    if not errors:
        return f"{stats['failed']} 个片段切割失败: {stats['failed_indexes'][:5]}"
    samples = []
    for idx in stats["failed_indexes"][:3]:
        msg = errors.get(idx, "")
        samples.append(f"#{idx}:{msg}" if msg else f"#{idx}")
    return f"{stats['failed']} 个片段切割失败（{'; '.join(samples)}）"


def _finalize_cut_success(task_id: str, video: dict, success_message: str) -> None:
    """切割成功：成品已在 OSS staging，等待用户发布。"""
    db.update_task(
        task_id,
        status=TaskStatus.DONE,
        progress=100,
        message=success_message,
    )


def _finalize_cut_failure(task_id: str, video: dict | None, message: str) -> None:
    db.update_task(task_id, status=TaskStatus.FAILED, message=message)
    # 失败清理未完成 staging，不触碰源片和已发布媒资
    cleanup_on_task_failure(task_id)


def run_manual_task(task_id: str, segments: list[SegmentInput]) -> None:
    if not _idempotent_begin(
        task_id,
        claim_from=[TaskStatus.PENDING.value, TaskStatus.FAILED.value],
        claim_to=TaskStatus.PROCESSING,
        continue_if=[TaskStatus.PROCESSING.value],
    ):
        return

    task = db.get_task(task_id)
    if not task:
        return

    video = db.get_video(task["video_id"])
    try:
        video_path = source_media_ref(task["video_id"])
    except Exception as exc:
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message=f"源片下载失败: {exc}",
        )
        cleanup_on_task_failure(task_id)
        return
    if _fail_missing_source(task_id, video, video_path):
        return

    try:
        db.update_task(task_id, status=TaskStatus.PROCESSING, progress=5, message="校验片段...")
        validated = validate_segments(segments, video["duration"])
        fps = float(video.get("fps") or 25.0)

        # 重投递 / 重切：先清旧片段与已发布标记，避免重复行与旧 OSS
        _invalidate_published_segments(task_id)
        db.delete_segments(task_id)

        segment_records = []
        for i, seg in enumerate(validated, start=1):
            sf = max(0, round(seg.start * fps))
            ef = max(sf + 1, round(seg.end * fps))
            segment_records.append({
                "id": generate_id(),
                "index": i,
                "start_time": seg.start,
                "end_time": seg.end,
                "start_frame": sf,
                "end_frame": ef,
                "source": SegmentSource.MANUAL.value,
                "status": "pending",
            })
        db.create_segments(task_id, segment_records)

        stored_segments = db.get_segments(task_id)

        def on_progress(pct: float):
            overall = min(99.0, 10 + pct * 0.9)
            db.update_task(
                task_id,
                progress=overall,
                message="正在并行切割...",
            )

        staged, stats = cut_segments_to_oss(
            str(video_path),
            task_id,
            _segments_to_cut_list(stored_segments, fps),
            on_progress,
            fps=fps,
            clips_prefix=_clips_prefix_for_video(video),
        )

        for seg in db.get_segments(task_id):
            item = staged.get(int(seg["index_num"]))
            if item:
                db.update_segment(
                    seg["id"],
                    output_path="",
                    staging_oss_key=item["staging_oss_key"],
                    thumb_oss_key=item.get("thumb_oss_key"),
                    status="done",
                    oss_key=item["staging_oss_key"],
                )
            else:
                db.update_segment(seg["id"], status="failed", oss_key=None)

        if stats["failed"]:
            _finalize_cut_failure(task_id, video, _format_cut_errors(stats))
            return

        _finalize_cut_success(task_id, video, "切割完成")
    except Exception as exc:
        _finalize_cut_failure(
            task_id,
            video,
            str(exc) or traceback.format_exc()[-500:],
        )


def run_auto_detect(
    task_id: str,
    detector: str,
    threshold: float,
    min_scene_len: float,
    auto_cut: bool = False,
    sample_fps: float | None = None,
) -> None:
    if not _idempotent_begin(
        task_id,
        claim_from=[TaskStatus.PENDING.value, TaskStatus.FAILED.value],
        claim_to=TaskStatus.DETECTING,
        continue_if=[TaskStatus.DETECTING.value, TaskStatus.PROCESSING.value],
    ):
        return

    task = db.get_task(task_id)
    if not task:
        return

    video = db.get_video(task["video_id"])
    try:
        video_path = source_media_ref(task["video_id"])
    except Exception as exc:
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message=f"源片下载失败: {exc}",
        )
        cleanup_on_task_failure(task_id)
        return
    if _fail_missing_source(task_id, video, video_path):
        return

    try:
        init_msg = "正在检测分镜... 初始化"
        if detector == "semantic":
            init_msg = "正在调用豆包语义分镜..."
        db.update_task(task_id, status=TaskStatus.DETECTING, progress=10, message=init_msg)

        last_db_update = [0.0]

        def on_detect_progress(pct: float, message: str):
            now = time.time()
            if now - last_db_update[0] >= 0.8:
                db.update_task(task_id, status=TaskStatus.DETECTING, progress=pct, message=message)
                last_db_update[0] = now

        detect_threshold = threshold
        if detector == "semantic" and sample_fps is not None:
            detect_threshold = float(sample_fps)

        if detector == "semantic":
            with temporary_detection_proxy(str(video_path), task_id) as proxy:
                scenes, detect_fps = detect_scenes(
                    str(proxy), detector=detector, threshold=detect_threshold,
                    min_scene_len=min_scene_len, on_progress=on_detect_progress,
                )
        else:
            try:
                scenes, detect_fps = detect_scenes(
                    str(video_path), detector=detector, threshold=detect_threshold,
                    min_scene_len=min_scene_len, on_progress=on_detect_progress,
                )
            except Exception:
                logger.warning("remote scene detection failed; using proxy", exc_info=True)
                with temporary_detection_proxy(str(video_path), task_id) as proxy:
                    scenes, detect_fps = detect_scenes(
                        str(proxy), detector=detector, threshold=detect_threshold,
                        min_scene_len=min_scene_len, on_progress=on_detect_progress,
                    )
        if detector != "semantic":
            scenes = postprocess_scenes(
                scenes,
                min_scene_len=min_scene_len,
                fps=detect_fps,
                detector=detector,
            )

        if not scenes:
            db.update_task(
                task_id,
                status=TaskStatus.FAILED,
                message=(
                    "未检测到有效分镜。请降低「差异阈值」后重试；"
                    "阈值过高时画面变化可能都被忽略。"
                ),
            )
            return

        if video and abs(float(video.get("fps") or 0) - detect_fps) > 0.01:
            db.update_video_fps(task["video_id"], detect_fps)

        _invalidate_published_segments(task_id)
        db.delete_segments(task_id)
        segment_records = []
        for i, scene in enumerate(scenes, start=1):
            segment_records.append({
                "id": generate_id(),
                "index": i,
                "start_time": scene.start,
                "end_time": scene.end,
                "start_frame": scene.start_frame,
                "end_frame": scene.end_frame,
                "source": SegmentSource.AUTO.value,
                "confidence": scene.confidence,
                "summary": getattr(scene, "summary", None),
                "status": "preview",
            })
        db.create_segments(task_id, segment_records)

        if auto_cut:
            db.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=0,
                message=f"检测完成（{len(scenes)} 个分镜），正在自动切割...",
            )
            run_auto_cut(task_id, None, cut_fps=detect_fps)
            return

        db.update_task(
            task_id,
            status=TaskStatus.PREVIEW,
            progress=100,
            message=f"✓ 检测完成，共 {len(scenes)} 个分镜，请确认后切割",
        )
    except Exception as exc:
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message=str(exc) or traceback.format_exc()[-500:],
        )
        cleanup_on_task_failure(task_id)


def run_auto_cut(
    task_id: str,
    segments: list[SegmentInput] | None = None,
    cut_fps: float | None = None,
) -> None:
    # API start_cut 已 CAS 到 processing；此处再抢一次，并跳过已 done 的重投递
    if not _idempotent_begin(
        task_id,
        claim_from=[
            TaskStatus.PROCESSING.value,
            TaskStatus.FAILED.value,
            TaskStatus.PREVIEW.value,
        ],
        claim_to=TaskStatus.PROCESSING,
        continue_if=[TaskStatus.PROCESSING.value],
    ):
        return

    task = db.get_task(task_id)
    if not task:
        return

    video = db.get_video(task["video_id"])
    try:
        video_path = source_media_ref(task["video_id"])
    except Exception as exc:
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message=f"源片下载失败: {exc}",
        )
        cleanup_on_task_failure(task_id)
        return
    if _fail_missing_source(task_id, video, video_path):
        return

    try:
        fps = float(cut_fps or video.get("fps") or 25.0)

        if segments:
            db.update_task(task_id, status=TaskStatus.PROCESSING, progress=5, message="更新片段...")
            validated = validate_segments(segments, video["duration"])
            _invalidate_published_segments(task_id)
            db.delete_segments(task_id)
            segment_records = []
            for i, seg in enumerate(validated, start=1):
                sf = seg.start_frame if seg.start_frame is not None else max(0, round(seg.start * fps))
                ef = seg.end_frame if seg.end_frame is not None else max(int(sf) + 1, round(seg.end * fps))
                segment_records.append({
                    "id": generate_id(),
                    "index": i,
                    "start_time": seg.start,
                    "end_time": seg.end,
                    "start_frame": int(sf),
                    "end_frame": int(ef),
                    "source": SegmentSource.AUTO.value,
                    "summary": seg.summary,
                    "status": "pending",
                })
            db.create_segments(task_id, segment_records)
        else:
            # 复用已有分镜重切：必须失效旧 oss_key，否则 publish 会跳过上传
            _invalidate_published_segments(task_id)

        stored_segments = db.get_segments(task_id)
        if not stored_segments:
            _finalize_cut_failure(task_id, video, "没有可切割的片段")
            return

        db.update_task(task_id, status=TaskStatus.PROCESSING, progress=10, message="正在切割...")

        def on_progress(pct: float):
            # pct 为切割阶段 0–100；总进度映射到 10–100，勿把阶段百分比写进文案
            overall = min(99.0, 10 + pct * 0.9)
            db.update_task(
                task_id,
                progress=overall,
                message="正在并行切割...",
            )

        staged, stats = cut_segments_to_oss(
            str(video_path),
            task_id,
            _segments_to_cut_list(stored_segments, fps),
            on_progress,
            fps=fps,
            clips_prefix=_clips_prefix_for_video(video),
        )

        for seg in db.get_segments(task_id):
            item = staged.get(int(seg["index_num"]))
            if item:
                db.update_segment(
                    seg["id"],
                    output_path="",
                    staging_oss_key=item["staging_oss_key"],
                    thumb_oss_key=item.get("thumb_oss_key"),
                    status="done",
                    oss_key=item["staging_oss_key"],
                )
            else:
                db.update_segment(seg["id"], status="failed", oss_key=None)

        if stats["failed"]:
            _finalize_cut_failure(task_id, video, _format_cut_errors(stats))
            return

        _finalize_cut_success(
            task_id,
            video,
            f"切割完成（{stats['success']} 个片段）",
        )
    except Exception as exc:
        _finalize_cut_failure(
            task_id,
            video,
            str(exc) or traceback.format_exc()[-500:],
        )
