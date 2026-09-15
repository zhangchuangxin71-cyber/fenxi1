from __future__ import annotations

"""Archive service logs to OSS when the FastAPI process shuts down."""

from datetime import datetime
import asyncio
import logging
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from app.ingestion.oss_client import upload_file_to_oss


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _log_file_path() -> Path:
    configured = str(os.getenv("INGEST_LOG_FILE") or "").strip()
    return Path(configured or "app.log").expanduser().resolve()


def _archive_key(now: datetime) -> str:
    prefix = str(os.getenv("INGEST_LOG_OSS_PREFIX") or "prod/doc-ingestion/logs").strip().strip("/")
    return f"{prefix}/{now.strftime('%Y%m%d_%H%M%S')}.md"


def _build_markdown(log_path: Path, now: datetime) -> str:
    if log_path.exists() and log_path.is_file():
        content = log_path.read_text(encoding="utf-8", errors="replace")
    else:
        content = f"log file not found: {log_path}\n"

    return "\n".join(
        [
            "# repo-doc-ingestion service log",
            "",
            f"- archived_at: {now.isoformat()}",
            f"- source_log: `{log_path}`",
            "",
            "```text",
            content.rstrip(),
            "```",
            "",
        ]
    )


async def archive_service_log_to_oss() -> None:
    """Upload the current service log as Markdown to OSS.

    This runs on normal FastAPI shutdown. It will not run if the process is
    killed with SIGKILL (`kill -9`) or the machine/container is forcibly stopped.
    """
    if not _env_bool("INGEST_LOG_ARCHIVE_ENABLED", True):
        return

    timezone_name = str(os.getenv("INGEST_LOG_TIMEZONE") or "Asia/Shanghai").strip()
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        logger.warning("invalid INGEST_LOG_TIMEZONE=%r; fallback=Asia/Shanghai", timezone_name)
        tz = ZoneInfo("Asia/Shanghai")

    now = datetime.now(tz)
    log_path = _log_file_path()
    oss_key = _archive_key(now)

    try:
        with tempfile.TemporaryDirectory(prefix="ingest_log_archive_") as tmp_dir:
            archive_path = Path(tmp_dir) / Path(oss_key).name
            archive_path.write_text(_build_markdown(log_path, now), encoding="utf-8")
            uploaded_key = await asyncio.to_thread(upload_file_to_oss, archive_path, oss_key)
            logger.info("service_log_archived_to_oss key=%s source=%s", uploaded_key, log_path)
    finally:
        if _env_bool("INGEST_LOG_DELETE_ON_SHUTDOWN", True) and log_path.exists() and log_path.is_file():
            try:
                log_path.unlink()
                logger.info("service_log_deleted_on_shutdown source=%s", log_path)
            except Exception as exc:
                logger.warning("service_log_delete_on_shutdown_failed source=%s error=%s", log_path, exc)
