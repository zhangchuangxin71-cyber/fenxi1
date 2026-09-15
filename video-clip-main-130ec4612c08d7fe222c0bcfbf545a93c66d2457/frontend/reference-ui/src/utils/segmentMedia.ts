import type { SegmentResponse } from "../api/types";
import { toProxyMediaUrl } from "../api/client";

type SegMediaFields = Pick<
  SegmentResponse,
  "id" | "thumb_url" | "preview_url" | "download_url" | "status"
>;

/** 片段视频/封面是否已可拉取（避免切割中抢先请求导致 404 被缓存） */
export function isSegmentMediaReady(
  seg: Pick<SegmentResponse, "status" | "preview_url" | "download_url" | "thumb_url">,
): boolean {
  const st = (seg.status || "").toLowerCase();
  if (st === "done" || st === "exported" || st === "completed") return true;
  return Boolean(seg.preview_url || seg.download_url || seg.thumb_url);
}

/** 切割成品预览地址（优先接口字段；未就绪不拼 URL） */
export function segmentPreviewUrl(seg: SegMediaFields): string | null {
  // 已发布且后端返回了 CDN/绝对 https → 直连
  // 本机 API 路径一律改成同源 /api/...，走 Vite 代理（避免错误的 API_PUBLIC_BASE / 前导 =）
  const raw = seg.preview_url || "";
  const normalized = toProxyMediaUrl(raw);
  if (normalized) {
    try {
      const u = new URL(normalized, window.location.origin);
      if (u.pathname.startsWith("/api/v1/segments/") && u.pathname.includes("/preview")) {
        return `${u.pathname}${u.search}${u.hash}`;
      }
    } catch {
      /* ignore */
    }
    if (/^https?:\/\//i.test(normalized)) return normalized;
    if (normalized.startsWith("/")) return normalized;
  }
  if (seg.download_url) {
    const fromDl = toProxyMediaUrl(
      seg.download_url.replace(/\/download\/?$/, "/preview"),
    );
    if (fromDl) {
      try {
        const u = new URL(fromDl, window.location.origin);
        if (u.pathname.startsWith("/api/v1/segments/")) {
          return `${u.pathname}${u.search}${u.hash}`;
        }
      } catch {
        /* ignore */
      }
      if (/^https?:\/\//i.test(fromDl)) return fromDl;
    }
  }
  if (seg.id && isSegmentMediaReady(seg)) {
    return `/api/v1/segments/${seg.id}/preview`;
  }
  return null;
}

/** 切割成品封面（首帧 JPEG）；未就绪返回 null，避免空打 /thumb */
export function segmentThumbUrl(seg: SegMediaFields): string | null {
  const raw = seg.thumb_url || "";
  const normalized = toProxyMediaUrl(raw);
  if (normalized) {
    try {
      const u = new URL(normalized, window.location.origin);
      if (u.pathname.startsWith("/api/v1/segments/") && u.pathname.includes("/thumb")) {
        return `${u.pathname}${u.search}${u.hash}`;
      }
    } catch {
      /* ignore */
    }
    if (/^https?:\/\//i.test(normalized)) return normalized;
    if (normalized.startsWith("/")) return normalized;
  }
  if (seg.id && isSegmentMediaReady(seg)) {
    return `/api/v1/segments/${seg.id}/thumb`;
  }
  const preview = segmentPreviewUrl(seg);
  if (preview && preview.includes("/preview")) {
    return preview.replace(/\/preview\/?$/, "/thumb");
  }
  return null;
}

export function segmentDownloadUrl(
  seg: Pick<SegmentResponse, "id" | "download_url" | "status">,
): string | null {
  if (seg.download_url) return toProxyMediaUrl(seg.download_url);
  if (seg.id && isSegmentMediaReady(seg)) {
    return `/api/v1/segments/${seg.id}/download`;
  }
  return null;
}

export function canPreviewSegment(seg: SegMediaFields): boolean {
  return Boolean(segmentPreviewUrl(seg));
}

/** 预热切割成品封面，列表出现前并行打 /thumb */
export function prefetchSegmentThumbs(segs: SegMediaFields[]): void {
  for (const seg of segs) {
    if (!isSegmentMediaReady(seg)) continue;
    const url = segmentThumbUrl(seg);
    if (!url) continue;
    const img = new Image();
    img.decoding = "async";
    img.src = url;
  }
}
