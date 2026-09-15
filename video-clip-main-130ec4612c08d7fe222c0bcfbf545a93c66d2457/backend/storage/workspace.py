"""OSS-native 工作区与兼容本地缓存、派生物及生命周期清理。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.config import (
    ALLOWED_EXTENSIONS,
    DATA_DIR,
    DISK_FREE_GB_MIN,
    LOCAL_RETENTION_HOURS,
    OUTPUT_DIR,
    UPLOAD_DIR,
    OSS_PREFIX,
    OSS_PROCESS_SIGN_EXPIRES,
    OSS_INGEST_PREFIX,
    OSS_DERIVED_PREFIX,
    OSS_MANAGED_SOURCE_RETENTION_HOURS,
    OSS_STAGING_PREFIX,
    OSS_STAGING_RETENTION_HOURS,
    STALE_TASK_HOURS,
    WORK_DIR,
)
from backend.storage.file_manager import (
    delete_task_outputs,
    delete_video_file,
    generate_id,
    get_task_output_dir,
    get_video_path,
)
from backend.storage import oss_client
from backend.storage.http_download import (
    HttpDownloadError,
    download_http_to,
    filename_from_url,
    validate_public_http_url,
)

logger = logging.getLogger(__name__)
_CLEANUP_LOCK = threading.Lock()


def ensure_work_dirs() -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)


def work_video_dir(video_id: str) -> Path:
    path = WORK_DIR / video_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_work_video_path(video_id: str) -> Path | None:
    d = WORK_DIR / video_id
    if not d.is_dir():
        return None
    for path in d.glob("source.*"):
        if path.is_file():
            return path
    return None


def resolve_local_video_path(video_id: str) -> Path | None:
    """优先 uploads，其次 work 缓存。"""
    path = get_video_path(video_id)
    if path and path.exists():
        return path
    return get_work_video_path(video_id)


def _guess_ext_from_key(key: str) -> str:
    suffix = Path(key).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return suffix
    return ".mp4"


def resolve_import_oss_key(
    *,
    oss_key: str | None = None,
    oss_url: str | None = None,
) -> str:
    """校验并解析导入用的 OSS object key（仅 oss_key；url 请走 HTTP 导入）。"""
    if oss_url:
        raise oss_client.OssError("url 导入请使用 HTTP 下载路径，勿再解析为 OSS key")
    if not oss_client.is_enabled():
        raise oss_client.OssError(
            "OSS 未启用：请配置 OSS 凭证后重试"
        )
    key = oss_client.parse_oss_ref(oss_key=oss_key, oss_url=None)
    outputs = f"{OSS_PREFIX}outputs/"
    if key.startswith(outputs):
        raise oss_client.OssError("不能导入切割成品目录中的文件作为源片")
    return key


def resolve_import_http_url(oss_url: str) -> str:
    """校验公开 http(s) 导入 URL（含 SSRF）。"""
    return validate_public_http_url(oss_url)


def download_oss_to_video(video_id: str, oss_key: str) -> Path:
    """将已知 oss_key 下载到 data/work/{video_id}/source.ext。"""
    if not oss_client.is_enabled():
        raise oss_client.OssError(
            "OSS 未启用：请配置 OSS 凭证后重试"
        )
    ext = _guess_ext_from_key(oss_key)
    local_path = work_video_dir(video_id) / f"source{ext}"
    oss_client.download_to(oss_key, local_path)
    return local_path


def download_http_to_video(video_id: str, source_url: str) -> Path:
    """将公开 http(s) URL 下载到 data/work/{video_id}/source.ext。"""
    name = filename_from_url(source_url)
    ext = _guess_ext_from_key(name)
    local_path = work_video_dir(video_id) / f"source{ext}"
    download_http_to(source_url, local_path)
    return local_path


def import_oss_source(
    *,
    oss_key: str | None = None,
    oss_url: str | None = None,
) -> tuple[str, Path, str]:
    """
    同步导入源片到 data/work/{video_id}/source.ext。
    - oss_key：走配置桶 OSS 下载，返回 (video_id, path, object_key)
    - oss_url：走 HTTP 下载，返回 (video_id, path, url)
    """
    video_id = generate_id()
    if oss_url:
        url = resolve_import_http_url(oss_url)
        local_path = download_http_to_video(video_id, url)
        return video_id, local_path, url
    key = resolve_import_oss_key(oss_key=oss_key, oss_url=None)
    local_path = download_oss_to_video(video_id, key)
    return video_id, local_path, key


def ensure_source_local(video_id: str) -> Path | None:
    """
    保证源片在本地可读。
    本地缺失时：优先 source_url 走 HTTP 重下；否则按 oss_key 从 OSS 重下。
    """
    existing = resolve_local_video_path(video_id)
    if existing and existing.exists():
        return existing

    from backend.storage.database import db

    video = db.get_video(video_id)
    if not video:
        return None

    source_url = (video.get("source_url") or "").strip() or None
    oss_key = video.get("oss_key")
    local_path: Path | None = None
    try:
        if source_url:
            local_path = download_http_to_video(video_id, source_url)
        elif oss_key and oss_client.is_enabled():
            ext = _guess_ext_from_key(oss_key)
            local_path = work_video_dir(video_id) / f"source{ext}"
            oss_client.download_to(oss_key, local_path)
        else:
            stored = video.get("path")
            if stored and Path(stored).exists():
                return Path(stored)
            return None
    except (oss_client.OssError, HttpDownloadError):
        logger.exception("ensure_source_local failed video_id=%s", video_id)
        raise

    if local_path is None:
        return None
    try:
        db.update_video_path(video_id, str(local_path))
    except Exception:
        logger.warning("update_video_path failed video_id=%s", video_id)
    return local_path


def source_media_ref(video_id: str) -> str | None:
    """返回可由 FFmpeg/FFprobe 读取的源：优先 OSS 签名 URL，不落整片。"""
    from backend.storage.database import db

    video = db.get_video(video_id)
    if not video:
        return None
    key = (video.get("oss_key") or "").strip()
    if key and oss_client.is_enabled():
        return oss_client.sign_url(
            key, expires=OSS_PROCESS_SIGN_EXPIRES, prefer_cdn=False
        )
    source_url = (video.get("source_url") or "").strip()
    if source_url:
        return source_url
    local = resolve_local_video_path(video_id)
    return str(local) if local else None


@contextmanager
def temporary_detection_proxy(source_ref: str, task_id: str):
    """生成低码率检测代理片；只在系统临时目录短期存在。"""
    from backend.core.cutter import FFmpegError, _resolve_ffmpeg

    with tempfile.TemporaryDirectory(prefix=f"clip_detect_{task_id[:8]}_") as tmp:
        out = Path(tmp) / "proxy.mp4"
        cmd = [
            _resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            source_ref,
            "-map",
            "0:v:0",
            "-vf",
            "scale=min(960\\,iw):-2",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-movflags",
            "+faststart",
            str(out),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            raise FFmpegError(
                f"生成检测代理片失败: {(result.stderr or '')[-400:]}"
            )
        yield out


def cleanup_task_outputs_only(task_id: str) -> None:
    """仅清理切割输出目录，保留源片 work 缓存便于重试/继续预览。"""
    delete_task_outputs(task_id)


def cleanup_task_staging(task_id: str) -> None:
    """删除旧版任务 staging 对象；新的 clips 预存对象必须保留。"""
    from backend.storage.database import db

    for seg in db.get_segments(task_id):
        staging_key = seg.get("staging_oss_key")
        staging_thumb = seg.get("thumb_oss_key") if staging_key else None
        for key in (staging_key, staging_thumb):
            if (
                key
                and key.startswith(OSS_STAGING_PREFIX)
                and oss_client.is_enabled()
            ):
                oss_client.delete_object(key)
        if staging_key:
            db.update_segment(seg["id"], status="failed")
    db.clear_segment_staging_keys(task_id)


def cleanup_video_local(video_id: str) -> None:
    """删除本地 uploads / work / 波形胶片缓存。"""
    delete_video_file(video_id)
    work = WORK_DIR / video_id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)


def cleanup_after_oss_task(video_id: str, task_id: str) -> None:
    """任务成功结束：清理 outputs + 源片本地缓存（DB 保留 oss_key）。"""
    cleanup_task_outputs_only(task_id)
    cleanup_video_local(video_id)
    from backend.storage.database import db

    try:
        db.update_video_path(video_id, "")
    except Exception:
        pass


def _delete_segment_thumb(segment_id: str) -> None:
    thumb = OUTPUT_DIR / "_thumbs" / f"{segment_id}.jpg"
    if thumb.exists():
        thumb.unlink(missing_ok=True)


def _clear_segment_local_file(seg: dict) -> None:
    """删除片段本地成品 / 海报 / 缩略图，并清空 output_path。"""
    from backend.storage.database import db

    path = seg.get("output_path")
    if path:
        local = Path(path)
        if local.exists():
            local.unlink(missing_ok=True)
        poster = local.with_suffix(".jpg")
        if poster.exists():
            poster.unlink(missing_ok=True)
        try:
            db.update_segment(seg["id"], output_path="")
        except Exception:
            logger.warning(
                "clear output_path failed segment_id=%s", seg.get("id")
            )
    _delete_segment_thumb(str(seg["id"]))


def cleanup_local_after_publish(video_id: str, task_id: str) -> None:
    """
    返回 OSS 对象列表后清理本地（支持部分选择）：
    - 已有 oss_key 的片段立即删本地成品 / 海报 / 缩略图
    - 全部片段均已上云后删除 outputs 目录
    - 同视频无需本地源片时再清理 work 缓存
    """
    from backend.storage.database import db

    for seg in db.get_segments(task_id):
        if not seg.get("oss_key"):
            continue
        _clear_segment_local_file(seg)

    segments = db.get_segments(task_id)
    if segments and all(s.get("oss_key") for s in segments):
        cleanup_task_outputs_only(task_id)
    else:
        # 部分上云：尝试去掉已空的 outputs 残留目录外的孤立空文件
        out_dir = OUTPUT_DIR / task_id
        if out_dir.is_dir() and not any(out_dir.iterdir()):
            shutil.rmtree(out_dir, ignore_errors=True)

    _maybe_cleanup_idle_video_work(video_id)


def cleanup_on_task_failure(task_id: str) -> None:
    """任务失败：清本地 outputs 与未完成 staging。"""
    cleanup_task_outputs_only(task_id)
    cleanup_task_staging(task_id)


def cleanup_expired_managed_oss() -> dict:
    """清理重启后失去内存记录的 staging/ingest 对象。"""
    from backend.storage.database import db

    if not oss_client.is_enabled():
        return {"staging": 0, "ingest": 0, "derived": 0}
    keep_staging: set[str] = set()
    for task_id in db.list_task_ids():
        for seg in db.get_segments(task_id):
            keep_staging.update(
                key for key in (seg.get("staging_oss_key"), seg.get("thumb_oss_key")) if key
            )
    keep_ingest = {
        str(v.get("oss_key"))
        for status in ("importing", "ready", "failed")
        for v in db.list_videos_by_status(status)
        if v.get("managed_source") and v.get("oss_key")
    }
    keep_derived = {
        str(v.get("filmstrip_oss_key"))
        for status in ("importing", "ready", "failed")
        for v in db.list_videos_by_status(status)
        if v.get("filmstrip_oss_key")
    }
    return {
        "staging": oss_client.delete_prefix_older_than(
            OSS_STAGING_PREFIX,
            OSS_STAGING_RETENTION_HOURS,
            keep_keys=keep_staging,
        ),
        "ingest": oss_client.delete_prefix_older_than(
            OSS_INGEST_PREFIX,
            OSS_MANAGED_SOURCE_RETENTION_HOURS,
            keep_keys=keep_ingest,
        ),
        "derived": oss_client.delete_prefix_older_than(
            OSS_DERIVED_PREFIX,
            OSS_MANAGED_SOURCE_RETENTION_HOURS,
            keep_keys=keep_derived,
        ),
    }


def reclaim_expired_staging() -> dict:
    """让仍在内存中的未发布 staging 也遵守保留期。"""
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    if OSS_STAGING_RETENTION_HOURS <= 0:
        return {"expired_tasks": 0}
    now = datetime.now(timezone.utc)
    expired = 0
    for task in db.list_tasks_by_statuses([TaskStatus.DONE.value]):
        created = _parse_task_created_at(task.get("created_at"))
        if not created:
            continue
        age_h = (now - created.astimezone(timezone.utc)).total_seconds() / 3600
        segments = db.get_segments(task["id"])
        legacy_staging = [
            s for s in segments
            if str(s.get("staging_oss_key") or "").startswith(OSS_STAGING_PREFIX)
        ]
        if age_h < OSS_STAGING_RETENTION_HOURS or not legacy_staging:
            continue
        cleanup_task_staging(task["id"])
        db.update_task(
            task["id"],
            status=TaskStatus.FAILED,
            message="OSS staging 已过保留期，请重新切割",
        )
        expired += 1
    return {"expired_tasks": expired}


def should_upload_outputs_to_oss(video: dict | None) -> bool:
    """成品仅保存至 OSS；源片统一从 OSS 导入。"""
    return oss_client.is_enabled()


def upload_task_segments_to_oss(
    task_id: str,
    segments: list[dict],
    *,
    source_filename: str | None = None,
    use_download_names: bool = False,
    oss_prefix: str | None = None,
) -> list[dict]:
    """
    将本地切割成品上传到 OSS。
    oss_prefix：前端指定的目录前缀；为空则回退默认 OSS_PREFIX/outputs/{task_id}/。
    中途失败会删除本批次已上传对象，避免 OSS 孤儿文件。
    """
    from backend.utils.naming import segment_download_filename

    results: list[dict] = []
    uploaded_keys: list[str] = []
    try:
        for seg in segments:
            local = seg.get("output_path")
            if not local or not Path(local).exists():
                continue
            local_path = Path(local)
            if use_download_names:
                object_name = segment_download_filename(
                    source_filename,
                    task_id,
                    int(seg["index_num"]),
                    float(seg["start_time"]),
                    float(seg["end_time"]),
                )
            else:
                object_name = local_path.name
            key = oss_client.output_object_key(
                task_id, object_name, prefix=oss_prefix
            )
            oss_client.upload_file(local_path, key)
            uploaded_keys.append(key)
            results.append(
                {
                    "id": seg["id"],
                    "oss_key": key,
                    "output_path": str(local_path),
                }
            )
        return results
    except Exception:
        for key in uploaded_keys:
            try:
                oss_client.delete_object(key)
            except Exception:
                logger.warning("rollback OSS object failed key=%s", key)
        raise


def _parse_task_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _path_mtime(path: Path) -> datetime | None:
    try:
        if path.exists():
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return None


def _task_artifact_age_hours(task: dict, now: datetime) -> float | None:
    """优先用 outputs 目录 mtime（更接近切割完成时间），否则用 created_at。"""
    out = OUTPUT_DIR / task["id"]
    ref = _path_mtime(out) if out.exists() else None
    if ref is None:
        ref = _parse_task_created_at(task.get("created_at"))
    if ref is None:
        return None
    return (now - ref.astimezone(timezone.utc)).total_seconds() / 3600.0


def _statuses_need_local_source() -> set[str]:
    from backend.models.schemas import TaskStatus

    return {
        TaskStatus.PENDING.value,
        TaskStatus.DETECTING.value,
        TaskStatus.PROCESSING.value,
        TaskStatus.PREVIEW.value,
    }


def _video_needs_local_source(video_id: str) -> bool:
    """进行中 / 预览中，或仍有本地成品的 done 任务，需要保留源片缓存。"""
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    need = _statuses_need_local_source()
    for task in db.get_tasks_by_video(video_id):
        status = task.get("status")
        if status in need:
            return True
        if status == TaskStatus.DONE.value and (OUTPUT_DIR / task["id"]).exists():
            return True
    return False


def _maybe_cleanup_idle_video_work(video_id: str) -> bool:
    """无本地需求时清理 work / uploads 源片缓存。"""
    from backend.storage.database import db

    if not video_id or _video_needs_local_source(video_id):
        return False
    cleanup_video_local(video_id)
    try:
        db.update_video_path(video_id, "")
    except Exception:
        logger.warning("update_video_path failed video_id=%s", video_id)
    return True


def reclaim_stale_tasks(*, max_age_hours: float | None = None) -> dict:
    """
    将长时间停留在 pending/detecting/processing 的任务标为失败，并清理 outputs。
    """
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    hours = STALE_TASK_HOURS if max_age_hours is None else float(max_age_hours)
    if hours <= 0:
        return {"reclaimed": 0, "hours": hours}

    now = datetime.now(timezone.utc)
    statuses = [
        TaskStatus.PENDING.value,
        TaskStatus.DETECTING.value,
        TaskStatus.PROCESSING.value,
    ]
    reclaimed = 0
    for task in db.list_tasks_by_statuses(statuses):
        created = _parse_task_created_at(task.get("created_at"))
        if not created:
            continue
        age_h = (now - created.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_h < hours:
            continue
        task_id = task["id"]
        db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message=f"任务超时未完成（>{hours:g}h），已自动回收",
        )
        cleanup_on_task_failure(task_id)
        reclaimed += 1
        logger.warning("reclaimed stale task id=%s age_h=%.1f", task_id, age_h)
    return {"reclaimed": reclaimed, "hours": hours}


def reclaim_stale_imports(*, max_age_hours: float | None = None) -> dict:
    """将长时间停留在 importing 的视频标为 failed，并清理本地缓存。"""
    from backend.storage.database import db

    hours = STALE_TASK_HOURS if max_age_hours is None else float(max_age_hours)
    if hours <= 0:
        return {"reclaimed_imports": 0, "hours": hours}

    now = datetime.now(timezone.utc)
    reclaimed = 0
    for video in db.list_videos_by_status("importing"):
        created = _parse_task_created_at(video.get("created_at"))
        if not created:
            continue
        age_h = (now - created.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_h < hours:
            continue
        video_id = video["id"]
        db.update_video_import(
            video_id,
            status="failed",
            progress=0,
            message=f"导入超时未完成（>{hours:g}h），已自动回收",
        )
        cleanup_video_local(video_id)
        reclaimed += 1
        logger.warning("reclaimed stale import video_id=%s age_h=%.1f", video_id, age_h)
    return {"reclaimed_imports": reclaimed, "hours": hours}


def reclaim_idle_local_artifacts(
    *,
    max_age_hours: float | None = None,
    ignore_age: bool = False,
    limit: int | None = None,
) -> dict:
    """
    回收 done/preview 的本地中间文件（不改任务状态）。
    - done：清 outputs；已发布片段仍可从 OSS 取；未发布需重切
    - preview：主要释放同视频空闲 work 源片（ensure_source_local 可再下）
    """
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    hours = (
        LOCAL_RETENTION_HOURS if max_age_hours is None else float(max_age_hours)
    )
    if not ignore_age and hours <= 0:
        return {
            "reclaimed_outputs": 0,
            "reclaimed_work": 0,
            "hours": hours,
            "ignore_age": ignore_age,
        }

    now = datetime.now(timezone.utc)
    statuses = [TaskStatus.DONE.value, TaskStatus.PREVIEW.value]
    candidates: list[tuple[float, dict, bool]] = []
    for task in db.list_tasks_by_statuses(statuses):
        age_h = _task_artifact_age_hours(task, now)
        if age_h is None:
            continue
        if not ignore_age and age_h < hours:
            continue
        has_out = (OUTPUT_DIR / task["id"]).exists()
        candidates.append((age_h, task, has_out))

    # 最旧优先（磁盘压力场景）
    candidates.sort(key=lambda item: item[0], reverse=True)

    reclaimed_outputs = 0
    reclaimed_work = 0
    touched_videos: set[str] = set()

    for age_h, task, has_out in candidates:
        if limit is not None and (reclaimed_outputs + reclaimed_work) >= limit:
            break
        task_id = task["id"]
        video_id = task.get("video_id") or ""
        if has_out:
            for seg in db.get_segments(task_id):
                _clear_segment_local_file(seg)
            cleanup_task_outputs_only(task_id)
            segs = db.get_segments(task_id)
            published = bool(segs) and all(bool(s.get("oss_key")) for s in segs)
            note = (
                f"本地成品已按保留策略清理（约 {age_h:.0f}h）"
                + (
                    "；已发布片段仍可从 OSS 获取"
                    if published
                    else "；未发布片段需重新切割"
                )
            )
            try:
                db.update_task(task_id, message=note)
            except Exception:
                pass
            reclaimed_outputs += 1
            logger.info(
                "reclaimed idle outputs task_id=%s age_h=%.1f ignore_age=%s",
                task_id,
                age_h,
                ignore_age,
            )
        if video_id:
            touched_videos.add(video_id)

    for video_id in touched_videos:
        if limit is not None and (reclaimed_outputs + reclaimed_work) >= limit:
            break
        if _maybe_cleanup_idle_video_work(video_id):
            reclaimed_work += 1
            logger.info("reclaimed idle work video_id=%s", video_id)

    # preview 无 outputs 时也可能只占 work：扫一遍仅 preview/done 的视频
    if limit is None or (reclaimed_outputs + reclaimed_work) < limit:
        for task in db.list_tasks_by_statuses(statuses):
            video_id = task.get("video_id") or ""
            if not video_id or video_id in touched_videos:
                continue
            age_h = _task_artifact_age_hours(task, now)
            if age_h is None:
                continue
            if not ignore_age and age_h < hours:
                continue
            if _maybe_cleanup_idle_video_work(video_id):
                reclaimed_work += 1
                touched_videos.add(video_id)
                logger.info(
                    "reclaimed idle work via preview/done video_id=%s age_h=%.1f",
                    video_id,
                    age_h,
                )
            if limit is not None and (reclaimed_outputs + reclaimed_work) >= limit:
                break

    return {
        "reclaimed_outputs": reclaimed_outputs,
        "reclaimed_work": reclaimed_work,
        "hours": hours,
        "ignore_age": ignore_age,
    }


def reclaim_by_disk_pressure() -> dict:
    """磁盘剩余不足时，忽略保留期，按最旧优先回收 done/preview 本地文件。"""
    if DISK_FREE_GB_MIN <= 0:
        return {"triggered": False, "reason": "disabled"}

    try:
        free_before = shutil.disk_usage(str(DATA_DIR)).free / (1024**3)
    except OSError as exc:
        return {"triggered": False, "error": str(exc)}

    if free_before >= DISK_FREE_GB_MIN:
        return {
            "triggered": False,
            "free_gb": round(free_before, 2),
            "min_gb": DISK_FREE_GB_MIN,
        }

    total_out = 0
    total_work = 0
    rounds = 0
    while rounds < 50:
        free_gb = shutil.disk_usage(str(DATA_DIR)).free / (1024**3)
        if free_gb >= DISK_FREE_GB_MIN:
            break
        batch = reclaim_idle_local_artifacts(ignore_age=True, limit=8)
        rounds += 1
        total_out += int(batch.get("reclaimed_outputs") or 0)
        total_work += int(batch.get("reclaimed_work") or 0)
        if (
            int(batch.get("reclaimed_outputs") or 0)
            + int(batch.get("reclaimed_work") or 0)
        ) == 0:
            break

    free_after = shutil.disk_usage(str(DATA_DIR)).free / (1024**3)
    logger.warning(
        "disk pressure cleanup free_before=%.2fGiB free_after=%.2fGiB "
        "min=%.2fGiB outputs=%s work=%s rounds=%s",
        free_before,
        free_after,
        DISK_FREE_GB_MIN,
        total_out,
        total_work,
        rounds,
    )
    return {
        "triggered": True,
        "free_gb": round(free_after, 2),
        "min_gb": DISK_FREE_GB_MIN,
        "reclaimed_outputs": total_out,
        "reclaimed_work": total_work,
        "rounds": rounds,
    }


def sweep_orphan_local_files() -> dict:
    """清理 DB 中不存在的 work/outputs 目录（不含 _thumbs）。"""
    from backend.storage.database import db

    video_ids = db.list_video_ids()
    task_ids = db.list_task_ids()
    removed_work = 0
    removed_outputs = 0

    if WORK_DIR.exists():
        for path in WORK_DIR.iterdir():
            if not path.is_dir():
                continue
            if path.name in video_ids:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed_work += 1
            logger.info("removed orphan work dir %s", path)

    if OUTPUT_DIR.exists():
        for path in OUTPUT_DIR.iterdir():
            if not path.is_dir():
                continue
            if path.name.startswith("_"):
                continue
            if path.name in task_ids:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed_outputs += 1
            logger.info("removed orphan outputs dir %s", path)

    return {
        "removed_work": removed_work,
        "removed_outputs": removed_outputs,
    }


def sweep_orphan_thumbs() -> dict:
    """清理 outputs/_thumbs 中 DB 已不存在的缩略图。"""
    from backend.storage.database import db

    thumbs = OUTPUT_DIR / "_thumbs"
    if not thumbs.is_dir():
        return {"removed_thumbs": 0}

    segment_ids = db.list_segment_ids()
    removed = 0
    for path in thumbs.glob("*.jpg"):
        if path.stem in segment_ids:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info("removed orphan thumbs count=%s", removed)
    return {"removed_thumbs": removed}


def reclaim_expired_local_derivatives(
    *, max_age_hours: float | None = None
) -> dict:
    """按文件年龄清理 uploads 中的本地波形/缩略图派生缓存。

    这些文件不是任务 outputs，不能依赖任务输出清理回收；仍在运行的
    视频任务会被跳过，避免清理正在使用的缓存。
    """
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    hours = LOCAL_RETENTION_HOURS if max_age_hours is None else float(max_age_hours)
    if hours <= 0 or not UPLOAD_DIR.is_dir():
        return {"removed_derivatives": 0, "derivative_hours": hours}

    active_statuses = {
        TaskStatus.PENDING.value,
        TaskStatus.DETECTING.value,
        TaskStatus.PROCESSING.value,
    }
    active_videos = {
        task.get("video_id")
        for status in active_statuses
        for task in db.list_tasks_by_statuses([status])
        if task.get("video_id")
    }
    now = datetime.now(timezone.utc).timestamp()
    removed = 0
    patterns = ("*_waveform_*.json", "*_filmstrip_*.jpg")
    for pattern in patterns:
        for path in UPLOAD_DIR.glob(pattern):
            video_id = path.name.split("_waveform_", 1)[0].split("_filmstrip_", 1)[0]
            if video_id in active_videos:
                continue
            try:
                age_h = (now - path.stat().st_mtime) / 3600.0
            except OSError:
                continue
            if age_h < hours:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        logger.info("removed expired local derivatives count=%s age_h=%.1f", removed, hours)
    return {"removed_derivatives": removed, "derivative_hours": hours}


def purge_expired_memory_records(
    *, max_age_hours: float | None = None
) -> dict:
    """
    删除超期终态任务的内存记录（done/failed/preview），并尝试释放无任务的视频行。
    本地文件应已由 reclaim_idle / orphan sweep 处理。
    """
    from backend.models.schemas import TaskStatus
    from backend.storage.database import db

    hours = (
        LOCAL_RETENTION_HOURS if max_age_hours is None else float(max_age_hours)
    )
    if hours <= 0:
        return {"purged_tasks": 0, "purged_videos": 0, "hours": hours}

    now = datetime.now(timezone.utc)
    terminal = {
        TaskStatus.DONE.value,
        TaskStatus.FAILED.value,
        TaskStatus.PREVIEW.value,
    }
    purged_tasks = 0
    for task in db.list_tasks_by_statuses(list(terminal)):
        age_h = _task_artifact_age_hours(task, now)
        if age_h is None or age_h < hours:
            continue
        task_id = task["id"]
        cleanup_task_outputs_only(task_id)
        cleanup_task_staging(task_id)
        db.delete_task(task_id)
        purged_tasks += 1
        logger.info("purged memory task id=%s age_h=%.1f", task_id, age_h)

    purged_videos = 0
    for video_id in list(db.list_video_ids()):
        tasks = db.get_tasks_by_video(video_id)
        if tasks:
            continue
        video = db.get_video(video_id)
        if not video:
            continue
        created = _parse_task_created_at(video.get("created_at"))
        if not created:
            continue
        age_h = (now - created.astimezone(timezone.utc)).total_seconds() / 3600.0
        if age_h < hours:
            continue
        status = video.get("status")
        if status not in ("ready", "failed"):
            continue
        cleanup_video_local(video_id)
        if video.get("filmstrip_oss_key") and oss_client.is_enabled():
            oss_client.delete_object(video["filmstrip_oss_key"])
        if video.get("managed_source") and video.get("oss_key") and oss_client.is_enabled():
            oss_client.delete_object(video["oss_key"])
        db.delete_video(video_id)
        purged_videos += 1
        logger.info("purged memory video id=%s age_h=%.1f", video_id, age_h)

    return {
        "purged_tasks": purged_tasks,
        "purged_videos": purged_videos,
        "hours": hours,
    }


def _acquire_cleanup_lock(ttl_sec: int = 900):
    """进程内锁（单 API 实例）；返回 True 表示持有锁。"""
    del ttl_sec  # 兼容旧签名
    return _CLEANUP_LOCK.acquire(blocking=False)


def _release_cleanup_lock(held) -> None:
    if held:
        try:
            _CLEANUP_LOCK.release()
        except RuntimeError:
            pass


def run_periodic_cleanup(*, skip_if_locked: bool = True) -> dict:
    """周期 / 启动共用：卡住回收 + 保留期 + 孤儿 + 磁盘水位。"""
    held = _acquire_cleanup_lock()
    if not held and skip_if_locked:
        logger.info("periodic cleanup skipped (lock held)")
        return {"skipped": True, "reason": "locked"}

    try:
        ensure_work_dirs()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        result: dict = {"skipped": False}
        result.update(reclaim_stale_tasks())
        result.update(reclaim_stale_imports())
        result["idle"] = reclaim_idle_local_artifacts()
        result["derivatives"] = reclaim_expired_local_derivatives()
        result.update(sweep_orphan_local_files())
        result.update(sweep_orphan_thumbs())
        result["disk"] = reclaim_by_disk_pressure()
        result["purged"] = purge_expired_memory_records()
        result["expired_staging"] = reclaim_expired_staging()
        try:
            result["managed_oss"] = cleanup_expired_managed_oss()
        except Exception:
            logger.exception("managed OSS cleanup failed")
            result["managed_oss"] = {"error": "cleanup failed"}
        logger.info("periodic cleanup done %s", result)
        return result
    finally:
        _release_cleanup_lock(held)


def run_startup_cleanup() -> dict:
    """API / Worker 启动时跑一轮周期清理。"""
    return run_periodic_cleanup(skip_if_locked=True)


__all__ = [
    "ensure_work_dirs",
    "work_video_dir",
    "get_work_video_path",
    "resolve_local_video_path",
    "import_oss_source",
    "ensure_source_local",
    "cleanup_task_outputs_only",
    "cleanup_task_staging",
    "cleanup_video_local",
    "cleanup_after_oss_task",
    "cleanup_local_after_publish",
    "cleanup_on_task_failure",
    "should_upload_outputs_to_oss",
    "upload_task_segments_to_oss",
    "reclaim_stale_tasks",
    "reclaim_stale_imports",
    "reclaim_idle_local_artifacts",
    "reclaim_expired_local_derivatives",
    "reclaim_by_disk_pressure",
    "sweep_orphan_local_files",
    "sweep_orphan_thumbs",
    "purge_expired_memory_records",
    "run_periodic_cleanup",
    "run_startup_cleanup",
    "get_task_output_dir",
]
