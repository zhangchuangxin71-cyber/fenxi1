import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";

export interface UseSplitPaneOptions {
  /** 右侧栏初始宽度（px） */
  initialAsideWidth?: number;
  minAsideWidth?: number;
  maxAsideWidth?: number;
  minMainWidth?: number;
  storageKey?: string;
}

/**
 * 左右分栏：拖拽分隔条调整右侧宽度，左侧吃剩余空间。
 */
export function useSplitPane(
  containerRef: RefObject<HTMLElement | null>,
  {
    initialAsideWidth = 300,
    minAsideWidth = 220,
    maxAsideWidth = 560,
    minMainWidth = 360,
    storageKey,
  }: UseSplitPaneOptions = {},
) {
  const [asideWidth, setAsideWidth] = useState(() => {
    if (storageKey && typeof window !== "undefined") {
      const raw = window.localStorage.getItem(storageKey);
      const n = raw ? Number(raw) : NaN;
      if (Number.isFinite(n) && n >= minAsideWidth && n <= maxAsideWidth) {
        return n;
      }
    }
    return initialAsideWidth;
  });

  const draggingRef = useRef(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!storageKey) return;
    try {
      window.localStorage.setItem(storageKey, String(Math.round(asideWidth)));
    } catch {
      /* ignore */
    }
  }, [asideWidth, storageKey]);

  const clampAside = useCallback(
    (next: number, containerWidth: number) => {
      const maxByMain = Math.max(minAsideWidth, containerWidth - minMainWidth);
      const maxAllowed = Math.min(maxAsideWidth, maxByMain);
      return Math.max(minAsideWidth, Math.min(maxAllowed, next));
    },
    [maxAsideWidth, minAsideWidth, minMainWidth],
  );

  const onSplitterPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      draggingRef.current = true;
      setDragging(true);
      e.currentTarget.setPointerCapture(e.pointerId);
      document.body.classList.add("is-split-dragging");
    },
    [],
  );

  const onSplitterPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (!draggingRef.current) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      setAsideWidth(clampAside(rect.right - e.clientX, rect.width));
    },
    [clampAside, containerRef],
  );

  const endSplitterDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    document.body.classList.remove("is-split-dragging");
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  }, []);

  return {
    asideWidth,
    setAsideWidth,
    dragging,
    splitterProps: {
      onPointerDown: onSplitterPointerDown,
      onPointerMove: onSplitterPointerMove,
      onPointerUp: endSplitterDrag,
      onPointerCancel: endSplitterDrag,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-valuenow": Math.round(asideWidth),
      tabIndex: 0,
    },
  };
}

export interface UseLeftPaneSplitOptions {
  /** 左侧栏初始宽度（px） */
  initialLeftWidth?: number;
  minLeftWidth?: number;
  minRightWidth?: number;
  maxLeftWidth?: number;
  storageKey?: string;
  /** 分隔条自身占位，计算时从容器宽度里扣掉 */
  splitterSize?: number;
}

/**
 * 左右分栏：拖拽分隔条直接调整左侧宽度（剪映式：拖的是左侧面板）。
 * 右侧吃剩余空间；左侧可继续拉宽，超出由外层 overflow 出滚动条。
 */
export function useLeftPaneSplit(
  containerRef: RefObject<HTMLElement | null>,
  {
    initialLeftWidth = 640,
    minLeftWidth = 280,
    minRightWidth = 240,
    maxLeftWidth = 2400,
    storageKey,
    splitterSize = 10,
  }: UseLeftPaneSplitOptions = {},
) {
  const [leftWidth, setLeftWidth] = useState(() => {
    if (storageKey && typeof window !== "undefined") {
      const raw = window.localStorage.getItem(storageKey);
      const n = raw ? Number(raw) : NaN;
      if (Number.isFinite(n) && n >= minLeftWidth) return n;
    }
    return initialLeftWidth;
  });

  const draggingRef = useRef(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!storageKey) return;
    try {
      window.localStorage.setItem(storageKey, String(Math.round(leftWidth)));
    } catch {
      /* ignore */
    }
  }, [leftWidth, storageKey]);

  const clampLeft = useCallback(
    (next: number, containerWidth: number) => {
      const maxByRight = Math.max(
        minLeftWidth,
        containerWidth - minRightWidth - splitterSize,
      );
      // 允许略超过可视宽度，由 overflow:auto 出横向滚动（剪映式撑开）
      const maxAllowed = Math.max(maxByRight, maxLeftWidth);
      return Math.max(minLeftWidth, Math.min(maxAllowed, next));
    },
    [maxLeftWidth, minLeftWidth, minRightWidth, splitterSize],
  );

  const onSplitterPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      draggingRef.current = true;
      setDragging(true);
      e.currentTarget.setPointerCapture(e.pointerId);
      document.body.classList.add("is-split-dragging");
    },
    [],
  );

  const onSplitterPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (!draggingRef.current) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const padLeft = Number.parseFloat(style.paddingLeft) || 0;
      const padRight = Number.parseFloat(style.paddingRight) || 0;
      const contentWidth = rect.width - padLeft - padRight;
      const raw = e.clientX - rect.left - padLeft + el.scrollLeft;
      setLeftWidth(clampLeft(raw, contentWidth));
    },
    [clampLeft, containerRef],
  );

  const endSplitterDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    document.body.classList.remove("is-split-dragging");
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  }, []);

  return {
    leftWidth,
    setLeftWidth,
    dragging,
    splitterProps: {
      onPointerDown: onSplitterPointerDown,
      onPointerMove: onSplitterPointerMove,
      onPointerUp: endSplitterDrag,
      onPointerCancel: endSplitterDrag,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-valuenow": Math.round(leftWidth),
      tabIndex: 0,
    },
  };
}

export interface UseRowSplitOptions {
  /** 上方面板初始高度（px） */
  initialTopHeight?: number;
  minTopHeight?: number;
  minBottomHeight?: number;
  storageKey?: string;
}

/**
 * 上下分栏：拖拽水平分隔条调整上方高度（下方吃剩余 / 或固定最小高度）。
 * 当上下最小高度之和超过容器时，由外层 overflow:auto 出滚动条。
 */
export function useRowSplit(
  containerRef: RefObject<HTMLElement | null>,
  {
    initialTopHeight = 360,
    minTopHeight = 160,
    storageKey,
  }: UseRowSplitOptions = {},
) {
  const [topHeight, setTopHeight] = useState(() => {
    if (storageKey && typeof window !== "undefined") {
      const raw = window.localStorage.getItem(storageKey);
      const n = raw ? Number(raw) : NaN;
      if (Number.isFinite(n) && n >= minTopHeight) return n;
    }
    return initialTopHeight;
  });

  const draggingRef = useRef(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!storageKey) return;
    try {
      window.localStorage.setItem(storageKey, String(Math.round(topHeight)));
    } catch {
      /* ignore */
    }
  }, [topHeight, storageKey]);

  const onSplitterPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      draggingRef.current = true;
      setDragging(true);
      e.currentTarget.setPointerCapture(e.pointerId);
      document.body.classList.add("is-row-split-dragging");
    },
    [],
  );

  const onSplitterPointerMove = useCallback(
    (e: ReactPointerEvent<HTMLElement>) => {
      if (!draggingRef.current) return;
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      // 相对内容顶部（含 scrollTop），不钳到可视高度；超出由 overflow:auto 出滚动条
      const next = Math.max(
        minTopHeight,
        e.clientY - rect.top + el.scrollTop,
      );
      setTopHeight(next);
    },
    [containerRef, minTopHeight],
  );

  const endSplitterDrag = useCallback((e: ReactPointerEvent<HTMLElement>) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    setDragging(false);
    document.body.classList.remove("is-row-split-dragging");
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  }, []);

  return {
    topHeight,
    setTopHeight,
    dragging,
    splitterProps: {
      onPointerDown: onSplitterPointerDown,
      onPointerMove: onSplitterPointerMove,
      onPointerUp: endSplitterDrag,
      onPointerCancel: endSplitterDrag,
      role: "separator" as const,
      "aria-orientation": "horizontal" as const,
      "aria-valuenow": Math.round(topHeight),
      tabIndex: 0,
    },
  };
}
