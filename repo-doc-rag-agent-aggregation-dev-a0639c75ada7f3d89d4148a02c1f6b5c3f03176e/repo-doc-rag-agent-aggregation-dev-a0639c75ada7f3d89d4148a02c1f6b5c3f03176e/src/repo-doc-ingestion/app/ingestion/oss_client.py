from __future__ import annotations

"""Small OSS helper used by the ingestion service.

The root-level ``utils.py`` is kept for the image annotation demo service.  This
module is intentionally separate so document ingestion can use environment
driven OSS settings without importing global, hard-coded bucket state.
"""

import os
from pathlib import Path
from typing import Any


def is_oss_key(value: str | None) -> bool:
    """Return whether a request value should be treated as an OSS object key."""
    text = str(value or "").strip()
    return bool(text) and not text.startswith(("http://", "https://", "file://"))


def download_oss_key(oss_key: str, target: Path) -> Path:
    """Download one OSS object to ``target`` and return the local path."""
    key = str(oss_key or "").strip().lstrip("/")
    if not key:
        raise ValueError("oss_key is required")

    bucket = get_oss_bucket()

    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    bucket.get_object_to_file(key, str(target))
    return target


def get_oss_bucket() -> Any:
    """Build an OSS bucket from environment variables."""
    bucket_name = str(os.getenv("OSS_BUCKET") or os.getenv("OSS_BUCKET_NAME") or "").strip()
    endpoint = str(os.getenv("OSS_ENDPOINT") or "").strip()
    region = str(os.getenv("OSS_REGION") or "").strip() or None
    if not bucket_name or not endpoint:
        raise RuntimeError("OSS_BUCKET/OSS_BUCKET_NAME and OSS_ENDPOINT are required for oss_key input")

    try:
        import oss2
        from oss2.credentials import EnvironmentVariableCredentialsProvider
    except ImportError as exc:  # pragma: no cover - depends on optional runtime dep
        raise RuntimeError("oss2 is required for oss_key input. Install dependency: pip install oss2") from exc

    auth = oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider())
    session = oss2.Session()
    # OSS calls should not inherit VS Code/Codex proxy variables such as
    # http_proxy=http://127.0.0.1:7890. Those tunnels may be absent when the
    # ingestion service starts or shuts down, causing downloads/log archival to
    # fail even when OSS is directly reachable from the server.
    session.session.trust_env = False
    kwargs = {"region": region} if region else {}
    return oss2.Bucket(auth, endpoint, bucket_name, session=session, proxies={}, **kwargs)


def upload_file_to_oss(local_path: Path, oss_key: str) -> str:
    """Upload one local file to OSS and return the object key."""
    key = str(oss_key or "").strip().lstrip("/")
    if not key:
        raise ValueError("oss_key is required")
    path = Path(local_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"local file not found: {path}")

    bucket = get_oss_bucket()
    bucket.put_object_from_file(key, str(path))
    return key
