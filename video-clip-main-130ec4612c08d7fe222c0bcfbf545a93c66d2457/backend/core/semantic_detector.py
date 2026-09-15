"""豆包 Seed 语义分镜：调用火山方舟 Chat Completions 理解视频语义切点。"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from backend.config import (
    ARK_API_KEY,
    ARK_BASE_URL,
    ARK_MODEL,
    ARK_TIMEOUT,
    SEMANTIC_SAMPLE_FPS,
)
from backend.core.cutter import FFmpegError, get_video_info, _resolve_ffmpeg
from backend.utils.proc import run_text
from backend.core.scene_detector import DetectedScene

ProgressCallback = Callable[[float, str], None]

# 内部保护策略（非用户配置）：优先原片，过大或失败时再压代理片
# base64 约膨胀 1.33 倍，过大直传极易 SSL/断连，故阈值不宜太高
_PREFER_ORIGINAL_MAX_BYTES = 28 * 1024 * 1024  # 小于此体积优先原片
_PROXY_TARGET_BYTES = 24 * 1024 * 1024  # 代理片目标体积
_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_BASE_SLEEP = 8.0  # 秒；指数退避起点
_NETWORK_RETRIES = 3
_NETWORK_BASE_SLEEP = 2.0
# 原片上传网络失败时少重试，尽快降级压缩代理片
_ORIGINAL_NETWORK_RETRIES = 1


class SemanticDetectError(Exception):
    pass


class _ArkUploadError(SemanticDetectError):
    """上传/调用阶段错误，可供判断是否降级重试。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        rate_limited: bool = False,
        payload_issue: bool = False,
        network_issue: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.payload_issue = payload_issue
        self.network_issue = network_issue


_TIME_RE = re.compile(
    r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)$|^(?P<sec>\d+(?:\.\d+)?)$"
)


def _parse_time(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError(f"无法解析时间: {value}")
    if match.group("sec") is not None:
        return float(match.group("sec"))
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = float(match.group("s") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _extract_json_array(text: str) -> list:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("scenes", "segments", "shots", "clips"):
                if isinstance(data.get(key), list):
                    return data[key]
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise SemanticDetectError("模型未返回可解析的 JSON 分镜列表")
    data = json.loads(match.group(0))
    if not isinstance(data, list):
        raise SemanticDetectError("模型返回的 JSON 不是数组")
    return data


def _build_prompt(video_duration: float, min_scene_len: float, time_offset: float = 0.0) -> str:
    offset_note = ""
    if time_offset > 0:
        offset_note = (
            f"\n当前片段在原视频中的起点为 {time_offset:.3f} 秒；"
            "请输出相对本片段文件的时间（从 0 开始），系统会自动加上偏移。"
        )
    return f"""你是专业视频剪辑助手。请基于视频内容做「语义分镜」切分（按话题/场景/动作/叙事段落变化切开，不是单纯画面硬切）。

约束：
1. 视频总时长约 {video_duration:.3f} 秒；第一个片段 start_time=0，最后一个 end_time 接近视频结尾
2. 片段须连续无间隙：上一段 end_time = 下一段 start_time，且 start_time < end_time
3. 每个片段时长尽量 >= {min_scene_len:.1f} 秒；过短碎段应合并
4. 切点优先落在语义边界（话题切换、场景切换、明显动作段落开始/结束）
5. summary 用中文简述该段语义内容（不超过 40 字）
{offset_note}

只输出紧凑 JSON 数组，不要解释，不要 Markdown：
[{{"start_time":0.0,"end_time":8.5,"summary":"开场介绍","confidence":0.9}}]

时间用秒（浮点数）。confidence 为 0~1。"""


def _guess_video_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime and mime.startswith("video/"):
        return mime
    return "video/mp4"


def _format_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def _is_rate_limit_error(exc: BaseException) -> bool:
    if isinstance(exc, _ArkUploadError) and exc.rate_limited:
        return True
    text = str(exc).lower()
    markers = (
        "429",
        "requestbursttoofast",
        "too many requests",
        "rate limit",
        "ratelimit",
        "quota",
        "限流",
        "请求过快",
        "并发",
    )
    return any(m in text for m in markers)


def _is_payload_issue(exc: BaseException) -> bool:
    if isinstance(exc, _ArkUploadError) and exc.payload_issue:
        return True
    if isinstance(exc, MemoryError):
        return True
    text = str(exc).lower()
    markers = (
        "413",
        "too large",
        "entity too large",
        "request too large",
        "payload",
        "body size",
        "content length",
        "过大",
        "请求体",
        "memory",
        "oom",
    )
    return any(m in text for m in markers)


def _is_transient_network_error(exc: BaseException) -> bool:
    """SSL 中断、连接重置等瞬时网络问题。"""
    if isinstance(exc, _ArkUploadError) and exc.network_issue:
        return True
    text = str(exc).lower()
    markers = (
        "eof occurred in violation of protocol",
        "ssl",
        "broken pipe",
        "connection reset",
        "connection aborted",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "network is unreachable",
        "name or service not known",
        "remote end closed connection",
        "incomplete read",
        "protocol error",
        "unexpected_eof",
    )
    return any(m in text for m in markers)


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """体积/网络瞬时故障可压片降级；429 限流不走这条路径。"""
    if _is_rate_limit_error(exc):
        return False
    if _is_transient_network_error(exc):
        return True
    if isinstance(exc, (TimeoutError, socket.timeout, MemoryError)):
        return True
    if isinstance(exc, _ArkUploadError):
        return bool(
            exc.payload_issue
            or exc.network_issue
            or (exc.retryable and not exc.rate_limited)
        )
    return _is_payload_issue(exc)


def _file_to_data_uri(path: Path) -> str:
    mime = _guess_video_mime(path)
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except MemoryError as exc:
        raise _ArkUploadError(
            f"视频过大，内存不足无法编码上传（{_format_mb(path.stat().st_size)}）",
            retryable=True,
            payload_issue=True,
        ) from exc
    return f"data:{mime};base64,{data}"


def _compress_proxy(input_path: Path, output_path: Path, *, tighter: bool = False) -> Path:
    """生成低码率代理片，作为大文件保护降级。"""
    ffmpeg = _resolve_ffmpeg()
    scale = "min(480,iw)" if tighter else "min(854,iw)"
    fps = "8" if tighter else "12"
    crf = "36" if tighter else "32"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-vf",
        f"scale='{scale}':-2",
        "-r",
        fps,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        crf,
        "-an",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run_text(cmd)
    if result.returncode != 0:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(input_path),
            "-vf",
            f"scale='{scale}':-2",
            "-r",
            fps,
            "-c:v",
            "mpeg4",
            "-q:v",
            "10" if tighter else "8",
            "-an",
            str(output_path),
        ]
        result = run_text(cmd)
        if result.returncode != 0:
            raise FFmpegError(result.stderr.strip() or "压缩代理视频失败")
    return output_path


def _make_proxy_video(source: Path, work_dir: Path) -> Path:
    """尽量压到目标体积；压不到也返回当前最好的代理片（仍尝试上传）。"""
    proxy = work_dir / "proxy.mp4"
    _compress_proxy(source, proxy, tighter=False)
    if proxy.stat().st_size <= _PROXY_TARGET_BYTES:
        return proxy

    tighter = work_dir / "proxy_tight.mp4"
    try:
        _compress_proxy(proxy, tighter, tighter=True)
        if tighter.exists() and tighter.stat().st_size < proxy.stat().st_size:
            return tighter
    except FFmpegError:
        pass
    return proxy


def _classify_http_error(code: int, detail: str) -> tuple[bool, bool, bool]:
    """返回 (retryable, rate_limited, payload_issue)。"""
    text = f"{code} {detail}"
    rate_limited = code == 429 or _is_rate_limit_error(Exception(text))
    payload_issue = code == 413 or _is_payload_issue(Exception(text))
    retryable = rate_limited or payload_issue or code in (408, 500, 502, 503, 504)
    return retryable, rate_limited, payload_issue


def _invoke_ark_once(body: bytes) -> str:
    req = urllib.request.Request(
        f"{ARK_BASE_URL.rstrip('/')}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ARK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=ARK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        retryable, rate_limited, payload_issue = _classify_http_error(exc.code, detail)
        raise _ArkUploadError(
            f"豆包 API 请求失败 ({exc.code}): {detail}",
            retryable=retryable,
            rate_limited=rate_limited,
            payload_issue=payload_issue,
        ) from exc
    except TimeoutError as exc:
        raise _ArkUploadError("豆包 API 请求超时", retryable=True, network_issue=True) from exc
    except socket.timeout as exc:
        raise _ArkUploadError("豆包 API 请求超时", retryable=True, network_issue=True) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        network = _is_transient_network_error(Exception(reason))
        raise _ArkUploadError(
            f"豆包 API 网络错误: {reason}",
            retryable=network or _is_payload_issue(Exception(reason)),
            network_issue=network,
            payload_issue=_is_payload_issue(Exception(reason)),
        ) from exc
    except OSError as exc:
        # 偶发底层 socket/SSL 错误不一定包成 URLError
        reason = str(exc)
        network = _is_transient_network_error(exc)
        raise _ArkUploadError(
            f"豆包 API 网络错误: {reason}",
            retryable=network,
            network_issue=network,
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SemanticDetectError("豆包 API 返回非 JSON") from exc

    if data.get("error"):
        err = data["error"]
        msg = str(err)
        raise _ArkUploadError(
            msg,
            retryable=_is_rate_limit_error(Exception(msg)) or _is_payload_issue(Exception(msg)),
            rate_limited=_is_rate_limit_error(Exception(msg)),
            payload_issue=_is_payload_issue(Exception(msg)),
        )

    choices = data.get("choices") or []
    if not choices:
        raise SemanticDetectError("豆包 API 未返回 choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif isinstance(item, str):
                parts.append(item)
        content = "\n".join(parts)
    if not content or not isinstance(content, str):
        raise SemanticDetectError("豆包 API 返回空文本")
    return content


def _call_ark(
    video_path: Path,
    prompt: str,
    sample_fps: float,
    on_progress: Optional[ProgressCallback] = None,
    *,
    network_retries: int = _NETWORK_RETRIES,
) -> str:
    if not ARK_API_KEY:
        raise SemanticDetectError(
            "未配置 ARK_API_KEY，无法调用豆包语义分镜。请设置环境变量 ARK_API_KEY"
        )

    data_uri = _file_to_data_uri(video_path)
    payload = {
        "model": ARK_MODEL,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": data_uri,
                            "fps": sample_fps,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    body_mb = len(body) / (1024 * 1024)

    last_exc: Exception | None = None
    # 限流与网络重试分开计数，避免「网络失败却显示 4/5」拖很久
    rate_attempt = 0
    net_attempt = 0
    max_rounds = _RATE_LIMIT_RETRIES + max(0, network_retries) + 1

    for _ in range(max_rounds):
        stop_hb = threading.Event()
        hb_state = {"pct": 48.0}

        def _heartbeat():
            while not stop_hb.wait(6.0):
                hb_state["pct"] = min(88.0, hb_state["pct"] + 1.5)
                if on_progress:
                    on_progress(
                        hb_state["pct"],
                        "大模型分析中，请耐心等待（通常 1–5 分钟）…",
                    )

        hb_thread: threading.Thread | None = None
        if on_progress:
            on_progress(
                48,
                f"正在调用 {ARK_MODEL}（请求体约 {body_mb:.1f}MB，可能需数分钟）...",
            )
            hb_thread = threading.Thread(target=_heartbeat, daemon=True)
            hb_thread.start()

        try:
            return _invoke_ark_once(body)
        except _ArkUploadError as exc:
            last_exc = exc
            brief = str(exc).replace("\n", " ")[:100]

            if exc.rate_limited and rate_attempt < _RATE_LIMIT_RETRIES:
                sleep_s = _RATE_LIMIT_BASE_SLEEP * (2 ** rate_attempt)
                rate_attempt += 1
                if on_progress:
                    on_progress(
                        42,
                        f"触发方舟限流，{sleep_s:.0f}s 后重试"
                        f"（{rate_attempt}/{_RATE_LIMIT_RETRIES}）… {brief}",
                    )
                time.sleep(sleep_s)
                continue

            if (
                (exc.network_issue or (exc.retryable and not exc.payload_issue))
                and not exc.rate_limited
                and net_attempt < network_retries
            ):
                sleep_s = _NETWORK_BASE_SLEEP * (2 ** net_attempt)
                net_attempt += 1
                if on_progress:
                    on_progress(
                        42,
                        f"网络异常，{sleep_s:.0f}s 后重试"
                        f"（{net_attempt}/{network_retries}）… {brief}",
                    )
                time.sleep(sleep_s)
                continue

            raise
        finally:
            stop_hb.set()
            if hb_thread is not None:
                hb_thread.join(timeout=0.2)

    assert last_exc is not None
    raise last_exc


def _call_ark_with_protection(
    source: Path,
    prompt: str,
    sample_fps: float,
    on_progress: Optional[ProgressCallback] = None,
) -> str:
    """优先小体积原片；偏大或网络失败时压代理片再传。"""
    size = source.stat().st_size
    use_original_first = size <= _PREFER_ORIGINAL_MAX_BYTES

    with tempfile.TemporaryDirectory(prefix="semantic_scene_") as tmp:
        work_dir = Path(tmp)
        upload_path = source
        used_proxy = False

        if not use_original_first:
            if on_progress:
                on_progress(
                    25,
                    f"原片 {_format_mb(size)} 偏大，先压缩再上传（避免网络中断）...",
                )
            upload_path = _make_proxy_video(source, work_dir)
            used_proxy = True
            if on_progress:
                on_progress(
                    35,
                    f"代理片 {_format_mb(upload_path.stat().st_size)}，正在调用模型...",
                )
        else:
            if on_progress:
                on_progress(
                    25,
                    f"优先上传原片（{_format_mb(size)}）...",
                )

        try:
            if on_progress and not used_proxy:
                on_progress(40, "准备上传并调用大模型...")
            return _call_ark(
                upload_path,
                prompt,
                sample_fps=sample_fps,
                on_progress=on_progress,
                # 原片网络失败尽快放弃，交给下方压缩降级
                network_retries=(
                    _NETWORK_RETRIES if used_proxy else _ORIGINAL_NETWORK_RETRIES
                ),
            )
        except Exception as exc:
            # 429 不改走压缩；体积过大或原片上传 SSL/网络中断可降级代理片
            if used_proxy or not _is_retryable_upload_error(exc):
                raise

            brief = str(exc).replace("\n", " ")[:80]
            if on_progress:
                on_progress(
                    30,
                    f"原片上传失败，改为压缩后重试… {brief}",
                )
            proxy = _make_proxy_video(source, work_dir)
            if on_progress:
                on_progress(
                    40,
                    f"代理片 {_format_mb(proxy.stat().st_size)}，正在重试调用...",
                )
            return _call_ark(
                proxy,
                prompt,
                sample_fps=sample_fps,
                on_progress=on_progress,
                network_retries=_NETWORK_RETRIES,
            )


def _normalize_scenes(
    items: list,
    video_duration: float,
    min_scene_len: float,
    fps: float,
    time_offset: float = 0.0,
    cover_full: bool = True,
    range_start: float | None = None,
    range_end: float | None = None,
) -> list[DetectedScene]:
    span_start = range_start if range_start is not None else time_offset
    span_end = range_end if range_end is not None else video_duration

    scenes: list[DetectedScene] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start_time", item.get("start", item.get("begin")))
        end_raw = item.get("end_time", item.get("end", item.get("finish")))
        if start_raw is None or end_raw is None:
            continue
        try:
            start = _parse_time(start_raw) + time_offset
            end = _parse_time(end_raw) + time_offset
        except ValueError:
            continue
        start = max(span_start, min(start, span_end))
        end = max(span_start, min(end, span_end))
        if end <= start:
            continue
        summary = str(item.get("summary") or item.get("content_summary") or item.get("title") or "").strip()
        conf = item.get("confidence", 1.0)
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            confidence = 1.0
        start_frame = max(0, round(start * fps))
        end_frame = max(start_frame + 1, round(end * fps))
        scenes.append(
            DetectedScene(
                start=round(start, 6),
                end=round(end, 6),
                confidence=max(0.0, min(1.0, confidence)),
                start_frame=start_frame,
                end_frame=end_frame,
                summary=summary or None,
            )
        )

    if not scenes:
        raise SemanticDetectError("未能从模型结果中解析出有效分镜")

    scenes.sort(key=lambda s: s.start)

    # 强制连续半开区间（可选覆盖整片或仅本段）
    fixed: list[DetectedScene] = []
    cursor = span_start if cover_full else scenes[0].start
    for i, scene in enumerate(scenes):
        start = cursor if cover_full or i == 0 else max(cursor, scene.start)
        end = scene.end
        if cover_full and i == len(scenes) - 1:
            end = span_end
        if end <= start:
            continue
        start_frame = max(0, round(start * fps))
        end_frame = max(start_frame + 1, round(end * fps))
        fixed.append(
            DetectedScene(
                start=round(start, 6),
                end=round(end, 6),
                confidence=scene.confidence,
                start_frame=start_frame,
                end_frame=end_frame,
                summary=scene.summary,
            )
        )
        cursor = end

    if not fixed:
        raise SemanticDetectError("语义分镜归一化后为空")

    # 合并过短段
    min_frames = max(1, int(round(min_scene_len * fps)))
    merged: list[DetectedScene] = []
    for scene in fixed:
        length = (scene.end_frame or 0) - (scene.start_frame or 0)
        if merged and length < min_frames:
            prev = merged[-1]
            summary = prev.summary or scene.summary
            if prev.summary and scene.summary and prev.summary != scene.summary:
                summary = f"{prev.summary} / {scene.summary}"
            merged[-1] = DetectedScene(
                start=prev.start,
                end=scene.end,
                confidence=min(prev.confidence, scene.confidence),
                start_frame=prev.start_frame,
                end_frame=scene.end_frame,
                summary=summary,
            )
        else:
            merged.append(scene)

    if len(merged) >= 2:
        last = merged[-1]
        last_len = (last.end_frame or 0) - (last.start_frame or 0)
        if last_len < min_frames:
            prev = merged[-2]
            summary = prev.summary or last.summary
            if prev.summary and last.summary and prev.summary != last.summary:
                summary = f"{prev.summary} / {last.summary}"
            merged[-2] = DetectedScene(
                start=prev.start,
                end=last.end,
                confidence=min(prev.confidence, last.confidence),
                start_frame=prev.start_frame,
                end_frame=last.end_frame,
                summary=summary,
            )
            merged.pop()

    return merged


def detect_semantic_scenes(
    video_path: str | Path,
    min_scene_len: float = 2.0,
    sample_fps: float | None = None,
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[list[DetectedScene], float]:
    """调用 doubao-seed 语义分镜，返回 (分镜列表, fps)。

    整片一次送模：不按时长分段；优先原片，大文件/失败时自动压代理片保护。
    """
    path = Path(video_path)
    if not path.exists():
        raise SemanticDetectError("视频文件不存在")

    info = get_video_info(path)
    duration = float(info.get("duration") or 0)
    fps = float(info.get("fps") or 25.0) or 25.0
    if duration <= 0:
        raise SemanticDetectError("无法获取视频时长")

    sample = sample_fps if sample_fps is not None else SEMANTIC_SAMPLE_FPS
    sample = max(0.2, min(5.0, float(sample)))

    if on_progress:
        on_progress(15, "正在准备语义分析...")

    prompt = _build_prompt(duration, min_scene_len, time_offset=0.0)
    content = _call_ark_with_protection(
        path,
        prompt,
        sample_fps=sample,
        on_progress=on_progress,
    )
    if on_progress:
        on_progress(90, "模型已返回，正在解析分镜结果...")
    items = _extract_json_array(content)
    scenes = _normalize_scenes(
        items,
        video_duration=duration,
        min_scene_len=min_scene_len,
        fps=fps,
        time_offset=0.0,
        cover_full=True,
        range_start=0.0,
        range_end=duration,
    )
    if on_progress:
        on_progress(96, f"语义分镜完成，共 {len(scenes)} 段，正在写入预览...")
    return scenes, fps
