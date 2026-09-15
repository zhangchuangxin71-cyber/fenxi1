"""任意 http(s) 源片下载（含 SSRF 防护）。"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from backend.config import (
    IMPORT_ALLOWED_HOSTS,
    MAX_UPLOAD_SIZE,
    OSS_DOWNLOAD_RETRIES,
    OSS_DOWNLOAD_RETRY_BASE_SLEEP,
)

logger = logging.getLogger(__name__)

# 下载超时（秒）：连接 + 读
HTTP_DOWNLOAD_TIMEOUT = 120
# 手动跟随重定向上限
MAX_REDIRECTS = 5

_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


class HttpDownloadError(Exception):
    pass


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    for net in _BLOCKED_NETWORKS:
        if ip in net:
            return True
    return False


def _resolve_host_ips(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HttpDownloadError(f"无法解析主机名: {hostname}") from exc
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            ips.append(addr)
    if not ips:
        raise HttpDownloadError(f"无法解析主机名: {hostname}")
    return ips


def _validate_connected_peer(resp) -> None:
    """Reject a connection that resolved to a restricted address after validation."""
    try:
        sock = resp.fp.raw._sock  # urllib HTTPResponse internals
        peer = sock.getpeername()[0]
        ip = ipaddress.ip_address(peer)
    except (AttributeError, OSError, ValueError, IndexError):
        return
    if _is_blocked_ip(ip):
        raise HttpDownloadError(f"url 实际连接到受限 IP: {peer}")


def validate_public_http_url(url: str) -> str:
    """
    校验 URL 可作源片下载地址：仅 http(s)，主机解析后禁止内网/元数据等地址。
    返回规范化后的 URL 字符串。
    """
    text = (url or "").strip()
    if not text:
        raise HttpDownloadError("url 不能为空")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise HttpDownloadError("url 仅支持 http/https")
    if not parsed.hostname:
        raise HttpDownloadError("url 缺少主机名")
    if parsed.username or parsed.password:
        raise HttpDownloadError("url 不允许包含用户名/密码")
    host = parsed.hostname
    if IMPORT_ALLOWED_HOSTS and host.lower().rstrip(".") not in IMPORT_ALLOWED_HOSTS:
        raise HttpDownloadError("url 主机不在 IMPORT_ALLOWED_HOSTS 白名单中")
    # 字面量 IP
    try:
        literal = ipaddress.ip_address(host)
        if _is_blocked_ip(literal):
            raise HttpDownloadError("url 目标地址不允许（内网或保留地址）")
    except ValueError:
        for ip_str in _resolve_host_ips(host):
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_blocked_ip(ip):
                raise HttpDownloadError(
                    f"url 目标地址不允许（解析到受限 IP: {ip_str}）"
                )
    return text


class _NoRedirect(HTTPRedirectHandler):
    """禁止自动跳转，由调用方校验后再跟。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _guess_filename(url: str, content_disposition: str | None) -> str:
    if content_disposition:
        # filename="..." 或 filename*=UTF-8''...
        lower = content_disposition.lower()
        if "filename*=" in lower:
            try:
                part = content_disposition.split("filename*=", 1)[1].split(";", 1)[0]
                part = part.strip().strip('"')
                if "''" in part:
                    part = part.split("''", 1)[1]
                name = unquote(part).strip()
                if name:
                    return Path(name).name
            except Exception:
                pass
        if "filename=" in lower:
            try:
                part = content_disposition.split("filename=", 1)[1].split(";", 1)[0]
                name = unquote(part.strip().strip('"')).strip()
                if name:
                    return Path(name).name
            except Exception:
                pass
    path_name = Path(unquote(urlparse(url).path or "")).name
    return path_name or "source.mp4"


def download_http_to(
    url: str,
    local_path: Path,
    *,
    max_bytes: int | None = None,
) -> Path:
    """将公开 http(s) URL 流式下载到本地；校验 SSRF，限制体积。"""
    from urllib.parse import urljoin

    validated = validate_public_http_url(url)
    limit = max_bytes if max_bytes is not None else MAX_UPLOAD_SIZE
    attempts = max(1, int(OSS_DOWNLOAD_RETRIES) + 1)
    last_exc: BaseException | None = None
    opener = build_opener(_NoRedirect())

    for attempt in range(attempts):
        current = validated
        try:
            if local_path.exists():
                local_path.unlink(missing_ok=True)
            local_path.parent.mkdir(parents=True, exist_ok=True)

            resp = None
            for _ in range(MAX_REDIRECTS + 1):
                validate_public_http_url(current)
                req = Request(
                    current,
                    headers={"User-Agent": "video-clip-import/1.0"},
                    method="GET",
                )
                try:
                    resp = opener.open(req, timeout=HTTP_DOWNLOAD_TIMEOUT)
                except HTTPError as exc:
                    if exc.code in (301, 302, 303, 307, 308):
                        loc = exc.headers.get("Location") if exc.headers else None
                        try:
                            exc.close()
                        except Exception:
                            pass
                        if not loc:
                            raise HttpDownloadError(
                                f"重定向缺少 Location（HTTP {exc.code}）"
                            ) from exc
                        current = urljoin(current, loc)
                        resp = None
                        continue
                    raise

                _validate_connected_peer(resp)
                code = getattr(resp, "status", None) or resp.getcode()
                if code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    try:
                        resp.close()
                    except Exception:
                        pass
                    if not loc:
                        raise HttpDownloadError(f"重定向缺少 Location（HTTP {code}）")
                    current = urljoin(current, loc)
                    resp = None
                    continue
                break
            else:
                raise HttpDownloadError(f"重定向次数超过上限（{MAX_REDIRECTS}）")

            assert resp is not None
            try:
                code = getattr(resp, "status", None) or resp.getcode()
                if code != 200:
                    raise HttpDownloadError(f"下载失败: HTTP {code}")

                cl = resp.headers.get("Content-Length")
                if cl is not None:
                    try:
                        if int(cl) > limit:
                            raise HttpDownloadError(
                                f"文件过大（Content-Length={cl}，上限 {limit} 字节）"
                            )
                    except ValueError:
                        pass

                written = 0
                with local_path.open("wb") as out:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            raise HttpDownloadError(
                                f"文件过大（超过上限 {limit} 字节）"
                            )
                        out.write(chunk)

                if written < 1:
                    raise HttpDownloadError("下载的文件为空")

                if attempt > 0:
                    logger.info(
                        "HTTP download succeeded after retry url=%s attempt=%s/%s",
                        validated,
                        attempt + 1,
                        attempts,
                    )
                return local_path
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        except HttpDownloadError:
            local_path.unlink(missing_ok=True)
            raise
        except (HTTPError, URLError, TimeoutError, OSError, socket.timeout) as exc:
            last_exc = exc
            local_path.unlink(missing_ok=True)
            if attempt >= attempts - 1:
                logger.exception(
                    "HTTP download failed url=%s attempt=%s/%s",
                    validated,
                    attempt + 1,
                    attempts,
                )
                raise HttpDownloadError(f"HTTP 下载失败: {exc}") from exc
            sleep_s = OSS_DOWNLOAD_RETRY_BASE_SLEEP * (2**attempt)
            logger.warning(
                "HTTP download retry url=%s attempt=%s/%s sleep=%.1fs err=%s",
                validated,
                attempt + 1,
                attempts,
                sleep_s,
                exc,
            )
            time.sleep(sleep_s)
        except Exception as exc:
            last_exc = exc
            local_path.unlink(missing_ok=True)
            raise HttpDownloadError(f"HTTP 下载失败: {exc}") from exc

    raise HttpDownloadError(f"HTTP 下载失败: {last_exc}")


def filename_from_url(url: str) -> str:
    return _guess_filename(url, None)


def iter_http_bytes(
    url: str,
    *,
    max_bytes: int | None = None,
    chunk_size: int = 1024 * 256,
):
    """安全地逐块读取公网 URL，供直接转存 OSS 使用，不写本地文件。"""
    from urllib.parse import urljoin

    current = validate_public_http_url(url)
    limit = max_bytes if max_bytes is not None else MAX_UPLOAD_SIZE
    opener = build_opener(_NoRedirect())
    resp = None
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_http_url(current)
        req = Request(
            current,
            headers={"User-Agent": "video-clip-import/1.0"},
            method="GET",
        )
        try:
            resp = opener.open(req, timeout=HTTP_DOWNLOAD_TIMEOUT)
        except HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise HttpDownloadError(f"HTTP 下载失败: {exc}") from exc
            loc = exc.headers.get("Location") if exc.headers else None
            exc.close()
            if not loc:
                raise HttpDownloadError(f"重定向缺少 Location（HTTP {exc.code}）")
            current = urljoin(current, loc)
            continue
        _validate_connected_peer(resp)
        code = getattr(resp, "status", None) or resp.getcode()
        if code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            resp = None
            if not loc:
                raise HttpDownloadError(f"重定向缺少 Location（HTTP {code}）")
            current = urljoin(current, loc)
            continue
        if code != 200:
            resp.close()
            raise HttpDownloadError(f"下载失败: HTTP {code}")
        break
    else:
        raise HttpDownloadError(f"重定向次数超过上限（{MAX_REDIRECTS}）")

    assert resp is not None
    content_length = resp.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HttpDownloadError(
                    f"文件过大（Content-Length={content_length}，上限 {limit} 字节）"
                )
        except ValueError:
            pass
    written = 0
    try:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                raise HttpDownloadError(f"文件过大（超过上限 {limit} 字节）")
            yield chunk
        if written < 1:
            raise HttpDownloadError("下载的文件为空")
    except (URLError, TimeoutError, OSError, socket.timeout) as exc:
        raise HttpDownloadError(f"HTTP 下载失败: {exc}") from exc
    finally:
        resp.close()
