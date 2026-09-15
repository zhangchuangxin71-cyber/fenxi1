import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { ScenePreviewState } from "../api/types";
import {
  sceneExclusiveEnd,
  sceneHoldTime,
  sceneTriggerTime,
} from "../utils/scenePreview";
import { roundTime } from "../utils/format";

export interface SceneSegmentLike {
  start_time: number;
  end_time: number;
  start_frame?: number | null;
  end_frame?: number | null;
}

interface UseVideoPlayerOptions {
  videoRef: RefObject<HTMLVideoElement | null>;
  freezeCanvasRef: RefObject<HTMLCanvasElement | null>;
  duration: number;
  fps: number;
}

export function useVideoPlayer({
  videoRef,
  freezeCanvasRef,
  duration,
  fps,
}: UseVideoPlayerOptions) {
  const [currentTime, setCurrentTime] = useState(0);
  const [loopPreview, setLoopPreview] = useState(false);
  const [mergePreviewActive, setMergePreviewActive] = useState(false);

  const scenePreviewRef = useRef<ScenePreviewState | null>(null);
  /** 仅在 playSceneSegment 播到刹停期间为 true；结束后不锁原片播放 */
  const scenePreviewArmedRef = useRef(false);
  const loopSelectionRef = useRef<{
    start: number;
    end: number;
    mode: "loop" | "once";
  } | null>(null);
  const mergeQueueRef = useRef<SceneSegmentLike[] | null>(null);
  const mergeQueueIndexRef = useRef(0);
  const mergeOnSegmentRef = useRef<((index: number) => void) | null>(null);
  const mergeOnCompleteRef = useRef<(() => void) | null>(null);
  const armAndPlaySegmentRef = useRef<(seg: SceneSegmentLike) => void>(() => {});
  const previewRafRef = useRef<number | null>(null);
  const previewUsesRvfcRef = useRef(false);
  const freezeSeekHandlerRef = useRef<(() => void) | null>(null);
  const internalSeekRef = useRef(false);
  const selectionMonitorRafRef = useRef<number | null>(null);

  const frameDuration = useCallback(() => 1 / (fps || 25), [fps]);

  const getPlayer = useCallback(() => videoRef.current, [videoRef]);

  const clearPreviewFreeze = useCallback(() => {
    const player = getPlayer();
    const freeze = freezeCanvasRef.current;
    if (!freeze) return;

    if (freezeSeekHandlerRef.current && player) {
      player.removeEventListener("seeked", freezeSeekHandlerRef.current);
      freezeSeekHandlerRef.current = null;
    }

    freeze.classList.add("hidden");
    const ctx = freeze.getContext("2d");
    if (ctx && freeze.width) {
      ctx.clearRect(0, 0, freeze.width, freeze.height);
    }
    if (player) player.style.visibility = "";
  }, [freezeCanvasRef, getPlayer]);

  const capturePreviewFreeze = useCallback(() => {
    const player = getPlayer();
    const freeze = freezeCanvasRef.current;
    if (!freeze || !player?.videoWidth) return false;

    freeze.width = player.videoWidth;
    freeze.height = player.videoHeight;
    const ctx = freeze.getContext("2d");
    if (!ctx) return false;
    ctx.drawImage(player, 0, 0, freeze.width, freeze.height);
    freeze.classList.remove("hidden");
    return true;
  }, [freezeCanvasRef, getPlayer]);

  const stopPreviewGuard = useCallback(() => {
    const player = getPlayer();
    if (previewRafRef.current != null) {
      if (
        previewUsesRvfcRef.current &&
        player &&
        typeof player.cancelVideoFrameCallback === "function"
      ) {
        try {
          player.cancelVideoFrameCallback(previewRafRef.current);
        } catch {
          cancelAnimationFrame(previewRafRef.current);
        }
      } else {
        cancelAnimationFrame(previewRafRef.current);
      }
      previewRafRef.current = null;
    }
    previewUsesRvfcRef.current = false;
  }, [getPlayer]);

  const releaseScenePreview = useCallback(() => {
    scenePreviewArmedRef.current = false;
    scenePreviewRef.current = null;
    stopPreviewGuard();
  }, [stopPreviewGuard]);

  const clearMergeQueue = useCallback(() => {
    mergeQueueRef.current = null;
    mergeQueueIndexRef.current = 0;
    mergeOnSegmentRef.current = null;
    mergeOnCompleteRef.current = null;
    setMergePreviewActive(false);
  }, []);

  const finishMergePreview = useCallback(() => {
    const onComplete = mergeOnCompleteRef.current;
    clearMergeQueue();
    onComplete?.();
  }, [clearMergeQueue]);

  const buildScenePreview = useCallback(
    (seg: SceneSegmentLike): ScenePreviewState => {
      const start = seg.start_time;
      const end = seg.end_time;
      const exclusiveEnd = sceneExclusiveEnd(end, seg.end_frame, fps);
      const holdAt = sceneHoldTime(
        start,
        end,
        seg.start_frame,
        seg.end_frame,
        fps,
      );
      const triggerAt = sceneTriggerTime(
        start,
        end,
        seg.start_frame,
        seg.end_frame,
        fps,
      );
      return { start, end, exclusiveEnd, holdAt, triggerAt };
    },
    [fps],
  );

  const clampScenePreview = useCallback(() => {
    const player = getPlayer();
    const preview = scenePreviewRef.current;
    if (!player || !preview || !scenePreviewArmedRef.current) return;

    const mergeQueue = mergeQueueRef.current;
    const mergeIdx = mergeQueueIndexRef.current;

    scenePreviewArmedRef.current = false;
    scenePreviewRef.current = null;
    stopPreviewGuard();

    if (mergeQueue && mergeIdx + 1 < mergeQueue.length) {
      const nextIdx = mergeIdx + 1;
      mergeQueueIndexRef.current = nextIdx;
      mergeOnSegmentRef.current?.(nextIdx);
      armAndPlaySegmentRef.current(mergeQueue[nextIdx]);
      return;
    }

    if (mergeQueue) {
      finishMergePreview();
    }

    const { start, holdAt, exclusiveEnd } = preview;
    const fd = frameDuration();
    const dangerAt = exclusiveEnd - 1.5 * fd;
    const safeHold = Math.max(start, Math.min(holdAt, exclusiveEnd - 2.5 * fd));

    // 本轮分镜预览结束：先解除锁定，避免用户再点原片播放时被反复刹停
    player.pause();

    if (player.currentTime < dangerAt) {
      capturePreviewFreeze();
      setCurrentTime(player.currentTime);
      return;
    }

    player.style.visibility = "hidden";
    if (freezeSeekHandlerRef.current) {
      player.removeEventListener("seeked", freezeSeekHandlerRef.current);
    }

    const onSeeked = () => {
      freezeSeekHandlerRef.current = null;
      internalSeekRef.current = false;
      capturePreviewFreeze();
      player.style.visibility = "";
      setCurrentTime(player.currentTime);
    };
    freezeSeekHandlerRef.current = onSeeked;
    player.addEventListener("seeked", onSeeked, { once: true });
    internalSeekRef.current = true;
    player.currentTime = safeHold;
    setCurrentTime(safeHold);
  }, [
    capturePreviewFreeze,
    finishMergePreview,
    frameDuration,
    getPlayer,
    stopPreviewGuard,
  ]);

  const schedulePreviewTick = useCallback(
    (tick: (mediaTime: number) => void) => {
      const player = getPlayer();
      if (!player) return;

      if (typeof player.requestVideoFrameCallback === "function") {
        previewUsesRvfcRef.current = true;
        previewRafRef.current = player.requestVideoFrameCallback((_now, meta) => {
          tick(meta?.mediaTime ?? player.currentTime);
        });
      } else {
        previewUsesRvfcRef.current = false;
        previewRafRef.current = requestAnimationFrame(() => {
          tick(player.currentTime);
        });
      }
    },
    [getPlayer],
  );

  /** 分镜限播守护（不含手动选区循环；选区由 timeupdate 处理） */
  const startPreviewGuard = useCallback(() => {
    stopPreviewGuard();
    clearPreviewFreeze();

    const tick = (mediaTime: number) => {
      previewRafRef.current = null;
      const player = getPlayer();
      if (!player || !duration) return;

      const preview = scenePreviewRef.current;
      if (preview && scenePreviewArmedRef.current && !player.paused) {
        const fd = frameDuration();
        if (
          mediaTime >= preview.triggerAt ||
          mediaTime >= preview.exclusiveEnd - fd * 0.25
        ) {
          clampScenePreview();
          return;
        }
        schedulePreviewTick(tick);
      }
    };

    schedulePreviewTick(tick);
  }, [
    clearPreviewFreeze,
    clampScenePreview,
    duration,
    frameDuration,
    getPlayer,
    schedulePreviewTick,
    stopPreviewGuard,
  ]);

  const applySelectionBoundary = useCallback(
    (mediaTime: number) => {
      const player = getPlayer();
      const loopSel = loopSelectionRef.current;
      if (!player || !loopSel || !duration) return;
      if (internalSeekRef.current || player.paused) return;

      const fd = 1 / (fps || 25);
      const triggerAt = Math.max(loopSel.start, loopSel.end - 0.5 * fd);
      if (mediaTime < triggerAt) return;

      if (loopSel.mode === "once") {
        const endT = roundTime(Math.min(loopSel.end, duration));
        loopSelectionRef.current = null;
        internalSeekRef.current = true;
        player.pause();
        player.currentTime = endT;
        setCurrentTime(endT);
        window.setTimeout(() => {
          internalSeekRef.current = false;
        }, 50);
        return;
      }

      // loop：折回入点继续播
      internalSeekRef.current = true;
      player.currentTime = loopSel.start;
      setCurrentTime(loopSel.start);
      const release = () => {
        internalSeekRef.current = false;
        if (loopSelectionRef.current?.mode === "loop" && player.paused) {
          player.play().catch(() => {});
        }
      };
      const onSeeked = () => {
        player.removeEventListener("seeked", onSeeked);
        window.clearTimeout(fallback);
        release();
      };
      player.addEventListener("seeked", onSeeked);
      const fallback = window.setTimeout(() => {
        player.removeEventListener("seeked", onSeeked);
        release();
      }, 300);
    },
    [duration, fps, getPlayer],
  );

  const stopSelectionMonitor = useCallback(() => {
    if (selectionMonitorRafRef.current != null) {
      cancelAnimationFrame(selectionMonitorRafRef.current);
      selectionMonitorRafRef.current = null;
    }
  }, []);

  const startSelectionMonitor = useCallback(() => {
    stopSelectionMonitor();
    const tick = () => {
      selectionMonitorRafRef.current = null;
      const player = getPlayer();
      if (!player || !loopSelectionRef.current) return;
      applySelectionBoundary(player.currentTime);
      if (loopSelectionRef.current) {
        selectionMonitorRafRef.current = requestAnimationFrame(tick);
      }
    };
    selectionMonitorRafRef.current = requestAnimationFrame(tick);
  }, [applySelectionBoundary, getPlayer, stopSelectionMonitor]);

  const setScenePreviewFromSegment = useCallback(
    (seg: SceneSegmentLike | null) => {
      // 仅同步区间信息，不自动开限播；真正限播由 playSceneSegment 开启
      if (!seg) {
        releaseScenePreview();
        return;
      }
      scenePreviewRef.current = buildScenePreview(seg);
    },
    [buildScenePreview, releaseScenePreview],
  );

  const seekPlayer = useCallback(
    (time: number, pause = true) => {
      const player = getPlayer();
      if (!player || !duration) return;
      // 用户主动seek时退出分镜限播，避免原片被拽回刹停点
      clearMergeQueue();
      releaseScenePreview();
      const t = roundTime(Math.max(0, Math.min(time, duration)));
      if (pause) player.pause();
      player.currentTime = t;
      setCurrentTime(t);
    },
    [clearMergeQueue, duration, getPlayer, releaseScenePreview],
  );

  const armAndPlaySegment = useCallback(
    (seg: SceneSegmentLike) => {
      const player = getPlayer();
      if (!seg || !player || !duration) return;

      setLoopPreview(false);
      loopSelectionRef.current = null;
      clearPreviewFreeze();

      const preview = buildScenePreview(seg);
      scenePreviewRef.current = preview;
      scenePreviewArmedRef.current = true;

      const fd = frameDuration();
      const safeStart = Math.min(
        seg.start_time + 0.001,
        Math.max(0, preview.exclusiveEnd - 3 * fd),
      );
      const t = Math.max(0, Math.min(safeStart, duration));
      internalSeekRef.current = true;
      player.currentTime = t;
      setCurrentTime(t);

      player.play().catch(() => {});
      startPreviewGuard();
      window.setTimeout(() => {
        internalSeekRef.current = false;
      }, 0);
    },
    [
      buildScenePreview,
      clearPreviewFreeze,
      duration,
      frameDuration,
      getPlayer,
      startPreviewGuard,
    ],
  );

  armAndPlaySegmentRef.current = armAndPlaySegment;

  const playSceneSegment = useCallback(
    (seg: SceneSegmentLike) => {
      clearMergeQueue();
      armAndPlaySegment(seg);
    },
    [armAndPlaySegment, clearMergeQueue],
  );

  const playMergedScenePreview = useCallback(
    (
      segments: SceneSegmentLike[],
      callbacks?: {
        onSegmentChange?: (index: number) => void;
        onComplete?: () => void;
      },
    ) => {
      if (!segments.length) return;
      clearMergeQueue();
      mergeQueueRef.current = segments;
      mergeQueueIndexRef.current = 0;
      mergeOnSegmentRef.current = callbacks?.onSegmentChange ?? null;
      mergeOnCompleteRef.current = callbacks?.onComplete ?? null;
      setMergePreviewActive(true);
      callbacks?.onSegmentChange?.(0);
      armAndPlaySegment(segments[0]);
    },
    [armAndPlaySegment, clearMergeQueue],
  );

  const stopMergedScenePreview = useCallback(() => {
    clearMergeQueue();
    releaseScenePreview();
    clearPreviewFreeze();
    getPlayer()?.pause();
  }, [clearMergeQueue, clearPreviewFreeze, getPlayer, releaseScenePreview]);

  const startLoopPreview = useCallback(
    (start: number, end: number) => {
      const player = getPlayer();
      if (!player || !duration) return;

      scenePreviewRef.current = null;
      scenePreviewArmedRef.current = false;
      stopPreviewGuard();
      loopSelectionRef.current = { start, end, mode: "loop" };
      setLoopPreview(true);
      internalSeekRef.current = true;
      player.currentTime = start;
      setCurrentTime(start);
      const onSeeked = () => {
        player.removeEventListener("seeked", onSeeked);
        window.clearTimeout(fallback);
        internalSeekRef.current = false;
        player.play().catch(() => {});
        startSelectionMonitor();
      };
      player.addEventListener("seeked", onSeeked);
      const fallback = window.setTimeout(() => {
        player.removeEventListener("seeked", onSeeked);
        internalSeekRef.current = false;
        player.play().catch(() => {});
        startSelectionMonitor();
      }, 300);
    },
    [duration, getPlayer, startSelectionMonitor, stopPreviewGuard],
  );

  /** 从入点播到出点后暂停（不循环） */
  const playSelectionOnce = useCallback(
    (start: number, end: number) => {
      const player = getPlayer();
      if (!player || !duration) return;

      scenePreviewRef.current = null;
      scenePreviewArmedRef.current = false;
      stopPreviewGuard();
      loopSelectionRef.current = { start, end, mode: "once" };
      setLoopPreview(false);
      internalSeekRef.current = true;
      player.currentTime = start;
      setCurrentTime(start);
      const onSeeked = () => {
        player.removeEventListener("seeked", onSeeked);
        window.clearTimeout(fallback);
        internalSeekRef.current = false;
        player.play().catch(() => {});
        startSelectionMonitor();
      };
      player.addEventListener("seeked", onSeeked);
      const fallback = window.setTimeout(() => {
        player.removeEventListener("seeked", onSeeked);
        internalSeekRef.current = false;
        player.play().catch(() => {});
        startSelectionMonitor();
      }, 300);
    },
    [duration, getPlayer, startSelectionMonitor, stopPreviewGuard],
  );

  const stopLoopPreview = useCallback(() => {
    loopSelectionRef.current = null;
    setLoopPreview(false);
    stopSelectionMonitor();
  }, [stopSelectionMonitor]);

  const stepFrame = useCallback(
    (delta: number) => {
      const player = getPlayer();
      if (!player || !duration) return;
      const step = 1 / (fps || 25);
      player.pause();
      const t = roundTime(
        Math.max(0, Math.min(duration, player.currentTime + delta * step)),
      );
      player.currentTime = t;
      setCurrentTime(t);
    },
    [duration, fps, getPlayer],
  );

  const resetPlayer = useCallback(() => {
    clearMergeQueue();
    releaseScenePreview();
    clearPreviewFreeze();
    loopSelectionRef.current = null;
    setLoopPreview(false);
    setCurrentTime(0);
  }, [clearMergeQueue, clearPreviewFreeze, releaseScenePreview]);

  useEffect(() => {
    const player = getPlayer();
    if (!player) return;

    const onTimeUpdate = () => {
      if (!duration) return;
      const t = player.currentTime;
      setCurrentTime(t);

      // 手动选区：循环 / 播一次到出点
      applySelectionBoundary(t);

      if (
        scenePreviewArmedRef.current &&
        scenePreviewRef.current &&
        !player.paused
      ) {
        const preview = scenePreviewRef.current;
        const fd = frameDuration();
        if (
          t >= preview.triggerAt ||
          t >= preview.exclusiveEnd - fd * 0.25
        ) {
          clampScenePreview();
        }
      }
    };

    const onPlay = () => {
      clearPreviewFreeze();
      if (loopSelectionRef.current) {
        startSelectionMonitor();
        return;
      }
      if (scenePreviewArmedRef.current && scenePreviewRef.current) {
        startPreviewGuard();
        return;
      }
      releaseScenePreview();
    };

    const onPause = () => {
      if (internalSeekRef.current) return;
      stopPreviewGuard();
      // 选区循环仍保留 monitor：折返 seek 的 pause 时序不稳定，停了就折不回去
    };

    const onEnded = () => {
      const loopSel = loopSelectionRef.current;
      if (loopSel?.mode === "loop") {
        internalSeekRef.current = true;
        player.currentTime = loopSel.start;
        setCurrentTime(loopSel.start);
        const onSeeked = () => {
          player.removeEventListener("seeked", onSeeked);
          window.clearTimeout(fallback);
          internalSeekRef.current = false;
          player.play().catch(() => {});
        };
        player.addEventListener("seeked", onSeeked);
        const fallback = window.setTimeout(() => {
          player.removeEventListener("seeked", onSeeked);
          internalSeekRef.current = false;
          player.play().catch(() => {});
        }, 300);
        return;
      }
      if (loopSel?.mode === "once") {
        loopSelectionRef.current = null;
      }
      stopPreviewGuard();
      releaseScenePreview();
    };

    const onSeeking = () => {
      // 用户拖进度条：退出分镜限播，不再强制拉回段尾
      if (internalSeekRef.current) return;
      if (mergeQueueRef.current) {
        clearMergeQueue();
      }
      if (scenePreviewArmedRef.current) {
        releaseScenePreview();
        clearPreviewFreeze();
      }
    };

    player.addEventListener("timeupdate", onTimeUpdate);
    player.addEventListener("play", onPlay);
    player.addEventListener("pause", onPause);
    player.addEventListener("ended", onEnded);
    player.addEventListener("seeking", onSeeking);

    return () => {
      player.removeEventListener("timeupdate", onTimeUpdate);
      player.removeEventListener("play", onPlay);
      player.removeEventListener("pause", onPause);
      player.removeEventListener("ended", onEnded);
      player.removeEventListener("seeking", onSeeking);
    };
  }, [
    applySelectionBoundary,
    clampScenePreview,
    clearMergeQueue,
    clearPreviewFreeze,
    duration,
    frameDuration,
    getPlayer,
    releaseScenePreview,
    startPreviewGuard,
    startSelectionMonitor,
    stopPreviewGuard,
    videoRef,
  ]);

  return {
    currentTime,
    loopPreview,
    mergePreviewActive,
    seekPlayer,
    playSceneSegment,
    playMergedScenePreview,
    stopMergedScenePreview,
    startLoopPreview,
    playSelectionOnce,
    stopLoopPreview,
    stepFrame,
    clearPreviewFreeze,
    capturePreviewFreeze,
    setScenePreviewFromSegment,
    resetPlayer,
    stopPreviewGuard,
  };
}

export type VideoPlayerApi = ReturnType<typeof useVideoPlayer>;
