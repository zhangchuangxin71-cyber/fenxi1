"""Aliyun OSS upload helpers for Flow B deliverables."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

OSS_PREFIX_DEFAULT = "prod/ImagesVideosText2Video"
ASS_CONTENT_TYPE = "text/plain; charset=utf-8"


def oss_configured() -> bool:
    if os.environ.get("OSS_UPLOAD_ENABLED", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    auth_mode = os.environ.get("ALIYUN_OSS_AUTH_MODE", "access_key").strip().lower()
    if auth_mode in {"v4", "provider_v4", "env_v4"} and os.environ.get(
        "OSS_ACCESS_KEY_ID", ""
    ).strip():
        required = (
            "ALIYUN_OSS_ENDPOINT",
            "OSS_ACCESS_KEY_ID",
            "OSS_ACCESS_KEY_SECRET",
            "ALIYUN_OSS_BUCKET",
        )
    else:
        required = (
            "ALIYUN_OSS_ENDPOINT",
            "ALIYUN_OSS_ACCESS_KEY_ID",
            "ALIYUN_OSS_ACCESS_KEY_SECRET",
            "ALIYUN_OSS_BUCKET",
        )
    return all(os.environ.get(key, "").strip() for key in required)


def _oss_prefix() -> str:
    raw = os.environ.get("ALIYUN_OSS_PREFIX", OSS_PREFIX_DEFAULT).strip()
    return raw.strip("/") or OSS_PREFIX_DEFAULT


def build_deliverable_base(
    *,
    code: str,
    user_id: str,
    job_id: str,
    day: date | None = None,
) -> str:
    day_dir = (day or date.today()).strftime("%Y-%m-%d")
    return f"{_oss_prefix()}/{code}-{user_id}/{day_dir}/{job_id}"


def build_compose_object_keys(
    *,
    code: str,
    user_id: str,
    job_id: str,
    day: date | None = None,
) -> dict[str, str]:
    base = build_deliverable_base(code=code, user_id=user_id, job_id=job_id, day=day)
    return {
        "video": f"{base}.mp4",
        "audio": f"{base}.wav",
        "subtitle_ass": f"{base}_subtitle.ass",
        "log": f"{base}.log",
    }


def build_audio_deliverable_keys(
    *,
    code: str,
    user_id: str,
    job_id: str,
    day: date | None = None,
) -> dict[str, str]:
    base = build_deliverable_base(code=code, user_id=user_id, job_id=job_id, day=day)
    return {
        "master": f"{base}_master.wav",
        "subtitle_srt": f"{base}_subtitle.srt",
        "subtitle_ass": f"{base}_subtitle.ass",
    }


def build_audio_segment_object_key(
    *,
    code: str,
    user_id: str,
    job_id: str,
    segment_index: int,
    day: date | None = None,
) -> str:
    base = build_deliverable_base(code=code, user_id=user_id, job_id=job_id, day=day)
    return f"{base}_seg{segment_index}.wav"


def build_preview_object_key(
    *,
    code: str,
    user_id: str,
    job_id: str,
    segment_index: int = 1,
    day: date | None = None,
) -> str:
    base = build_deliverable_base(code=code, user_id=user_id, job_id=job_id, day=day)
    return f"{base}_preview_seg{segment_index}.jpg"


def oss_public_url(object_key: str) -> str:
    bucket = os.environ["ALIYUN_OSS_BUCKET"].strip()
    endpoint = os.environ["ALIYUN_OSS_ENDPOINT"].strip()
    endpoint = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    key = object_key.lstrip("/")
    return f"https://{bucket}.{endpoint}/{key}"


def _oss_endpoint_url() -> str:
    endpoint = os.environ["ALIYUN_OSS_ENDPOINT"].strip()
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    return endpoint


def _oss_region() -> str | None:
    raw = os.environ.get("ALIYUN_OSS_REGION", "").strip()
    return raw or None


def _oss_auth():
    import oss2

    auth_mode = os.environ.get("ALIYUN_OSS_AUTH_MODE", "access_key").strip().lower()
    if auth_mode in {"v4", "provider_v4", "env_v4"}:
        from oss2.credentials import EnvironmentVariableCredentialsProvider

        return oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())

    return oss2.Auth(
        os.environ["ALIYUN_OSS_ACCESS_KEY_ID"].strip(),
        os.environ["ALIYUN_OSS_ACCESS_KEY_SECRET"].strip(),
    )


def _get_oss_bucket():
    import oss2

    kwargs: dict[str, str] = {}
    region = _oss_region()
    if region:
        kwargs["region"] = region
    return oss2.Bucket(
        _oss_auth(),
        _oss_endpoint_url(),
        os.environ["ALIYUN_OSS_BUCKET"].strip(),
        **kwargs,
    )


def preview_video_oss_snapshot_enabled() -> bool:
    raw = os.environ.get("PREVIEW_VIDEO_OSS_SNAPSHOT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def preview_video_oss_snapshot_fallback_enabled() -> bool:
    raw = os.environ.get("PREVIEW_VIDEO_SNAPSHOT_FALLBACK", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def preview_video_snapshot_mode() -> str:
    mode = os.environ.get("PREVIEW_VIDEO_SNAPSHOT_MODE", "fast").strip().lower()
    return mode if mode in {"fast", "exact"} else "fast"


def preview_video_snapshot_format() -> str:
    fmt = os.environ.get("PREVIEW_VIDEO_SNAPSHOT_FORMAT", "jpg").strip().lower()
    return fmt if fmt in {"jpg", "jpeg", "png"} else "jpg"


def build_video_snapshot_process(
    *,
    start_sec: float = 0.0,
    fmt: str | None = None,
    mode: str | None = None,
) -> str:
    """OSS video/snapshot process string for a single frame at start_sec."""
    t_ms = max(0, int(round(float(start_sec) * 1000)))
    snapshot_fmt = (fmt or preview_video_snapshot_format()).lower()
    if snapshot_fmt == "jpeg":
        snapshot_fmt = "jpg"
    parts = [f"video/snapshot,t_{t_ms},f_{snapshot_fmt}"]
    if (mode or preview_video_snapshot_mode()) == "fast":
        parts.append("m_fast")
    return ",".join(parts)


def resolve_oss_object_key(source: str) -> str | None:
    """Return OSS object key when source targets the configured bucket."""
    ref = (source or "").strip()
    if not ref or not oss_configured():
        return None

    configured_bucket = os.environ["ALIYUN_OSS_BUCKET"].strip()
    endpoint = os.environ["ALIYUN_OSS_ENDPOINT"].strip()
    endpoint = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")

    parsed = urlparse(ref)
    if parsed.scheme == "oss":
        bucket = parsed.netloc.strip()
        key = parsed.path.lstrip("/")
        if bucket == configured_bucket and key:
            return key
        return None

    if parsed.scheme in ("http", "https"):
        host = (parsed.hostname or "").lower()
        expected_host = f"{configured_bucket}.{endpoint}".lower()
        if host != expected_host:
            return None
        key = parsed.path.lstrip("/")
        return key or None

    if ref.startswith(("http://", "https://", "oss://")):
        return None
    if not ref.startswith(("./", "../")):
        return ref.lstrip("/")
    return None


def download_video_snapshot_oss(
    object_key: str,
    dest: Path,
    *,
    start_sec: float = 0.0,
    process: str | None = None,
) -> int:
    if not oss_configured():
        raise RuntimeError("OSS credentials not configured")
    try:
        import oss2  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("oss2 is not installed") from exc

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    key = object_key.lstrip("/")
    style = process or build_video_snapshot_process(start_sec=start_sec)
    bucket = _get_oss_bucket()
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        result = bucket.get_object(key, process=style)
        with open(tmp, "wb") as handle:
            handle.write(result.read())
        tmp.replace(dest)
        logger.info(
            "OSS snapshot %s (%s) -> %s",
            key,
            style,
            dest,
        )
        return dest.stat().st_size
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        logger.exception("OSS video snapshot failed for %s (%s)", key, style)
        raise


def download_file_oss(object_key: str, dest: Path) -> int:
    if not oss_configured():
        raise RuntimeError("OSS credentials not configured")
    try:
        import oss2  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("oss2 is not installed") from exc

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    key = object_key.lstrip("/")
    bucket = _get_oss_bucket()
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        bucket.get_object_to_file(key, str(tmp))
        tmp.replace(dest)
        logger.info("Downloaded oss://%s/%s -> %s", bucket.bucket_name, key, dest)
        return dest.stat().st_size
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        logger.exception("OSS download failed for %s", key)
        raise


def upload_file_oss(local_file_path: str | Path, object_name: str) -> bool:
    path = Path(local_file_path)
    if not path.is_file():
        logger.error("OSS upload skipped, file missing: %s", path)
        return False
    if not oss_configured():
        logger.error("OSS upload skipped, credentials not configured")
        return False
    try:
        import oss2
    except ImportError:
        logger.error("OSS upload failed: oss2 is not installed")
        return False

    bucket = _get_oss_bucket()
    key = object_name.lstrip("/")
    try:
        if path.suffix.lower() == ".ass":
            bucket.put_object_from_file(
                key,
                str(path),
                headers={"Content-Type": ASS_CONTENT_TYPE},
            )
        else:
            bucket.put_object_from_file(key, str(path))
        logger.info("Uploaded %s -> oss://%s/%s", path, bucket.bucket_name, key)
        return True
    except Exception:
        logger.exception("OSS upload failed for %s -> %s", path, key)
        return False
