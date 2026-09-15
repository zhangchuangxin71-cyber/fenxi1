import { useEffect, useState } from "react";

export interface FilmstripInfo {
  url: string;
  count: number;
  loading: boolean;
  error: string | null;
  windowStart: number;
  windowEnd: number;
}

/** 列表 / 时间轴：约每 3 秒一格，受上限约束（与后端 MAX_COUNT 一致） */
const SECONDS_PER_THUMB = 3;
const MIN_COUNT = 8;
const MAX_COUNT = 120;
/** 先出一张稀疏条，让界面马上有图；再按需补到目标密度 */
const PREVIEW_COUNT = 40;

/** 缓存版本：与后端 CACHE_VER 一并 bump */
const CACHE_QS = "9";

function clampCount(n: number): number {
  return Math.max(MIN_COUNT, Math.min(MAX_COUNT, Math.round(n)));
}

/** 全片胶片张数：约每 3 秒一格，最长 MAX_COUNT */
export function suggestFilmstripCount(spanSeconds: number): number {
  const span = Math.max(spanSeconds || 0, 0.1);
  return clampCount(span / SECONDS_PER_THUMB);
}

/** 时间轴全片胶片张数（与 suggestFilmstripCount 同规则） */
export function suggestTimelineFilmstripCount(duration: number): number {
  return suggestFilmstripCount(duration);
}

async function fetchFilmstripBlob(
  videoId: string,
  qs: URLSearchParams,
  signal: AbortSignal,
): Promise<{ blob: Blob; count: number }> {
  const res = await fetch(`/api/v1/videos/${videoId}/filmstrip?${qs}`, { signal });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail || "缩略图生成失败");
  }
  const headerCount = Number(res.headers.get("X-Filmstrip-Count") || 0);
  const blob = await res.blob();
  return { blob, count: headerCount };
}

function filmstripQuery(count: number): URLSearchParams {
  // 不传 start/end：后端按整段生成，文件名只含 count，避免时长微调打穿缓存
  const qs = new URLSearchParams({ v: CACHE_QS, count: String(count) });
  return qs;
}

function applyBlobUrl(
  blob: Blob,
  setUrl: (updater: (prev: string) => string) => void,
): void {
  const objectUrl = URL.createObjectURL(blob);
  setUrl((prev) => {
    if (prev) URL.revokeObjectURL(prev);
    return objectUrl;
  });
}

/**
 * 整段只生成一次全片胶片条。
 * 策略：先拉 PREVIEW_COUNT 快速出图，再补到目标张数（若更大）。
 */
function useStaticFilmstrip(
  videoId: string | null | undefined,
  duration: number,
  targetCount: number | undefined,
  enabled: boolean,
): FilmstripInfo {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolvedCount, setResolvedCount] = useState(MIN_COUNT);

  const finalCount =
    targetCount != null
      ? clampCount(targetCount)
      : duration > 0
        ? suggestFilmstripCount(duration)
        : MIN_COUNT;

  useEffect(() => {
    // duration 未就绪时不要用默认 MIN_COUNT 空打，避免短片误请求失败
    if (!enabled || !videoId || !(duration > 0)) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 180_000);
    setLoading(true);
    setError(null);

    const previewCount = Math.min(PREVIEW_COUNT, finalCount);

    (async () => {
      try {
        // 1) 快速预览条
        const preview = await fetchFilmstripBlob(
          videoId,
          filmstripQuery(previewCount),
          controller.signal,
        );
        if (cancelled) return;
        applyBlobUrl(preview.blob, setUrl);
        setResolvedCount(preview.count || previewCount);
        // 有预览图即可结束「转圈」，后台继续补密
        setLoading(false);

        // 2) 目标更密时再拉完整条（命中缓存则很快）
        if (finalCount > previewCount) {
          const full = await fetchFilmstripBlob(
            videoId,
            filmstripQuery(finalCount),
            controller.signal,
          );
          if (cancelled) return;
          applyBlobUrl(full.blob, setUrl);
          setResolvedCount(full.count || finalCount);
        }
      } catch (e: unknown) {
        if (cancelled || (e instanceof DOMException && e.name === "AbortError")) {
          if (!cancelled) {
            setLoading(false);
            setError("缩略图超时或已取消");
          }
          return;
        }
        setError(e instanceof Error ? e.message : "缩略图生成失败");
        setLoading(false);
      } finally {
        clearTimeout(timer);
      }
    })();

    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [videoId, duration, finalCount, enabled]);

  useEffect(() => {
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);

  return {
    url,
    count: resolvedCount,
    loading,
    error,
    windowStart: 0,
    windowEnd: duration || 0,
  };
}

export function useFilmstrip(
  videoId: string | null | undefined,
  duration = 0,
  enabled = true,
): FilmstripInfo {
  const count = duration > 0 ? suggestFilmstripCount(duration) : undefined;
  return useStaticFilmstrip(videoId, duration, count, enabled);
}

export function useTimelineFilmstrip(
  videoId: string | null | undefined,
  duration = 0,
  enabled = true,
): FilmstripInfo {
  const count =
    duration > 0 ? suggestTimelineFilmstripCount(duration) : undefined;
  return useStaticFilmstrip(videoId, duration, count, enabled);
}

export function useOverviewFilmstrip(
  videoId: string | null | undefined,
  duration = 0,
  enabled = true,
): FilmstripInfo {
  return useTimelineFilmstrip(videoId, duration, enabled);
}

export function useViewportFilmstrip(
  videoId: string | null | undefined,
  duration: number,
  _scrollRef?: unknown,
  _viewSeconds?: number,
  enabled = true,
): FilmstripInfo {
  return useTimelineFilmstrip(videoId, duration, enabled);
}
