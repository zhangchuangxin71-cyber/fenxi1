import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  buildTicks,
  clampViewSeconds,
  defaultViewSeconds,
  formatViewLabel,
  scrollRangeIntoView,
  scrollTimeIntoView,
  scrollToKeepTime,
  timeAtClientX,
  visibleTimeRange,
  zoomFromView,
  zoomInView,
  zoomOutView,
  type TickRange,
} from "../utils/timelineScale";

/**
 * 按「视口可见时长」缩放：长/短视频默认细粒度接近，+/- 按比例放大缩小。
 */
export function useTimelineZoom(duration: number, currentTime: number) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [viewSeconds, setViewSeconds] = useState(() =>
    defaultViewSeconds(duration),
  );
  const [tickRange, setTickRange] = useState<TickRange>({
    start: 0,
    end: duration || 0,
  });
  const currentTimeRef = useRef(currentTime);
  currentTimeRef.current = currentTime;
  const viewRef = useRef(viewSeconds);
  viewRef.current = viewSeconds;
  const followRafRef = useRef(0);

  const zoom = zoomFromView(duration, viewSeconds);
  const atMin = duration > 0 && viewSeconds <= Math.min(5, duration) + 0.01;
  const atMax = duration > 0 && viewSeconds >= duration - 0.01;
  const viewLabel = formatViewLabel(viewSeconds, duration);

  const refreshTickRange = useCallback(() => {
    setTickRange(visibleTimeRange(scrollRef.current, duration));
  }, [duration]);

  /** 确保某时刻在横向滚动视口内（贴边则自动滑动） */
  const ensureTimeVisible = useCallback(
    (time: number) => {
      if (!duration) return;
      const el = scrollRef.current;
      if (!el) return;
      if (scrollTimeIntoView(el, time, duration)) {
        refreshTickRange();
      }
    },
    [duration, refreshTickRange],
  );

  /** 确保浅蓝选区区间在视口内（比视口宽时优先保入点） */
  const ensureRangeVisible = useCallback(
    (start: number, end: number) => {
      if (!duration) return;
      const el = scrollRef.current;
      if (!el) return;
      if (scrollRangeIntoView(el, start, end, duration)) {
        refreshTickRange();
      }
    },
    [duration, refreshTickRange],
  );

  const applyViewSeconds = useCallback(
    (next: number, anchorTime?: number) => {
      const clamped = clampViewSeconds(next, duration);
      const prev = viewRef.current;
      if (Math.abs(clamped - prev) < 0.01) return;
      const t = anchorTime ?? (duration > 0 ? currentTimeRef.current : 0);
      setViewSeconds(clamped);
      requestAnimationFrame(() => {
        scrollToKeepTime(scrollRef.current, t, duration);
        refreshTickRange();
      });
    },
    [duration, refreshTickRange],
  );

  const fitZoom = useCallback(() => {
    setViewSeconds(duration > 0 ? duration : defaultViewSeconds(0));
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
    requestAnimationFrame(refreshTickRange);
  }, [duration, refreshTickRange]);

  const zoomIn = useCallback(() => {
    applyViewSeconds(zoomInView(viewRef.current, duration));
  }, [applyViewSeconds, duration]);

  const zoomOut = useCallback(() => {
    applyViewSeconds(zoomOutView(viewRef.current, duration));
  }, [applyViewSeconds, duration]);

  const minViewSeconds = duration > 0 ? Math.min(5, duration) : 5;
  const maxViewSeconds = duration > 0 ? duration : defaultViewSeconds(0);
  const zoomPct =
    maxViewSeconds > minViewSeconds
      ? Math.round(
          ((maxViewSeconds - viewSeconds) / (maxViewSeconds - minViewSeconds)) *
            100,
        )
      : 0;

  const setZoomPct = useCallback(
    (pct: number) => {
      const clamped = Math.max(0, Math.min(100, pct));
      const next =
        maxViewSeconds -
        (clamped / 100) * (maxViewSeconds - minViewSeconds);
      applyViewSeconds(next);
    },
    [applyViewSeconds, maxViewSeconds, minViewSeconds],
  );

  // 换片：按新时长给合理默认窗口（短片全片，长片约 2 分钟）
  useEffect(() => {
    setViewSeconds(defaultViewSeconds(duration));
    if (scrollRef.current) scrollRef.current.scrollLeft = 0;
    requestAnimationFrame(refreshTickRange);
  }, [duration, refreshTickRange]);

  // 播放头（橙线）靠近/超出视口边缘时，带动底部横向滚动条跟随
  useEffect(() => {
    if (!duration) return;
    if (followRafRef.current) cancelAnimationFrame(followRafRef.current);
    followRafRef.current = requestAnimationFrame(() => {
      ensureTimeVisible(currentTime);
    });
    return () => {
      if (followRafRef.current) cancelAnimationFrame(followRafRef.current);
    };
  }, [currentTime, duration, ensureTimeVisible]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!duration || !(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const anchor = timeAtClientX(el, e.clientX, duration);
      const factor = e.deltaY < 0 ? 1 / 1.2 : 1.2;
      applyViewSeconds(viewRef.current * factor, anchor);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [applyViewSeconds, duration]);

  const ticks = useMemo(
    () => buildTicks(duration, viewSeconds, tickRange),
    [duration, viewSeconds, tickRange],
  );

  return {
    scrollRef,
    zoom,
    viewSeconds,
    viewLabel,
    atMin,
    atMax,
    ticks,
    fitZoom,
    zoomIn,
    zoomOut,
    zoomPct,
    setZoomPct,
    refreshTickRange,
    ensureTimeVisible,
    ensureRangeVisible,
  };
}
