"""系统接口：健康检查、服务信息。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter

from backend.api.errors import CODE_SERVICE_UNAVAILABLE, raise_api_error
from backend.api.v1.response import HealthResponse, ok
from backend.api.v1.schemas import HealthChecks, HealthData
from backend.config import APP_BUILD_TIME, APP_GIT_SHA, APP_VERSION
from backend.storage import oss_client
from backend.storage.database import db
from backend.workers.inline_scheduler import scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["系统"])


def _bin_ok(configured: str, fallback_name: str) -> bool:
    cand = (configured or "").strip()
    if cand:
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return True
    import shutil

    return shutil.which(fallback_name) is not None


def _check_oss() -> dict:
    if not oss_client.is_enabled():
        return {"ok": False, "error": "OSS 未启用"}
    try:
        oss_client.probe()
        return {"ok": True}
    except Exception as exc:
        logger.warning("health oss failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}


def _check_store() -> dict:
    try:
        db.ping()
        stats = db.stats() if hasattr(db, "stats") else {}
        return {"ok": True, "backend": "memory", **stats}
    except Exception as exc:
        logger.warning("health store failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}


def _check_scheduler() -> dict:
    return scheduler.stats()


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health():
    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
    ffprobe_bin = os.environ.get("FFPROBE_BIN", "/usr/bin/ffprobe")
    ffmpeg = {"ok": _bin_ok(ffmpeg_bin, "ffmpeg")}
    ffprobe = {"ok": _bin_ok(ffprobe_bin, "ffprobe")}
    oss = _check_oss()
    store = _check_store()
    sched = _check_scheduler()

    checks = HealthChecks(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        oss=oss,
        store=store,
        scheduler=sched,
    )
    all_ok = all(
        c.get("ok")
        for c in (ffmpeg, ffprobe, oss, store, sched)
    )
    data = HealthData(
        ok=all_ok,
        checks=checks,
        version=APP_VERSION,
        git_sha=APP_GIT_SHA,
        build_time=APP_BUILD_TIME,
    )
    if not all_ok:
        raise_api_error(
            503,
            CODE_SERVICE_UNAVAILABLE,
            "依赖不可用",
            detail=data.model_dump(),
        )
    return ok(data)
