import { useCallback, useEffect, useRef, useState } from "react";

interface Props {
  /** 切割成品视频地址（无海报时的回退） */
  src?: string | null;
  /** 首帧封面 JPEG（优先使用） */
  posterSrc?: string | null;
  className?: string;
  /** 已知片段时长（秒），仅 video 回退时使用 */
  durationHint?: number;
}

/**
 * 从 video 元素截取当前帧为 dataURL，用作封面。
 */
function captureFrame(video: HTMLVideoElement): string | null {
  try {
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return null;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(video, 0, 0, w, h);
    return canvas.toDataURL("image/jpeg", 0.85);
  } catch {
    return null;
  }
}

/**
 * 列表缩略图：优先服务端首帧 JPEG；失败则用 video 截帧。
 */
export function SegmentThumb({
  src,
  posterSrc,
  className,
  durationHint: _durationHint,
}: Props) {
  void _durationHint;
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const capturedRef = useRef(false);

  // 优先加载服务端海报
  useEffect(() => {
    capturedRef.current = false;
    setFailed(false);
    setDisplayUrl(null);
    if (!posterSrc) return;

    let cancelled = false;
    const img = new Image();
    img.onload = () => {
      if (!cancelled) setDisplayUrl(posterSrc);
    };
    img.onerror = () => {
      if (!cancelled) setDisplayUrl(null);
    };
    img.src = posterSrc;
    return () => {
      cancelled = true;
    };
  }, [posterSrc, src]);

  const tryCapture = useCallback(() => {
    const v = videoRef.current;
    if (!v || capturedRef.current) return;
    const data = captureFrame(v);
    if (data) {
      capturedRef.current = true;
      setDisplayUrl(data);
      setFailed(false);
      try {
        v.pause();
      } catch {
        /* ignore */
      }
    }
  }, []);

  const onLoadedData = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    // 尽量停在首帧附近再截图
    const seekTo = 0.04;
    const onSeeked = () => {
      v.removeEventListener("seeked", onSeeked);
      tryCapture();
    };
    v.addEventListener("seeked", onSeeked);
    try {
      if (Math.abs(v.currentTime - seekTo) > 0.01) {
        v.currentTime = seekTo;
      } else {
        v.removeEventListener("seeked", onSeeked);
        tryCapture();
      }
    } catch {
      v.removeEventListener("seeked", onSeeked);
      tryCapture();
    }
  }, [tryCapture]);

  // 已有海报图
  if (displayUrl) {
    return (
      <span className="segment-thumb-wrap is-ready">
        <img className={className} src={displayUrl} alt="" />
      </span>
    );
  }

  if (failed || !src) {
    return <span className="segment-thumb-ph">无预览</span>;
  }

  // 隐藏 video 只为截帧；截到后切到 img
  return (
    <span className="segment-thumb-wrap">
      <span className="segment-thumb-ph" aria-hidden />
      <video
        ref={videoRef}
        className={className}
        src={src}
        muted
        playsInline
        preload="auto"
        onLoadedData={onLoadedData}
        onError={() => setFailed(true)}
      />
    </span>
  );
}

/** @deprecated 保留导出以免其它引用报错 */
export function thumbSeekTime(duration: number): number {
  if (!Number.isFinite(duration) || duration <= 0) return 0.5;
  if (duration <= 0.4) return Math.max(0, duration * 0.5);
  if (duration <= 2) return Math.min(0.4, duration * 0.35);
  return Math.min(1.2, duration * 0.2);
}

export function thumbSrcWithHint(src: string, seekAt: number): string {
  const base = src.split("#")[0];
  const t = Math.max(0, Math.round(seekAt * 10) / 10);
  return `${base}#t=${t}`;
}
