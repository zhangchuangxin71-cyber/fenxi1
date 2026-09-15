"""Cross-process coordination via filesystem locks (single-machine multi-worker)."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from videoaudiotext.api.job_context import is_managed_job_directory, jobs_root
from videoaudiotext.concurrency.limits import global_job_workers

_COORD_DIRNAME = "_coord"
_GLOBAL_LOCK = "global_jobs.lock"
_GLOBAL_STATE = "global_jobs.json"
_WAIT_POLL_SEC = 0.15
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def coord_dir() -> Path:
    path = jobs_root() / _COORD_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_lock_name(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip())
    return cleaned or "default"


@contextlib.contextmanager
def file_lock(lock_path: Path, *, exclusive: bool = True) -> Iterator[None]:
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def workspace_stage_lock_path(workspace_id: str, stage: str) -> Path:
    return jobs_root() / workspace_id / "_locks" / f"{_safe_lock_name(stage)}.lock"


@contextlib.contextmanager
def workspace_stage_claim_lock(workspace_id: str, stage: str) -> Iterator[None]:
    """Exclusive lock for atomically claiming a workspace stage job slot."""
    with file_lock(workspace_stage_lock_path(workspace_id, stage)):
        yield


def _global_lock_path() -> Path:
    return coord_dir() / _GLOBAL_LOCK


def _global_state_path() -> Path:
    return coord_dir() / _GLOBAL_STATE


def _default_global_state() -> dict[str, Any]:
    return {"running": 0, "waiting": []}


def _read_global_state() -> dict[str, Any]:
    path = _global_state_path()
    if not path.is_file():
        return _default_global_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_global_state()
    if not isinstance(data, dict):
        return _default_global_state()
    waiting = data.get("waiting")
    if not isinstance(waiting, list):
        waiting = []
    running = data.get("running")
    try:
        running_int = max(0, int(running))
    except (TypeError, ValueError):
        running_int = 0
    return {"running": running_int, "waiting": [str(x) for x in waiting if x]}


def _write_global_state(state: dict[str, Any]) -> None:
    path = _global_state_path()
    payload = {
        "running": max(0, int(state.get("running") or 0)),
        "waiting": [str(x) for x in (state.get("waiting") or []) if x],
    }
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _remove_waiting(state: dict[str, Any], job_id: str) -> None:
    state["waiting"] = [jid for jid in state["waiting"] if jid != job_id]


def _ensure_waiting(state: dict[str, Any], job_id: str) -> None:
    if job_id not in state["waiting"]:
        state["waiting"].append(job_id)


@contextlib.contextmanager
def global_job_slot(
    job_id: str,
    *,
    should_run: Callable[[], bool] | None = None,
) -> Iterator[bool]:
    """Block until a cluster-wide job slot is available, then hold until done."""
    limit = global_job_workers()
    acquired = False
    try:
        while True:
            if should_run and not should_run():
                with file_lock(_global_lock_path()):
                    state = _read_global_state()
                    _remove_waiting(state, job_id)
                    _write_global_state(state)
                yield False
                return

            with file_lock(_global_lock_path()):
                state = _read_global_state()
                if state["running"] < limit:
                    state["running"] += 1
                    _remove_waiting(state, job_id)
                    _write_global_state(state)
                    acquired = True
                    break
                _ensure_waiting(state, job_id)
                _write_global_state(state)

            time.sleep(_WAIT_POLL_SEC)

        yield True
    finally:
        if acquired:
            with file_lock(_global_lock_path()):
                state = _read_global_state()
                state["running"] = max(0, state["running"] - 1)
                _write_global_state(state)


def queue_info_for_job(job_id: str) -> dict[str, Any]:
    """Return queue_position / queue_ahead for a queued job (cross-process)."""
    with file_lock(_global_lock_path(), exclusive=False):
        state = _read_global_state()
    waiting: list[str] = state["waiting"]
    if job_id not in waiting:
        if state["running"] >= global_job_workers():
            ahead = len(waiting)
            if ahead <= 0:
                return {}
            return {
                "queue_position": ahead + 1,
                "queue_ahead": ahead,
                "queue_message": f"前方还有 {ahead} 个任务等待执行槽位",
            }
        return {}

    idx = waiting.index(job_id)
    ahead = idx
    return {
        "queue_position": ahead + 1,
        "queue_ahead": ahead,
        "queue_message": (
            f"前方还有 {ahead} 个任务等待执行槽位" if ahead else "即将获得执行槽位"
        ),
    }


def enrich_job_queue_fields(job: dict[str, Any]) -> dict[str, Any]:
    if str(job.get("status") or "") != "queued":
        return job
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return job
    extra = queue_info_for_job(job_id)
    if not extra:
        return job
    merged = dict(job)
    merged.update(extra)
    return merged


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def reset_global_coordination(*, startup: bool = False) -> dict[str, Any]:
    """Sync or reset cross-process slot counters against on-disk job state."""
    if not startup:
        return get_coord_status()
    scan = _scan_active_jobs_on_disk()
    with file_lock(_global_lock_path()):
        # After reconcile_all_workspaces, running on disk should be 0; keep file in sync.
        _write_global_state(
            {
                "running": min(scan["running_count"], global_job_workers()),
                "waiting": [j["job_id"] for j in scan["queued_jobs"]],
            }
        )
    return get_coord_status()


def _scan_active_jobs_on_disk() -> dict[str, Any]:
    root = jobs_root()
    running_jobs: list[dict[str, Any]] = []
    queued_jobs: list[dict[str, Any]] = []
    if not root.is_dir():
        return {"running_count": 0, "queued_count": 0, "running_jobs": [], "queued_jobs": []}
    for ws_dir in sorted(root.iterdir()):
        if not ws_dir.is_dir() or not is_managed_job_directory(ws_dir.name):
            continue
        jobs_dir = ws_dir / "jobs"
        if not jobs_dir.is_dir():
            continue
        for job_path in jobs_dir.glob("*.json"):
            try:
                data = json.loads(job_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "")
            entry = {
                "workspace_id": ws_dir.name,
                "job_id": data.get("job_id"),
                "kind": data.get("kind") or job_path.stem.split("_", 1)[0],
                "status": status,
                "message": ((data.get("progress") or {}).get("message") or "")[:80],
            }
            if status == "running":
                running_jobs.append(entry)
            elif status == "queued":
                queued_jobs.append(entry)
    return {
        "running_count": len(running_jobs),
        "queued_count": len(queued_jobs),
        "running_jobs": running_jobs,
        "queued_jobs": queued_jobs,
    }


def get_coord_status() -> dict[str, Any]:
    with file_lock(_global_lock_path(), exclusive=False):
        state = _read_global_state()
    scan = _scan_active_jobs_on_disk()
    limit = global_job_workers()
    return {
        "global_job_workers": limit,
        "coord_state": state,
        "jobs_on_disk": {
            "running_count": scan["running_count"],
            "queued_count": scan["queued_count"],
            "running_jobs": scan["running_jobs"],
            "queued_jobs": scan["queued_jobs"],
        },
        "desync": int(state.get("running") or 0) != scan["running_count"],
        "waiting_count": len(state.get("waiting") or []),
    }


def _reset_global_state_for_tests() -> None:
    path = _global_state_path()
    if path.is_file():
        path.unlink(missing_ok=True)
