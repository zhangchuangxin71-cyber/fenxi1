"""阿里云 OSS 封装：导入源片、上传成品、签名下载。"""

from __future__ import annotations

import logging
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterable
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from backend.config import (
    OSS_ACCESS_KEY_ID,
    OSS_ACCESS_KEY_SECRET,
    OSS_BUCKET,
    OSS_DOWNLOAD_RETRIES,
    OSS_DOWNLOAD_RETRY_BASE_SLEEP,
    OSS_ENABLED,
    OSS_ENDPOINT,
    OSS_MULTIPART_PART_SIZE_MB,
    OSS_PREFIX,
    OSS_SIGN_EXPIRES,
    OSS_STAGING_PREFIX,
)

logger = logging.getLogger(__name__)

_bucket = None
_cdn_probe_lock = threading.Lock()
_cdn_object_probe: dict[str, tuple[float, bool]] = {}


class OssError(Exception):
    pass


def is_enabled() -> bool:
    return bool(OSS_ENABLED)


def _require_enabled() -> None:
    if not is_enabled():
        raise OssError(
            "OSS 未启用：请配置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / "
            "OSS_ENDPOINT / OSS_BUCKET，或设置 OSS_ENABLED=true"
        )


def _get_bucket():
    global _bucket
    _require_enabled()
    if _bucket is not None:
        return _bucket
    try:
        import oss2
    except ImportError as exc:
        raise OssError("未安装 oss2，请执行 pip install oss2") from exc

    auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
    endpoint = OSS_ENDPOINT
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        endpoint = f"https://{endpoint}"
    _bucket = oss2.Bucket(auth, endpoint, OSS_BUCKET)
    return _bucket


def parse_oss_ref(*, oss_key: str | None = None, oss_url: str | None = None) -> str:
    """将 oss_key 或完整 URL 解析为 bucket 内 object key。"""
    key = (oss_key or "").strip()
    url = (oss_url or "").strip()
    if key and url:
        raise OssError("请只传 oss_key 或 oss_url 其中之一")
    if not key and not url:
        raise OssError("请提供 oss_key 或 oss_url")

    if key:
        return key.lstrip("/")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OssError("oss_url 格式无效")

    host = parsed.netloc.lower()
    path = unquote(parsed.path or "").lstrip("/")

    # https://bucket.oss-cn-xxx.aliyuncs.com/path/to/obj
    m = re.match(r"^([^.]+)\.oss-[^.]+\.aliyuncs\.com$", host)
    if m:
        bucket_in_host = m.group(1)
        if OSS_BUCKET and bucket_in_host != OSS_BUCKET:
            raise OssError(
                f"URL 中的 bucket ({bucket_in_host}) 与配置 OSS_BUCKET ({OSS_BUCKET}) 不一致"
            )
        if not path:
            raise OssError("oss_url 缺少对象路径")
        return path

    # https://oss-cn-xxx.aliyuncs.com/bucket/path
    if host.startswith("oss-") and ".aliyuncs.com" in host:
        parts = path.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise OssError("oss_url 缺少对象路径")
        if OSS_BUCKET and parts[0] != OSS_BUCKET:
            raise OssError(
                f"URL 中的 bucket ({parts[0]}) 与配置 OSS_BUCKET ({OSS_BUCKET}) 不一致"
            )
        return parts[1]

    # 其它 CDN / 自定义域名：取 path 作为 key
    if not path:
        raise OssError("无法从 oss_url 解析对象 key")
    return path


def normalize_output_prefix(prefix: str | None) -> str | None:
    """规范化成品目录前缀；空则返回 None。禁止 .. 与反斜杠路径穿越。"""
    if prefix is None:
        return None
    text = str(prefix).strip().replace("\\", "/").lstrip("/")
    if not text:
        return None
    if ".." in text.split("/"):
        raise OssError("oss_key 目录前缀无效（禁止 ..）")
    if not text.endswith("/"):
        text += "/"
    return text


def output_object_key(
    task_id: str,
    filename: str,
    *,
    prefix: str | None = None,
) -> str:
    """
    生成成品 object key。
    优先使用前端传入的目录前缀；未传时回退 {OSS_PREFIX}outputs/{task_id}/。
    """
    name = Path(filename).name
    normalized = normalize_output_prefix(prefix)
    if normalized:
        return f"{normalized}{name}"
    base = OSS_PREFIX or "video-clip/"
    if not base.endswith("/"):
        base += "/"
    return f"{base}outputs/{task_id}/{name}"


def _is_retryable_oss_error(exc: BaseException) -> bool:
    """网络超时 / 连接类错误可重试；明确业务错误不重试。"""
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    markers = (
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "reset by peer",
        "broken pipe",
        "503",
        "502",
        "500",
        "slow down",
        "requesttimeout",
    )
    if any(m in text for m in markers):
        return True
    if name in {
        "RequestError",
        "RequestErrorEx",
        "ServerError",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "ChunkedEncodingError",
    }:
        return True
    return False


def download_to(key: str, local_path: Path) -> Path:
    bucket = _get_bucket()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(OSS_DOWNLOAD_RETRIES) + 1)
    last_exc: BaseException | None = None

    for attempt in range(attempts):
        try:
            if local_path.exists():
                local_path.unlink(missing_ok=True)
            bucket.get_object_to_file(key, str(local_path))
            if not local_path.exists() or local_path.stat().st_size < 1:
                local_path.unlink(missing_ok=True)
                raise OssError("从 OSS 下载的文件为空")
            if attempt > 0:
                logger.info(
                    "OSS download succeeded after retry key=%s attempt=%s/%s",
                    key,
                    attempt + 1,
                    attempts,
                )
            return local_path
        except OssError:
            raise
        except Exception as exc:
            last_exc = exc
            local_path.unlink(missing_ok=True)
            retryable = _is_retryable_oss_error(exc)
            if (not retryable) or attempt >= attempts - 1:
                logger.exception(
                    "OSS download failed key=%s attempt=%s/%s",
                    key,
                    attempt + 1,
                    attempts,
                )
                raise OssError(f"从 OSS 下载失败: {exc}") from exc
            sleep_s = OSS_DOWNLOAD_RETRY_BASE_SLEEP * (2**attempt)
            logger.warning(
                "OSS download retry key=%s attempt=%s/%s sleep=%.1fs err=%s",
                key,
                attempt + 1,
                attempts,
                sleep_s,
                exc,
            )
            time.sleep(sleep_s)

    raise OssError(f"从 OSS 下载失败: {last_exc}")


def upload_file(local_path: Path, key: str) -> str:
    bucket = _get_bucket()
    if not local_path.exists():
        raise OssError(f"本地文件不存在: {local_path}")
    try:
        bucket.put_object_from_file(key, str(local_path))
    except Exception as exc:
        logger.exception("OSS upload failed key=%s", key)
        raise OssError(f"上传到 OSS 失败: {exc}") from exc
    return key


def staging_object_key(task_id: str, filename: str) -> str:
    return f"{OSS_STAGING_PREFIX}jobs/{task_id}/{Path(filename).name}"


def clips_object_key(source_key: str, filename: str) -> str:
    """Return the pre-saved clip key beside the source object's directory."""
    raw = (source_key or "").strip().replace("\\", "/").lstrip("/")
    parent = raw.rsplit("/", 1)[0] if "/" in raw else ""
    prefix = f"{parent}/clips/" if parent else "clips/"
    return f"{prefix}{Path(filename).name}"


def clips_prefix(source_key: str) -> str:
    """Return the directory used for pre-saved clips beside a source key."""
    return clips_object_key(source_key, "").rsplit("/", 1)[0] + "/"


def multipart_upload_fileobj(
    key: str,
    stream: BinaryIO,
    *,
    content_type: str = "application/octet-stream",
    part_size: int | None = None,
) -> str:
    """顺序读取 file-like 对象并 multipart 上传；失败时中止上传。"""
    bucket = _get_bucket()
    size = max(
        100 * 1024,
        int(part_size or OSS_MULTIPART_PART_SIZE_MB * 1024 * 1024),
    )
    upload_id: str | None = None
    try:
        result = bucket.init_multipart_upload(
            key, headers={"Content-Type": content_type}
        )
        upload_id = result.upload_id
        parts = []
        part_number = 1
        while True:
            chunk = stream.read(size)
            if not chunk:
                break
            uploaded = bucket.upload_part(key, upload_id, part_number, chunk)
            import oss2

            parts.append(oss2.models.PartInfo(part_number, uploaded.etag))
            part_number += 1
        if not parts:
            raise OssError("不能上传空对象")
        bucket.complete_multipart_upload(key, upload_id, parts)
        return key
    except Exception as exc:
        if upload_id:
            try:
                bucket.abort_multipart_upload(key, upload_id)
            except Exception:
                logger.warning("abort multipart failed key=%s", key)
        if isinstance(exc, OssError):
            raise
        raise OssError(f"multipart 上传失败: {exc}") from exc


class _ChunkReader:
    def __init__(self, chunks: Iterable[bytes]):
        self._iter = iter(chunks)
        self._buffer = bytearray()
        self._done = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return b"".join([bytes(self._buffer), *self._iter])
        while len(self._buffer) < size and not self._done:
            try:
                self._buffer.extend(next(self._iter))
            except StopIteration:
                self._done = True
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


def multipart_upload_chunks(
    key: str,
    chunks: Iterable[bytes],
    *,
    content_type: str = "application/octet-stream",
) -> str:
    return multipart_upload_fileobj(
        key, _ChunkReader(chunks), content_type=content_type
    )


def copy_object(source_key: str, target_key: str) -> str:
    bucket = _get_bucket()
    try:
        bucket.copy_object(OSS_BUCKET, source_key, target_key)
    except Exception as exc:
        raise OssError(f"OSS 同桶复制失败: {exc}") from exc
    if head_object_size(source_key) != head_object_size(target_key):
        delete_object(target_key)
        raise OssError("OSS 同桶复制校验失败：目标大小不一致")
    return target_key


def delete_prefix_older_than(
    prefix: str,
    hours: float,
    *,
    keep_keys: set[str] | None = None,
) -> int:
    """清理服务受管前缀中的过期对象。"""
    if not prefix or hours <= 0:
        return 0
    bucket = _get_bucket()
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    deleted = 0
    token = ""
    while True:
        result = bucket.list_objects_v2(prefix=prefix, continuation_token=token)
        for obj in getattr(result, "object_list", []) or []:
            if keep_keys and obj.key in keep_keys:
                continue
            if float(getattr(obj, "last_modified", 0) or 0) < cutoff:
                bucket.delete_object(obj.key)
                deleted += 1
        if not getattr(result, "is_truncated", False):
            break
        token = getattr(result, "next_continuation_token", "") or ""
        if not token:
            break
    return deleted


def sign_url(
    key: str,
    expires: int | None = None,
    *,
    prefer_cdn: bool = True,
) -> str:
    """生成对象 URL；服务端处理可禁用 CDN，强制使用 OSS 签名 URL。"""
    from urllib.parse import quote

    from backend.config import OSS_CDN_BASE

    raw_key = (key or "").strip().lstrip("/")
    cdn_base = (OSS_CDN_BASE or "").rstrip("/")
    if prefer_cdn and raw_key and cdn_base:
        encoded = "/".join(
            quote(seg, safe="") for seg in raw_key.split("/") if seg != ""
        )
        if encoded:
            return f"{cdn_base}/{encoded}"

    bucket = _get_bucket()
    ttl = expires if expires is not None else OSS_SIGN_EXPIRES
    try:
        return bucket.sign_url("GET", key, ttl)
    except Exception as exc:
        raise OssError(f"生成签名 URL 失败: {exc}") from exc


def cdn_url(key: str) -> str | None:
    """Return the configured public CDN URL without probing it."""
    from backend.config import OSS_CDN_BASE
    raw_key = (key or "").strip().lstrip("/")
    base = (OSS_CDN_BASE or "").rstrip("/")
    if not raw_key or not base:
        return None
    from urllib.parse import quote
    encoded = "/".join(quote(seg, safe="") for seg in raw_key.split("/") if seg)
    return f"{base}/{encoded}" if encoded else None


def preview_url(key: str) -> str:
    """Return CDN URL when reachable, otherwise a short-lived OSS URL."""
    target = cdn_url(key)
    if target:
        now = time.monotonic()
        with _cdn_probe_lock:
            cached = _cdn_object_probe.get(key)
            if cached and now < cached[0]:
                if cached[1]:
                    return target
            else:
                ok = False
                try:
                    req = Request(target, headers={"Range": "bytes=0-0"}, method="GET")
                    with urlopen(req, timeout=3) as response:
                        ok = int(getattr(response, "status", 200)) < 400
                except Exception:
                    ok = False
                _cdn_object_probe[key] = (now + 30, ok)
                if ok:
                    return target
    return sign_url(key, prefer_cdn=False)


def head_object_size(key: str) -> int:
    bucket = _get_bucket()
    try:
        meta = bucket.head_object(key)
        return int(getattr(meta, "content_length", 0) or 0)
    except Exception as exc:
        raise OssError(f"读取 OSS 对象元信息失败: {exc}") from exc


def iter_object_bytes(
    key: str,
    *,
    start: int | None = None,
    end: int | None = None,
    chunk_size: int = 1024 * 256,
):
    """按块读取 OSS 对象；可选 byte range（含端点）。"""
    bucket = _get_bucket()
    try:
        if start is None:
            result = bucket.get_object(key)
        else:
            # oss2: byte_range=(start, end) end 可为 None 表示直到末尾
            result = bucket.get_object(key, byte_range=(start, end))
    except Exception as exc:
        logger.exception("OSS get_object failed key=%s", key)
        raise OssError(f"从 OSS 读取失败: {exc}") from exc

    try:
        while True:
            chunk = result.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            result.close()
        except Exception:
            pass


def delete_object(key: str) -> None:
    if not key:
        return
    bucket = _get_bucket()
    try:
        bucket.delete_object(key)
    except Exception as exc:
        logger.warning("OSS delete failed key=%s err=%s", key, exc)


def object_exists(key: str) -> bool:
    bucket = _get_bucket()
    try:
        return bool(bucket.object_exists(key))
    except Exception:
        return False


def probe() -> None:
    """轻量探测 bucket 可达与凭证有效；失败抛 OssError。"""
    bucket = _get_bucket()
    try:
        bucket.get_bucket_info()
    except Exception as exc:
        raise OssError(f"OSS 探测失败: {exc}") from exc
