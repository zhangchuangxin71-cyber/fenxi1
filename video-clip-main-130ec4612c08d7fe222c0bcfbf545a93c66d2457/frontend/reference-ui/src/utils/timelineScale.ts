import { roundTime } from "./format";

/** 刻度候选步长（秒），从细到粗 */
const TICK_STEPS = [
  0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
];

/** 默认视口内目标可见时长：长视频打开后不必先 Fit 再狂放大 */
const DEFAULT_VIEW_SECONDS = 120;

/** 视口内最少 / 最多可见时长 */
const MIN_VIEW_SECONDS = 5;
const VIEW_ZOOM_FACTOR = 1.5;

export type TickRange = { start: number; end: number };

export function timeToPct(time: number, duration: number): number {
  if (!duration) return 0;
  return Math.max(0, Math.min(100, (time / duration) * 100));
}

/** 播放头线宽（与 CSS `.timeline-playhead` 一致） */
export const PLAYHEAD_LINE_PX = 4;
/** 两端内缩，避开圆角裁切与轨道描边 */
export const PLAYHEAD_EDGE_INSET_PX = 4;

/**
 * 播放头 left：以时刻为中心，并夹在轨道内（含两端 inset）。
 */
export function playheadLeftCss(
  time: number,
  duration: number,
  linePx = PLAYHEAD_LINE_PX,
  edgeInsetPx = PLAYHEAD_EDGE_INSET_PX,
): string {
  const pct = timeToPct(time, duration);
  const half = linePx / 2;
  return `max(${edgeInsetPx}px, min(calc(100% - ${linePx + edgeInsetPx}px), calc(${pct}% - ${half}px)))`;
}

/**
 * 根据视口可见时长选刻度步长，目标约 8～12 个主刻度。
 */
export function pickTickStep(visibleDuration: number): number {
  if (!visibleDuration || visibleDuration <= 0) return 5;
  const ideal = visibleDuration / 10;
  for (const step of TICK_STEPS) {
    if (step >= ideal) return step;
  }
  return TICK_STEPS[TICK_STEPS.length - 1];
}

/** 新视频的默认可见窗口：短片看全片，长片先看约 2 分钟 */
export function defaultViewSeconds(duration: number): number {
  if (!duration || duration <= 0) return DEFAULT_VIEW_SECONDS;
  return Math.min(duration, DEFAULT_VIEW_SECONDS);
}

export function clampViewSeconds(viewSeconds: number, duration: number): number {
  if (!duration || duration <= 0) return DEFAULT_VIEW_SECONDS;
  const min = Math.min(MIN_VIEW_SECONDS, duration);
  return Math.max(min, Math.min(duration, viewSeconds));
}

/** 内容宽度相对视口的倍数（≥1） */
export function zoomFromView(duration: number, viewSeconds: number): number {
  if (!duration || !viewSeconds) return 1;
  return Math.max(1, duration / clampViewSeconds(viewSeconds, duration));
}

export function zoomInView(viewSeconds: number, duration: number): number {
  return clampViewSeconds(viewSeconds / VIEW_ZOOM_FACTOR, duration);
}

export function zoomOutView(viewSeconds: number, duration: number): number {
  return clampViewSeconds(viewSeconds * VIEW_ZOOM_FACTOR, duration);
}

export function buildTicks(
  duration: number,
  viewSeconds: number,
  range?: TickRange,
): number[] {
  if (!duration) return [];
  const visible = clampViewSeconds(viewSeconds, duration);
  const step = pickTickStep(visible);
  const from = range ? Math.max(0, range.start) : 0;
  const to = range ? Math.min(duration, range.end) : duration;
  const first = Math.floor(from / step) * step;
  const out: number[] = [];
  for (let t = first; t <= to + step * 0.001; t += step) {
    if (t < -0.0001) continue;
    const v = roundTime(Math.min(duration, Math.max(0, t)));
    if (out.length === 0 || out[out.length - 1] !== v) out.push(v);
  }
  const end = roundTime(duration);
  if (
    (!range || range.end >= duration - 0.001) &&
    out[out.length - 1] !== end
  ) {
    out.push(end);
  }
  return out;
}

export function visibleTimeRange(
  scrollEl: HTMLElement | null,
  duration: number,
  padRatio = 0.5,
): TickRange {
  if (!scrollEl || !duration) return { start: 0, end: duration || 0 };
  const { scrollLeft, clientWidth, scrollWidth } = scrollEl;
  if (scrollWidth <= 0) return { start: 0, end: duration };
  const start = (scrollLeft / scrollWidth) * duration;
  const end = ((scrollLeft + clientWidth) / scrollWidth) * duration;
  const pad = Math.max(0, (end - start) * padRatio);
  return {
    start: Math.max(0, start - pad),
    end: Math.min(duration, end + pad),
  };
}

export function scrollToKeepTime(
  scrollEl: HTMLElement | null,
  time: number,
  duration: number,
  viewportAnchor = 0.5,
): void {
  if (!scrollEl || !duration) return;
  const apply = () => {
    const contentW = scrollEl.scrollWidth;
    const viewW = scrollEl.clientWidth;
    if (contentW <= viewW) {
      scrollEl.scrollLeft = 0;
      return;
    }
    const x = (time / duration) * contentW;
    scrollEl.scrollLeft = Math.max(
      0,
      Math.min(contentW - viewW, x - viewW * viewportAnchor),
    );
  };
  requestAnimationFrame(apply);
}

/**
 * 若 time 已靠近/超出可视区左右边缘，则滚动使该时刻回到视口内。
 * 用于播放头、选区跟随底部横向滚动条。
 * @returns 是否发生了滚动
 */
export function scrollTimeIntoView(
  scrollEl: HTMLElement | null,
  time: number,
  duration: number,
  options?: { marginRatio?: number },
): boolean {
  if (!scrollEl || !duration) return false;
  const contentW = scrollEl.scrollWidth;
  const viewW = scrollEl.clientWidth;
  if (contentW <= viewW + 1) return false;

  const t = Math.max(0, Math.min(duration, time));
  const x = (t / duration) * contentW;
  const left = scrollEl.scrollLeft;
  const right = left + viewW;
  const margin = Math.max(24, viewW * (options?.marginRatio ?? 0.12));

  let next = left;
  if (x < left + margin) {
    // 偏左：把时刻放到左侧留白处
    next = Math.max(0, x - margin);
  } else if (x > right - margin) {
    // 偏右 / 贴右缘：向右滑，把时刻放到右侧留白内
    next = Math.min(contentW - viewW, x - viewW + margin);
  } else {
    return false;
  }

  if (Math.abs(next - left) < 1) return false;
  scrollEl.scrollLeft = next;
  return true;
}

/**
 * 让时间区间（浅蓝选区）尽量落在视口内；区间比视口宽时优先保证入点可见。
 */
export function scrollRangeIntoView(
  scrollEl: HTMLElement | null,
  start: number,
  end: number,
  duration: number,
  options?: { marginRatio?: number },
): boolean {
  if (!scrollEl || !duration) return false;
  const s = Math.max(0, Math.min(duration, Math.min(start, end)));
  const e = Math.max(0, Math.min(duration, Math.max(start, end)));
  if (e - s < 0.001) {
    return scrollTimeIntoView(scrollEl, s, duration, options);
  }

  const contentW = scrollEl.scrollWidth;
  const viewW = scrollEl.clientWidth;
  if (contentW <= viewW + 1) return false;

  const x0 = (s / duration) * contentW;
  const x1 = (e / duration) * contentW;
  const left = scrollEl.scrollLeft;
  const right = left + viewW;
  const margin = Math.max(24, viewW * (options?.marginRatio ?? 0.08));

  if (x0 >= left + margin && x1 <= right - margin) return false;

  const rangeW = x1 - x0;
  let next = left;
  if (rangeW + margin * 2 <= viewW) {
    if (x0 < left + margin) {
      next = Math.max(0, x0 - margin);
    } else if (x1 > right - margin) {
      next = Math.min(contentW - viewW, x1 - viewW + margin);
    }
  } else if (x0 < left + margin || x0 > right - margin) {
    next = Math.max(0, Math.min(contentW - viewW, x0 - margin));
  } else {
    return false;
  }

  if (Math.abs(next - left) < 1) return false;
  scrollEl.scrollLeft = next;
  return true;
}

export function timeAtClientX(
  scrollEl: HTMLElement,
  clientX: number,
  duration: number,
): number {
  const rect = scrollEl.getBoundingClientRect();
  const xInView = clientX - rect.left;
  const contentX = scrollEl.scrollLeft + xInView;
  const pct = contentX / Math.max(1, scrollEl.scrollWidth);
  return roundTime(Math.max(0, Math.min(duration, pct * duration)));
}

/** 工具栏文案：显示当前视口大约覆盖多长时间 */
export function formatViewLabel(viewSeconds: number, duration: number): string {
  const v = clampViewSeconds(viewSeconds, duration);
  if (duration > 0 && v >= duration - 0.05) return "全片";
  if (v < 60) return `${Math.round(v)}秒`;
  const m = Math.floor(v / 60);
  const s = Math.round(v % 60);
  if (s === 0) return `${m}分钟`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export { DEFAULT_VIEW_SECONDS, MIN_VIEW_SECONDS, VIEW_ZOOM_FACTOR };
