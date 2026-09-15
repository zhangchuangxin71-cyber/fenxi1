"""HTTP(S) URL and OSS object key fetch with SSRF checks and retries."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

from videoaudiotext.api.errors import bad_request, upstream_failed
from videoaudiotext.api.media_utils import ensure_dir, get_extension_from_content_type
from videoaudiotext.storage.oss import download_file_oss, oss_configured


def _max_retries() -> int:
    return max(1, int(os.environ.get("MEDIA_UPLOAD_MAX_RETRIES", "3")))


def _fetch_timeout() -> Optional[float]:
    raw = os.environ.get("MEDIA_UPLOAD_FETCH_TIMEOUT_SEC", "0").strip()
    if not raw or raw == "0":
        return None
    return float(raw)


def _is_blocked_host(host: str) -> bool:
    if os.environ.get("API_FETCH_ALLOW_PRIVATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if not host:
        return True
    host = host.strip().lower()
    if host in {"localhost", "metadata.google.internal"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
        )
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            try:
                parsed = ipaddress.ip_address(ip)
                if (
                    parsed.is_private
                    or parsed.is_loopback
                    or parsed.is_link_local
                    or parsed.is_reserved
                ):
                    return True
            except ValueError:
                continue
    except OSError:
        return False
    return False


def normalize_http_url(url: str) -> str:
    """Percent-encode non-ASCII characters in HTTP(S) URL paths and queries."""
    stripped = url.strip()
    parts = urlsplit(stripped)
    if parts.scheme not in ("http", "https"):
        return stripped
    path = quote(unquote(parts.path), safe="/%")
    query = quote(unquote(parts.query), safe="=&?/%+:") if parts.query else parts.query
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def validate_url(url: str) -> str:
    normalized = normalize_http_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https"):
        raise bad_request(40001, "url must be http or https")
    if _is_blocked_host(parsed.hostname or ""):
        raise bad_request(40001, "url host not allowed")
    return normalized


def _resolve_local_file(source: str) -> Path | None:
    """Return an existing local file path, or None.

    Bare keys like ``audio/1.wav`` are treated as OSS object keys, not local paths.
    """
    ref = source.strip()
    if not ref or ref.startswith(("http://", "https://", "oss://")):
        return None

    path = Path(ref)
    if not path.is_absolute() and not ref.startswith(("./", "../")):
        return None
    if path.is_file():
        return path.resolve()
    return None


def classify_media_source(
    source: str,
) -> tuple[Literal["http", "oss", "local"], str]:
    """Classify input as HTTP(S) URL, OSS object key, or local file path."""
    ref = source.strip()
    if not ref:
        raise bad_request(40001, "media source is required")

    local = _resolve_local_file(ref)
    if local is not None:
        return "local", str(local)

    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https"):
        return "http", validate_url(ref)

    if parsed.scheme == "oss":
        bucket = parsed.netloc.strip()
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise bad_request(40001, "oss:// URI must be oss://bucket/object-key")
        configured = os.environ.get("ALIYUN_OSS_BUCKET", "").strip()
        if configured and bucket != configured:
            raise bad_request(
                40001,
                f"oss bucket mismatch: expected {configured}, got {bucket}",
            )
        return "oss", key

    if not oss_configured():
        raise bad_request(
            40001,
            "bare OSS object key requires ALIYUN_OSS_* configuration; use https URL",
        )
    return "oss", ref.lstrip("/")


def _copy_local_to_dest(local_path: Path, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if local_path.resolve() != dest.resolve():
        shutil.copy2(local_path, dest)
    return dest.stat().st_size


def _http_head_content_type(url: str) -> str:
    timeout = _fetch_timeout()
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return str(resp.headers.get("Content-Type", ""))


def _guess_http_filename(url: str, filename: str | None) -> str:
    if filename:
        return filename
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if name and "." in name:
        return name
    try:
        content_type = _http_head_content_type(url)
    except (urllib.error.URLError, OSError, TimeoutError):
        content_type = ""
    ext = get_extension_from_content_type(content_type) or ".bin"
    return f"file_{uuid.uuid4().hex[:8]}{ext}"


def _fetch_http_to_path(url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeout = _fetch_timeout()
    last_err: Exception | None = None
    for attempt in range(_max_retries()):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                tmp.replace(dest)
                return dest.stat().st_size
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_err = exc
            if attempt + 1 < _max_retries():
                time.sleep(2**attempt)
    raise upstream_failed(
        "media_fetch_failed",
        url=url,
        error=str(last_err),
    )


def _fetch_oss_to_path(object_key: str, dest: Path) -> int:
    last_err: Exception | None = None
    for attempt in range(_max_retries()):
        try:
            return download_file_oss(object_key, dest)
        except Exception as exc:
            last_err = exc
            if attempt + 1 < _max_retries():
                time.sleep(2**attempt)
    raise upstream_failed(
        "media_fetch_failed",
        object_key=object_key,
        error=str(last_err),
    )


def fetch_media_to_path(source: str, dest: Path) -> int:
    """Download media from local path, HTTPS URL, or OSS object key to dest."""
    kind, value = classify_media_source(source)
    if kind == "local":
        return _copy_local_to_dest(Path(value), dest)
    if kind == "http":
        return _fetch_http_to_path(value, dest)
    return _fetch_oss_to_path(value, dest)


def fetch_url_to_path(url: str, dest: Path) -> int:
    """Download media from local path, HTTPS URL, or OSS object key (alias)."""
    return fetch_media_to_path(url, dest)


def download_file(
    url: str,
    output_dir: str | Path | None = None,
    filename: str | None = None,
) -> str:
    """
    Download a file from local path, OSS object key, or HTTP(S) URL.

    Returns the local file path. Raises on failure.
    """
    local = _resolve_local_file(url)
    if local is not None:
        return str(local)

    if output_dir is None:
        output_dir = Path(__file__).resolve().parent / "temp"
    output_dir = ensure_dir(output_dir)

    kind, value = classify_media_source(url)
    if kind == "http":
        name = _guess_http_filename(value, filename)
        output_path = Path(output_dir) / name
        _fetch_http_to_path(value, output_path)
        return str(output_path)

    if kind == "oss":
        if filename is None:
            filename = os.path.basename(value)
            if not filename or "." not in filename:
                filename = f"file_{uuid.uuid4().hex[:8]}.bin"
        output_path = Path(output_dir) / filename
        _fetch_oss_to_path(value, output_path)
        return str(output_path)

    raise upstream_failed("media_fetch_failed", source=url, error="unsupported source")
