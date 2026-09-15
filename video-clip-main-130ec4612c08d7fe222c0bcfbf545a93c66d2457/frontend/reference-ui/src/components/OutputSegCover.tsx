import { useEffect, useState } from "react";

interface Props {
  thumbUrl?: string | null;
  videoUrl?: string | null;
  className?: string;
  /** 源片宽高比（宽/高）；不传则用缩略图/视频自身尺寸 */
  aspectRatio?: number | null;
}

const MAX_THUMB_RETRIES = 6;
const RETRY_MS = [400, 800, 1200, 2000, 3000, 4000];

function resolveAspect(
  aspectRatio?: number | null,
  mediaRatio?: number | null,
): number | null {
  if (aspectRatio && Number.isFinite(aspectRatio) && aspectRatio > 0.05) {
    return aspectRatio;
  }
  if (mediaRatio && Number.isFinite(mediaRatio) && mediaRatio > 0.05) {
    return mediaRatio;
  }
  return null;
}

/**
 * 切割成品封面：矩形框跟随源片比例（竖屏竖框、横屏横框）。
 * 先用视频元数据帧占位，JPEG 就绪后替换；不强制 16:9，不裁成椭圆/圆。
 */
export function OutputSegCover({
  thumbUrl,
  videoUrl,
  className = "",
  aspectRatio,
}: Props) {
  const [thumbOk, setThumbOk] = useState(false);
  const [thumbSrc, setThumbSrc] = useState<string | null>(null);
  const [videoFailed, setVideoFailed] = useState(false);
  const [mediaRatio, setMediaRatio] = useState<number | null>(null);

  useEffect(() => {
    setThumbOk(false);
    setThumbSrc(null);
    setVideoFailed(false);
    setMediaRatio(null);
    if (!thumbUrl) return;

    let cancelled = false;
    let timer: number | null = null;
    let attempt = 0;

    const load = () => {
      if (cancelled) return;
      const img = new Image();
      img.decoding = "async";
      const src =
        attempt === 0
          ? thumbUrl
          : `${thumbUrl}${thumbUrl.includes("?") ? "&" : "?"}_r=${attempt}`;
      img.onload = () => {
        if (cancelled) return;
        if (img.naturalWidth > 0 && img.naturalHeight > 0) {
          setMediaRatio(img.naturalWidth / img.naturalHeight);
        }
        setThumbSrc(src);
        setThumbOk(true);
      };
      img.onerror = () => {
        if (cancelled) return;
        setThumbOk(false);
        if (attempt >= MAX_THUMB_RETRIES) return;
        const wait = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
        attempt += 1;
        timer = window.setTimeout(load, wait);
      };
      img.src = src;
    };

    load();
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [thumbUrl]);

  const ratio = resolveAspect(aspectRatio, mediaRatio);
  const frameStyle = ratio
    ? ({ aspectRatio: String(ratio), width: "100%" } as const)
    : ({ width: "100%" } as const);
  const frameClass = `output-seg-frame ${className}`.trim();

  if (thumbOk && thumbSrc) {
    return (
      <span className={frameClass} style={frameStyle}>
        <img
          className="output-seg-media output-seg-cover"
          src={thumbSrc}
          alt=""
          loading="eager"
          decoding="async"
          fetchPriority="high"
          draggable={false}
        />
      </span>
    );
  }

  if (videoUrl && !videoFailed) {
    return (
      <span className={frameClass} style={frameStyle}>
        <video
          className="output-seg-media output-seg-cover"
          src={videoUrl}
          muted
          playsInline
          preload="metadata"
          onLoadedMetadata={(e) => {
            const v = e.currentTarget;
            if (v.videoWidth > 0 && v.videoHeight > 0) {
              setMediaRatio(v.videoWidth / v.videoHeight);
            }
          }}
          onError={() => setVideoFailed(true)}
        />
      </span>
    );
  }

  if (!thumbUrl && !videoUrl) {
    return (
      <span className={`${frameClass} output-seg-ph`} style={frameStyle}>
        无预览
      </span>
    );
  }

  return (
    <span
      className={`${frameClass} output-seg-ph output-seg-ph-loading`}
      style={frameStyle}
      aria-hidden
    />
  );
}
