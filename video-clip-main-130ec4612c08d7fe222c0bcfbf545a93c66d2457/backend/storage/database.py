"""进程内内存账本：替代 Postgres。进程重启后全部丢失。"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import MAX_INMEM_TASKS, MAX_INMEM_VIDEOS
from backend.models.schemas import TaskMode, TaskStatus


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CapacityError(RuntimeError):
    """内存条目达到上限。"""


class MemoryStore:
    """线程安全的 videos / tasks / segments 内存存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._videos: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._segments: dict[str, dict[str, Any]] = {}  # segment_id -> row
        self._segments_by_task: dict[str, list[str]] = {}  # task_id -> [segment_ids]

    def ping(self) -> None:
        return None

    def _snap(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        return copy.deepcopy(row) if row is not None else None

    def create_video(
        self,
        video_id: str,
        filename: str,
        path: str,
        info: dict,
        *,
        oss_key: str | None = None,
        source: str = "local",
        status: str = "ready",
        progress: float = 100.0,
        message: str = "",
    ) -> dict:
        with self._lock:
            self._ensure_video_capacity()
            self._videos[video_id] = {
                "id": video_id,
                "filename": filename,
                "path": path,
                "duration": info["duration"],
                "width": info["width"],
                "height": info["height"],
                "fps": info["fps"],
                "codec": info["codec"],
                "size_bytes": info["size_bytes"],
                "has_audio": bool(info.get("has_audio", False)),
                "created_at": _utcnow(),
                "oss_key": oss_key,
                "source": source or "local",
                "status": status,
                "progress": progress,
                "message": message or "",
            }
            return self._snap(self._videos[video_id])  # type: ignore[return-value]

    def create_video_importing(
        self,
        video_id: str,
        *,
        filename: str,
        oss_key: str | None = None,
        source_url: str | None = None,
        source: str = "oss",
        message: str = "已入队，等待导入...",
    ) -> dict:
        with self._lock:
            self._ensure_video_capacity()
            self._videos[video_id] = {
                "id": video_id,
                "filename": filename,
                "path": "",
                "duration": None,
                "width": None,
                "height": None,
                "fps": None,
                "codec": None,
                "size_bytes": None,
                "has_audio": False,
                "created_at": _utcnow(),
                "oss_key": oss_key,
                "source_url": source_url,
                "managed_source": False,
                "source": source or "oss",
                "status": "importing",
                "progress": 0.0,
                "message": message or "",
            }
            return self._snap(self._videos[video_id])  # type: ignore[return-value]

    def _ensure_video_capacity(self) -> None:
        if MAX_INMEM_VIDEOS > 0 and len(self._videos) >= MAX_INMEM_VIDEOS:
            raise CapacityError(
                f"内存视频数已达上限（{MAX_INMEM_VIDEOS}），请删除旧视频或重启服务"
            )

    def _ensure_task_capacity(self) -> None:
        if MAX_INMEM_TASKS > 0 and len(self._tasks) >= MAX_INMEM_TASKS:
            raise CapacityError(
                f"内存任务数已达上限（{MAX_INMEM_TASKS}），请删除旧任务或重启服务"
            )

    def update_video_path(self, video_id: str, path: str) -> None:
        with self._lock:
            row = self._videos.get(video_id)
            if row:
                row["path"] = path

    def update_video_import(
        self,
        video_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        path: str | None = None,
        info: dict | None = None,
        filename: str | None = None,
        oss_key: str | None = None,
        managed_source: bool | None = None,
    ) -> None:
        with self._lock:
            row = self._videos.get(video_id)
            if not row:
                return
            if status is not None:
                row["status"] = status
            if progress is not None:
                row["progress"] = progress
            if message is not None:
                row["message"] = message
            if path is not None:
                row["path"] = path
            if filename is not None:
                row["filename"] = filename
            if oss_key is not None:
                row["oss_key"] = oss_key
            if managed_source is not None:
                row["managed_source"] = managed_source
            if info is not None:
                row["duration"] = info.get("duration")
                row["width"] = info.get("width")
                row["height"] = info.get("height")
                row["fps"] = info.get("fps")
                row["codec"] = info.get("codec")
                row["size_bytes"] = info.get("size_bytes")
                if "has_audio" in info:
                    row["has_audio"] = bool(info.get("has_audio"))

    def get_video(self, video_id: str) -> Optional[dict]:
        with self._lock:
            return self._snap(self._videos.get(video_id))

    def list_videos_by_status(self, status: str) -> list[dict]:
        with self._lock:
            rows = [v for v in self._videos.values() if v.get("status") == status]
            rows.sort(key=lambda r: r.get("created_at") or "")
            return [self._snap(r) for r in rows]  # type: ignore[misc]

    def update_video_fps(self, video_id: str, fps: float) -> None:
        with self._lock:
            row = self._videos.get(video_id)
            if row:
                row["fps"] = round(fps, 3)

    def update_video_artifacts(
        self,
        video_id: str,
        *,
        filmstrip_oss_key: str | None = None,
    ) -> None:
        with self._lock:
            row = self._videos.get(video_id)
            if row and filmstrip_oss_key is not None:
                row["filmstrip_oss_key"] = filmstrip_oss_key

    def create_task(self, task_id: str, video_id: str, mode: TaskMode) -> dict:
        with self._lock:
            self._ensure_task_capacity()
            self._tasks[task_id] = {
                "id": task_id,
                "video_id": video_id,
                "mode": mode.value,
                "status": TaskStatus.PENDING.value,
                "progress": 0,
                "message": "",
                "created_at": _utcnow(),
            }
            self._segments_by_task.setdefault(task_id, [])
            return self._snap(self._tasks[task_id])  # type: ignore[return-value]

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
    ) -> None:
        with self._lock:
            row = self._tasks.get(task_id)
            if not row:
                return
            if status is not None:
                row["status"] = status.value
            if progress is not None:
                row["progress"] = progress
            if message is not None:
                row["message"] = message

    def claim_task_status(
        self,
        task_id: str,
        *,
        from_statuses: list[str],
        to_status: TaskStatus,
        progress: Optional[float] = None,
        message: Optional[str] = None,
    ) -> bool:
        with self._lock:
            row = self._tasks.get(task_id)
            if not row:
                return False
            if row.get("status") not in from_statuses:
                return False
            row["status"] = to_status.value
            if progress is not None:
                row["progress"] = progress
            if message is not None:
                row["message"] = message
            return True

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._snap(self._tasks.get(task_id))

    def create_segments(self, task_id: str, segments: list[dict]) -> list[dict]:
        with self._lock:
            ids: list[str] = []
            for seg in segments:
                sid = seg["id"]
                self._segments[sid] = {
                    "id": sid,
                    "task_id": task_id,
                    "index_num": seg["index"],
                    "start_time": seg["start_time"],
                    "end_time": seg["end_time"],
                    "start_frame": seg.get("start_frame"),
                    "end_frame": seg.get("end_frame"),
                    "output_path": seg.get("output_path"),
                    "source": seg["source"],
                    "confidence": seg.get("confidence"),
                    "summary": seg.get("summary"),
                    "status": seg.get("status", "pending"),
                    "audio_path": seg.get("audio_path"),
                    "oss_key": seg.get("oss_key"),
                    "staging_oss_key": seg.get("staging_oss_key"),
                    "thumb_oss_key": seg.get("thumb_oss_key"),
                }
                ids.append(sid)
            self._segments_by_task[task_id] = ids
            return self.get_segments(task_id)

    def get_segments(self, task_id: str) -> list[dict]:
        with self._lock:
            ids = self._segments_by_task.get(task_id, [])
            rows = [self._segments[i] for i in ids if i in self._segments]
            rows.sort(key=lambda r: int(r.get("index_num") or 0))
            return [self._snap(r) for r in rows]  # type: ignore[misc]

    def update_segment(self, segment_id: str, **kwargs) -> None:
        allowed = {
            "output_path",
            "audio_path",
            "oss_key",
            "status",
            "start_time",
            "end_time",
            "start_frame",
            "end_frame",
            "staging_oss_key",
            "thumb_oss_key",
        }
        with self._lock:
            row = self._segments.get(segment_id)
            if not row:
                return
            for key, value in kwargs.items():
                if key in allowed:
                    row[key] = value

    def delete_segments(self, task_id: str) -> None:
        with self._lock:
            for sid in list(self._segments_by_task.get(task_id, [])):
                self._segments.pop(sid, None)
            self._segments_by_task[task_id] = []

    def clear_segment_oss_keys(self, task_id: str) -> None:
        with self._lock:
            for sid in self._segments_by_task.get(task_id, []):
                row = self._segments.get(sid)
                if row:
                    row["oss_key"] = None

    def clear_segment_staging_keys(self, task_id: str) -> None:
        with self._lock:
            for sid in self._segments_by_task.get(task_id, []):
                row = self._segments.get(sid)
                if row:
                    row["staging_oss_key"] = None
                    row["thumb_oss_key"] = None

    def get_segment(self, segment_id: str) -> Optional[dict]:
        with self._lock:
            return self._snap(self._segments.get(segment_id))

    def get_tasks_by_video(self, video_id: str) -> list[dict]:
        with self._lock:
            rows = [t for t in self._tasks.values() if t.get("video_id") == video_id]
            rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
            return [self._snap(r) for r in rows]  # type: ignore[misc]

    def list_video_ids(self) -> set[str]:
        with self._lock:
            return set(self._videos.keys())

    def list_task_ids(self) -> set[str]:
        with self._lock:
            return set(self._tasks.keys())

    def list_segment_ids(self) -> set[str]:
        with self._lock:
            return set(self._segments.keys())

    def list_tasks_by_statuses(self, statuses: list[str]) -> list[dict]:
        if not statuses:
            return []
        status_set = set(statuses)
        with self._lock:
            rows = [t for t in self._tasks.values() if t.get("status") in status_set]
            rows.sort(key=lambda r: r.get("created_at") or "")
            return [self._snap(r) for r in rows]  # type: ignore[misc]

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            for sid in list(self._segments_by_task.get(task_id, [])):
                self._segments.pop(sid, None)
            self._segments_by_task.pop(task_id, None)
            self._tasks.pop(task_id, None)

    def delete_video(self, video_id: str) -> bool:
        with self._lock:
            if video_id not in self._videos:
                return False
            task_ids = [
                tid
                for tid, t in self._tasks.items()
                if t.get("video_id") == video_id
            ]
            for tid in task_ids:
                for sid in list(self._segments_by_task.get(tid, [])):
                    self._segments.pop(sid, None)
                self._segments_by_task.pop(tid, None)
                self._tasks.pop(tid, None)
            self._videos.pop(video_id, None)
            return True

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "videos": len(self._videos),
                "tasks": len(self._tasks),
                "segments": len(self._segments),
            }


# 兼容旧 import：from backend.storage.database import db
Database = MemoryStore
db = MemoryStore()
