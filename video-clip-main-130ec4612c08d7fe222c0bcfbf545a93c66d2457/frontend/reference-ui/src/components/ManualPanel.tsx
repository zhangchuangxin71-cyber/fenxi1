import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { SegmentResponse, TaskResponse, TimeRange } from "../api/types";
import { createManualTask, getTaskStatus, publishSegments } from "../api/client";
import { Timeline } from "./Timeline";
import { PlayerSelectionOverlay } from "./PlayerSelectionOverlay";
import {
  formatClockMs,
  formatSmpte,
  formatTime,
  parseTimeInput,
  roundTime,
  timeToFrame,
} from "../utils/format";
import type { VideoPlayerApi } from "../hooks/useVideoPlayer";
import { useUndoableState } from "../hooks/useUndoableState";
import { ClipPreviewModal } from "./ClipPreviewModal";
import { OutputSegCover } from "./OutputSegCover";
import {
  canPreviewSegment,
  prefetchSegmentThumbs,
  segmentPreviewUrl,
  segmentThumbUrl,
} from "../utils/segmentMedia";

export interface ManualPanelHandle {
  handleKey: (e: KeyboardEvent | {
    key: string;
    ctrlKey?: boolean;
    metaKey?: boolean;
    shiftKey?: boolean;
  }) => void;
}

interface ManualSeg extends TimeRange {
  deleted?: boolean;
}

interface Props {
  videoId: string;
  duration: number;
  fps?: number;
  previewUrl?: string | null;
  sourceFilename?: string | null;
  /** 源片宽高比（宽/高），输出预览按真实横竖屏展示 */
  aspectRatio?: number | null;
  playerRef: RefObject<HTMLVideoElement | null>;
  player: VideoPlayerApi;
  toast: (msg: string, isError?: boolean) => void;
  active: boolean;
  resultMountId?: string;
  timelineMountId?: string;
  playerMountId?: string;
  onSelectionChange?: (sel: {
    start: number | null;
    end: number | null;
  }) => void;
  onStepChange?: (step: number) => void;
  onTaskChange?: (
    task: TaskResponse | null,
    opts?: { enterExecute?: boolean },
  ) => void;
  /** 从「执行切割」返回编辑时递增，用于恢复预览列表、退出结果态 */
  editResumeKey?: number;
}

function hasSelection(sel: { start: number | null; end: number | null }): boolean {
  return sel.start != null && sel.end != null && sel.start < sel.end;
}

function hasPendingInPoint(sel: {
  start: number | null;
  end: number | null;
}): boolean {
  return sel.start != null && sel.end == null;
}

export const ManualPanel = forwardRef<ManualPanelHandle, Props>(
  function ManualPanel(
    {
      videoId,
      duration,
      fps = 25,
      previewUrl,
      sourceFilename,
      aspectRatio,
      playerRef,
      player,
      toast,
      active,
      resultMountId,
      timelineMountId,
      playerMountId = "player-stage",
      onSelectionChange,
      onStepChange,
      onTaskChange,
      editResumeKey = 0,
    },
    ref,
  ) {
    void playerRef;
    void sourceFilename;
    const activeRef = useRef(active);
    activeRef.current = active;
    const [publishing, setPublishing] = useState(false);
    const [selection, setSelection] = useState<{
      start: number | null;
      end: number | null;
    }>({ start: null, end: null });
    const {
      value: manualSegments,
      set: setManualSegments,
      reset: resetManualSegments,
      undo,
      redo,
    } = useUndoableState<ManualSeg[]>([]);
    const [selectedSegmentIndex, setSelectedSegmentIndex] = useState(-1);
    const [inputStart, setInputStart] = useState("");
    const [inputEnd, setInputEnd] = useState("");
    const [loopOn, setLoopOn] = useState(false);

    const [taskId, setTaskId] = useState<string | null>(null);
    const [resultPreview, setResultPreview] = useState<{
      title: string;
      url: string;
      range: string;
    } | null>(null);
    const [taskRunning, setTaskRunning] = useState(false);
    const [results, setResults] = useState<SegmentResponse[]>([]);
    const [showResults, setShowResults] = useState(false);
    const [checkedResults, setCheckedResults] = useState<Set<number>>(new Set());

    const pollTimerRef = useRef<number | null>(null);

    const stopPolling = useCallback(() => {
      if (pollTimerRef.current != null) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    }, []);

    useEffect(() => () => stopPolling(), [stopPolling]);

    // 离开编辑页时停轮询，避免与执行页双重刷新
    useEffect(() => {
      if (!active) stopPolling();
    }, [active, stopPolling]);

    useEffect(() => {
      setSelection({ start: null, end: null });
      resetManualSegments([]);
      setSelectedSegmentIndex(-1);
      setInputStart("");
      setInputEnd("");
      setLoopOn(false);
      setTaskId(null);
      setTaskRunning(false);
      setResults([]);
      setShowResults(false);
      setCheckedResults(new Set());
      stopPolling();
      onSelectionChange?.({ start: null, end: null });
    }, [videoId, stopPolling, resetManualSegments]); // eslint-disable-line react-hooks/exhaustive-deps

    const activeSegments = useMemo(
      () => manualSegments.filter((s) => !s.deleted),
      [manualSegments],
    );

    // 设计稿：0上传 1选模式 2预览编辑 3执行切割 4下载
    const workflowStep = showResults || taskRunning ? 3 : 2;

    useEffect(() => {
      // 仅在编辑页激活时同步步骤，避免隐藏态把流程又推回「执行切割」
      if (!active) return;
      onStepChange?.(workflowStep);
    }, [active, onStepChange, workflowStep]);

    // 从执行页返回 / 步骤回退：停轮询并退出切割中 UI
    useEffect(() => {
      if (!editResumeKey) return;
      stopPolling();
      setShowResults(false);
      setTaskRunning(false);
    }, [editResumeKey, stopPolling]);

    const applySelection = useCallback(
      (start: number, end: number, updatePlayer = false) => {
        let s = roundTime(Math.max(0, Math.min(start, duration)));
        let e = roundTime(Math.max(0, Math.min(end, duration)));
        if (s >= e) {
          toast("出点不能早于入点", true);
          return false;
        }
        const sel = { start: s, end: e };
        setSelection(sel);
        setInputStart(formatTime(s));
        setInputEnd(formatTime(e));
        onSelectionChange?.(sel);
        if (updatePlayer) player.seekPlayer(s);
        return true;
      },
      [duration, onSelectionChange, player, toast],
    );

    const commitPartialInPoint = useCallback(
      (time: number) => {
        const t = roundTime(Math.max(0, Math.min(time, duration)));
        if (selection.end != null && t >= selection.end) {
          toast("出点不能早于入点", true);
          return;
        }
        const sel = { start: t, end: null as number | null };
        setSelection(sel);
        setInputStart(formatTime(t));
        setInputEnd("");
        onSelectionChange?.(sel);
        player.seekPlayer(t);
      },
      [duration, onSelectionChange, player, selection.end, toast],
    );

    const setStartAtCurrent = useCallback(() => {
      const t = roundTime(player.currentTime);
      if (selection.end != null) {
        if (t >= selection.end) {
          toast("出点不能早于入点", true);
          return;
        }
        applySelection(t, selection.end);
        player.seekPlayer(t);
        return;
      }
      commitPartialInPoint(t);
    }, [
      applySelection,
      commitPartialInPoint,
      player,
      selection.end,
      toast,
    ]);

    const setEndAtCurrent = useCallback(() => {
      const t = roundTime(player.currentTime);
      if (selection.start == null) {
        toast("请先标记入点", true);
        return;
      }
      if (t <= selection.start) {
        toast("出点不能早于入点", true);
        return;
      }
      applySelection(selection.start, t);
      player.seekPlayer(t);
    }, [applySelection, player, selection.start, toast]);

    const clearSelection = useCallback(() => {
      setSelection({ start: null, end: null });
      setInputStart("");
      setInputEnd("");
      setSelectedSegmentIndex(-1);
      onSelectionChange?.({ start: null, end: null });
    }, [onSelectionChange]);

    const nudgeSelection = useCallback(
      (edge: "start" | "end" | "both", delta: number) => {
        if (!hasSelection(selection)) {
          toast("请先在时间轴拖选区间，或选中列表中的片段", true);
          return;
        }
        const minGap = 1 / (fps || 25);
        const s = selection.start!;
        const e = selection.end!;
        if (edge === "start") {
          const newStart = roundTime(
            Math.max(0, Math.min(s + delta, e - minGap)),
          );
          if (newStart >= e - minGap) return;
          // 始终跳到新入点，便于预览选区起点画面
          applySelection(newStart, e, true);
        } else if (edge === "end") {
          const newEnd = roundTime(
            Math.max(s + minGap, Math.min(e + delta, duration)),
          );
          if (newEnd <= s + minGap) return;
          applySelection(s, newEnd, false);
          // 出点微调后跳到新出点预览
          player.seekPlayer(newEnd, true);
        } else {
          const len = e - s;
          let ns = roundTime(s + delta);
          let ne = roundTime(e + delta);
          if (ns < 0) {
            ns = 0;
            ne = roundTime(len);
          }
          if (ne > duration) {
            ne = roundTime(duration);
            ns = roundTime(Math.max(0, duration - len));
          }
          applySelection(ns, ne, true);
        }
      },
      [applySelection, duration, fps, player, selection, toast],
    );

    /**
     * 已「选中微调」时：帧步进 / ±秒推动离播放头更近的入点或出点，
     * 并跳到该侧预览，便于接着点「更新片段」。
     * 未选中片段时：只移动播放头。
     */
    const nudgeNearerEdgeOrSeek = useCallback(
      (deltaSec: number) => {
        if (selectedSegmentIndex < 0 || !hasSelection(selection)) {
          return false;
        }
        const s = selection.start!;
        const e = selection.end!;
        const t = player.currentTime;
        const edge: "start" | "end" =
          Math.abs(t - s) <= Math.abs(t - e) ? "start" : "end";
        nudgeSelection(edge, deltaSec);
        return true;
      },
      [nudgeSelection, player, selectedSegmentIndex, selection],
    );

    const seekBySeconds = useCallback(
      (delta: number) => {
        if (nudgeNearerEdgeOrSeek(delta)) return;
        const t = roundTime(
          Math.max(0, Math.min(duration, player.currentTime + delta)),
        );
        player.seekPlayer(t, true);
      },
      [duration, nudgeNearerEdgeOrSeek, player],
    );

    const stepFrameOrEdge = useCallback(
      (frameDelta: number) => {
        const deltaSec = frameDelta / (fps || 25);
        if (nudgeNearerEdgeOrSeek(deltaSec)) return;
        player.stepFrame(frameDelta);
      },
      [fps, nudgeNearerEdgeOrSeek, player],
    );

    const toggleLoop = useCallback(() => {
      if (!hasSelection(selection)) {
        toast("请先选择片段区间", true);
        return;
      }
      if (loopOn) {
        player.stopLoopPreview();
        setLoopOn(false);
        toast("循环播放选区已关闭");
      } else {
        setLoopOn(true);
        player.startLoopPreview(selection.start!, selection.end!);
        toast("循环播放选区已开启");
      }
    }, [loopOn, player, selection, toast]);

    const playSelection = useCallback(() => {
      if (!hasSelection(selection)) {
        toast("请先选择片段区间", true);
        return;
      }
      const v = playerRef.current;
      // 已在选区内播放时再次点击 / Space → 暂停
      if (
        v &&
        !v.paused &&
        selection.start != null &&
        selection.end != null &&
        v.currentTime >= selection.start - 0.05 &&
        v.currentTime <= selection.end + 0.05
      ) {
        v.pause();
        return;
      }
      if (loopOn) {
        player.startLoopPreview(selection.start!, selection.end!);
      } else {
        player.playSelectionOnce(selection.start!, selection.end!);
      }
    }, [loopOn, player, playerRef, selection, toast]);

    const addCurrentSegment = useCallback(() => {
      if (!hasSelection(selection)) {
        toast("请先选择片段区间", true);
        return;
      }
      const { start, end } = selection;
      if (end! - start! < 0.1) {
        toast("片段太短，至少 0.1 秒", true);
        return;
      }
      setManualSegments((prev) => {
        const next = [...prev, { start: start!, end: end! }];
        next.sort((a, b) => a.start - b.start);
        return next;
      });
      clearSelection();
      player.stopLoopPreview();
      setLoopOn(false);
      setSelectedSegmentIndex(-1);
      toast(`已添加片段 ${activeSegments.length + 1}`);
    }, [
      activeSegments.length,
      clearSelection,
      player,
      selection,
      setManualSegments,
      toast,
    ]);

    const applyToCurrentSegment = useCallback(() => {
      if (selectedSegmentIndex < 0) {
        toast("请先在列表中点「选中微调」选中要改的片段", true);
        return;
      }
      // 优先用时间轴选区；若选区无效则尝试输入框
      let start = selection.start;
      let end = selection.end;
      if (start == null || end == null || start >= end) {
        const parsedStart = parseTimeInput(inputStart);
        const parsedEnd = parseTimeInput(inputEnd);
        if (
          parsedStart == null ||
          parsedEnd == null ||
          parsedStart >= parsedEnd
        ) {
          toast("请先选择有效片段区间（出点须晚于入点）", true);
          return;
        }
        start = parsedStart;
        end = parsedEnd;
        applySelection(start, end, true);
      }
      if (end! - start! < 0.1) {
        toast("片段太短，至少 0.1 秒", true);
        return;
      }
      const nextStart = start!;
      const nextEnd = end!;
      setManualSegments((prev) =>
        prev.map((seg, i) =>
          i === selectedSegmentIndex
            ? { ...seg, start: nextStart, end: nextEnd, deleted: false }
            : seg,
        ),
      );
      toast(`已更新片段 ${selectedSegmentIndex + 1}`);
    }, [
      applySelection,
      inputEnd,
      inputStart,
      selection.end,
      selection.start,
      selectedSegmentIndex,
      setManualSegments,
      toast,
    ]);

    const selectSavedSegment = useCallback(
      (index: number, autoPlay = false) => {
        const seg = manualSegments[index];
        if (!seg || seg.deleted) return;
        applySelection(seg.start, seg.end, true);
        setSelectedSegmentIndex(index);
        if (autoPlay) {
          player.startLoopPreview(seg.start, seg.end);
          setLoopOn(true);
        }
      },
      [applySelection, manualSegments, player],
    );

    const removeSegment = useCallback(
      (index: number) => {
        setManualSegments((prev) =>
          prev.map((seg, i) =>
            i === index ? { ...seg, deleted: true } : seg,
          ),
        );
        if (selectedSegmentIndex === index) {
          clearSelection();
        }
        setSelectedSegmentIndex(-1);
      },
      [clearSelection, selectedSegmentIndex, setManualSegments],
    );

    const clearSegmentList = useCallback(() => {
      if (manualSegments.length === 0) return;
      resetManualSegments([]);
      clearSelection();
      toast("已清空片段列表");
    }, [clearSelection, manualSegments.length, resetManualSegments, toast]);

    const doUndo = useCallback(() => {
      if (undo()) {
        setSelectedSegmentIndex(-1);
        toast("已撤销");
      }
    }, [toast, undo]);

    const doRedo = useCallback(() => {
      if (redo()) {
        setSelectedSegmentIndex(-1);
        toast("已重做");
      }
    }, [redo, toast]);

    const startManualCut = useCallback(async () => {
      if (!videoId || activeSegments.length === 0) {
        toast("请先添加至少一个片段", true);
        return;
      }
      try {
        const task = await createManualTask(
          videoId,
          activeSegments.map(({ start, end }) => ({ start, end })),
        );
        setTaskId(task.id);
        setTaskRunning(true);
        setShowResults(false);
        setResults([]);
        onTaskChange?.(task, { enterExecute: true });
        stopPolling();
        pollTimerRef.current = window.setInterval(async () => {
          try {
            const status = await getTaskStatus(task.id);
            onTaskChange?.(status);

            if (status.status === "done") {
              stopPolling();
              setTaskRunning(false);
              setResults(status.segments);
              setShowResults(!activeRef.current);
              setCheckedResults(new Set());
              prefetchSegmentThumbs(status.segments);
              toast("切割完成！可预览并保存片段");
            } else if (status.status === "failed") {
              stopPolling();
              setTaskRunning(false);
              toast(status.message || "任务失败", true);
            }
          } catch {
            stopPolling();
            setTaskRunning(false);
            toast("任务状态刷新失败，请稍后重试", true);
          }
        }, 1000);
        toast("手动切割任务已创建");
      } catch (e) {
        toast(e instanceof Error ? e.message : "创建任务失败", true);
      }
    }, [activeSegments, onTaskChange, stopPolling, toast, videoId]);

    const publishToLibrary = useCallback(async () => {
      if (!taskId || publishing || results.length === 0) return;
      const selectedIds = [...checkedResults]
        .sort((a, b) => a - b)
        .map((i) => results[i]?.id)
        .filter((id): id is string => Boolean(id));
      const ids =
        selectedIds.length > 0 && selectedIds.length < results.length
          ? selectedIds
          : undefined;
      setPublishing(true);
      try {
        const result = await publishSegments(taskId, {
          segmentIds: ids,
        });
        toast(`已返回 ${result.selected.length} 个选中切片 OSS 地址`);
        const t = await getTaskStatus(taskId);
        setResults(t.segments);
        onTaskChange?.(t);
      } catch (e) {
        toast(e instanceof Error ? e.message : "获取 OSS 地址失败", true);
      } finally {
        setPublishing(false);
      }
    }, [checkedResults, onTaskChange, publishing, results, taskId, toast]);

    const toggleResultCheck = useCallback((index: number) => {
      setCheckedResults((prev) => {
        const next = new Set(prev);
        if (next.has(index)) next.delete(index);
        else next.add(index);
        return next;
      });
    }, []);

    const toggleSelectAllResults = useCallback(() => {
      if (checkedResults.size >= results.length) {
        setCheckedResults(new Set());
        return;
      }
      setCheckedResults(new Set(results.map((_, i) => i)));
    }, [checkedResults.size, results]);

    useImperativeHandle(
      ref,
      () => ({
        handleKey: (e) => {
          const key = e.key;
          const mod = Boolean(e.ctrlKey || e.metaKey);
          if (mod && key.toLowerCase() === "z" && !e.shiftKey) {
            doUndo();
            return;
          }
          if (
            mod &&
            (key.toLowerCase() === "y" ||
              (key.toLowerCase() === "z" && e.shiftKey))
          ) {
            doRedo();
            return;
          }
          if (key === " " || key === "Spacebar") {
            playSelection();
            return;
          }
          if (key === "i" || key === "I") setStartAtCurrent();
          else if (key === "o" || key === "O") setEndAtCurrent();
          else if (key === "Enter") addCurrentSegment();
          else if (key === "l" || key === "L") toggleLoop();
          else if (key === "j" || key === "J") seekBySeconds(-1);
          else if (key === "k" || key === "K") seekBySeconds(1);
          else if (key === "," || key === "ArrowLeft") stepFrameOrEdge(-1);
          else if (key === "." || key === "ArrowRight") stepFrameOrEdge(1);
          else if (key === "Delete" || key === "Backspace") {
            if (selectedSegmentIndex >= 0) removeSegment(selectedSegmentIndex);
            else clearSelection();
          }
        },
      }),
      [
        addCurrentSegment,
        clearSelection,
        doRedo,
        doUndo,
        playSelection,
        player,
        removeSegment,
        seekBySeconds,
        selectedSegmentIndex,
        setEndAtCurrent,
        setStartAtCurrent,
        stepFrameOrEdge,
        toggleLoop,
      ],
    );

    const selectionActive = hasSelection(selection);
    const pendingInPoint = hasPendingInPoint(selection);
    const fineTuningSegment = selectedSegmentIndex >= 0 && selectionActive;
    const selectionDuration =
      selectionActive && selection.start != null && selection.end != null
        ? selection.end - selection.start
        : 0;
    const rate = fps || 25;
    const currentFrame = timeToFrame(player.currentTime, rate);
    const inFrame =
      selection.start != null ? timeToFrame(selection.start, rate) : null;
    const outFrame =
      selection.end != null ? timeToFrame(selection.end, rate) : null;
    const selectionFrames = selectionActive
      ? Math.max(0, Math.round(selectionDuration * rate))
      : 0;

    const selectedSeg =
      selectedSegmentIndex >= 0
        ? manualSegments[selectedSegmentIndex]
        : null;

    const segmentStatuses = useMemo(() => {
      type SegStatus = "pending" | "done" | "deleted" | "cutting" | "exported";
      let activeIdx = 0;
      return manualSegments.map((seg): SegStatus => {
        if (seg.deleted) return "deleted";
        if (showResults) {
          const ok = Boolean(results[activeIdx]);
          activeIdx += 1;
          return ok ? "exported" : "done";
        }
        if (taskRunning) {
          activeIdx += 1;
          return "cutting";
        }
        activeIdx += 1;
        return "pending";
      });
    }, [manualSegments, results, showResults, taskRunning]);

    const [resultMount, setResultMount] = useState<HTMLElement | null>(null);
    const [timelineMount, setTimelineMount] = useState<HTMLElement | null>(
      null,
    );
    useEffect(() => {
      if (!resultMountId) {
        setResultMount(null);
        return;
      }
      setResultMount(document.getElementById(resultMountId));
    }, [resultMountId, active, manualSegments.length, showResults, taskRunning]);

    useEffect(() => {
      if (!timelineMountId) {
        setTimelineMount(null);
        return;
      }
      setTimelineMount(document.getElementById(timelineMountId));
    }, [timelineMountId, active]);

    useEffect(() => {
      if (!resultMount) return;
      resultMount.classList.toggle("has-sidebar-content", active);
      return () => {
        resultMount.classList.remove("has-sidebar-content");
      };
    }, [resultMount, active]);

    const frameSec = 1 / (fps || 25);

    const timelineNode = (
      <Timeline
        videoId={videoId}
        duration={duration}
        currentTime={player.currentTime}
        selection={selection}
        savedSegments={manualSegments}
        selectedSegmentIndex={selectedSegmentIndex}
        segmentStatuses={segmentStatuses}
        onSeek={player.seekPlayer}
        onSelectionChange={applySelection}
        onSelectSegment={selectSavedSegment}
        showLegend
        enableMedia={active}
      />
    );

    const previewStart = selectedSeg?.start ?? selection.start;
    const previewEnd = selectedSeg?.end ?? selection.end;
    const previewDur = selectedSeg
      ? selectedSeg.end - selectedSeg.start
      : selectionDuration;

    const aside = (
      <div className="aside-stack manual-aside manual-aside-design">
        {(selectedSeg || selectionActive || pendingInPoint) && (
          <section className="aside-card selection-preview-card design compact">
            <div className="aside-card-title">当前选区</div>
            <div className="selection-preview-body design">
              <div
                className="selection-thumb"
                style={
                  aspectRatio && aspectRatio > 0
                    ? { aspectRatio: String(aspectRatio) }
                    : undefined
                }
              >
                {previewUrl ? (
                  <video
                    src={previewUrl}
                    muted
                    playsInline
                    preload="metadata"
                    onLoadedMetadata={(e) => {
                      if (previewStart != null) {
                        e.currentTarget.currentTime = Math.max(0, previewStart);
                      }
                    }}
                  />
                ) : (
                  <div className="selection-thumb-ph" />
                )}
                <span className="selection-thumb-badge">
                  {selectedSeg
                    ? `片段 ${String(selectedSegmentIndex + 1).padStart(2, "0")}`
                    : pendingInPoint
                      ? "已标入点"
                      : "仅预览（未入库）"}
                </span>
              </div>
              <div className="selection-preview-meta">
                <div className="selected-range design">
                  {formatTime(previewStart)}
                  {pendingInPoint ? " → 待标出点" : ` → ${formatTime(previewEnd)}`}
                </div>
                <div className="selected-duration design">
                  {pendingInPoint
                    ? "请继续标记出点 [O]"
                    : `时长 ${formatTime(previewDur)}`}
                </div>
                <button
                  type="button"
                  className="secondary linkish manual-loop-link"
                  disabled={pendingInPoint}
                  onClick={() => {
                    if (previewStart == null || previewEnd == null) return;
                    player.startLoopPreview(previewStart, previewEnd);
                    setLoopOn(true);
                  }}
                >
                  循环播放选区
                </button>
              </div>
            </div>
          </section>
        )}

        <section className="aside-card aside-card-flex manual-list-card design">
          <div className="aside-card-title-row">
            <div className="aside-card-title">片段列表</div>
            <span className="count-pill design">{activeSegments.length}</span>
          </div>
          {manualSegments.length === 0 ? (
            <p className="hint-text result-preview-empty">
              在时间轴拖选区间后点「+ 添加片段」
            </p>
          ) : (
            <div className="segment-list workbench-seg-list design">
              {manualSegments.map((seg, i) => {
                const status = segmentStatuses[i];
                return (
                  <div
                    key={`${seg.start}-${seg.end}-${i}`}
                    className={`seg-row clickable status-${status}${selectedSegmentIndex === i ? " selected" : ""}${seg.deleted ? " is-deleted" : ""}`}
                    onClick={() => {
                      if (!seg.deleted) selectSavedSegment(i);
                    }}
                  >
                    <div className="seg-index">
                      {String(i + 1).padStart(2, "0")}
                    </div>
                    <div className="seg-body">
                      <div className="seg-time">
                        {formatTime(seg.start)} → {formatTime(seg.end)}
                      </div>
                      <div className="seg-sub">
                        {formatTime(seg.end - seg.start)}
                      </div>
                    </div>
                    <div className="seg-actions">
                      {!seg.deleted && (
                        <>
                          {selectedSegmentIndex === i ? (
                            <button
                              type="button"
                              className="secondary btn-update-segment"
                              title="用当前时间轴选区覆盖此片段"
                              onClick={(e) => {
                                e.stopPropagation();
                                applyToCurrentSegment();
                              }}
                            >
                              更新片段
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="secondary"
                              onClick={(e) => {
                                e.stopPropagation();
                                selectSavedSegment(i);
                              }}
                            >
                              选中微调
                            </button>
                          )}
                          <button
                            type="button"
                            className="danger secondary"
                            onClick={(e) => {
                              e.stopPropagation();
                              removeSegment(i);
                            }}
                          >
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <div className="seg-list-footer design">
            <button
              type="button"
              className="secondary"
              onClick={clearSegmentList}
              disabled={manualSegments.length === 0}
            >
              清空列表
            </button>
          </div>
        </section>

        <section className="aside-card export-card design manual-export-card">
          <div className="aside-card-title">
            {showResults ? "获取 OSS 地址" : "切割与获取地址"}
          </div>
          {!showResults ? (
            <>
              <p className="hint-text export-format-note">
                输出格式：MP4 (H.264 / AAC)。完成后返回 OSS 对象地址。
              </p>
              <button
                type="button"
                className="full-width btn-primary-lg btn-confirm-cut"
                onClick={startManualCut}
                disabled={activeSegments.length === 0 || taskRunning}
              >
                <span className="btn-confirm-icon" aria-hidden>
                  ⚡
                </span>
                {taskRunning ? "切割中..." : "下一步：切割与保存"}
              </button>
            </>
          ) : (
            <>
              <p className="hint-text export-format-note">
                将片段推送到媒资切片区。勾选要保存的片段；未勾选则保存全部。
              </p>
              <div className="download-select-row">
                <button
                  type="button"
                  className="secondary linkish"
                  onClick={toggleSelectAllResults}
                >
                  {checkedResults.size >= results.length
                    ? "取消全选"
                    : "全选"}
                </button>
                <span className="download-selected-count">
                  已选 {checkedResults.size} / {results.length}
                </span>
              </div>
              <div className="export-list manual-result-list cut-result-grid">
                {results.map((seg, i) => {
                  const src = segmentPreviewUrl(seg);
                  const thumb = segmentThumbUrl(seg);
                  const playable = canPreviewSegment(seg);
                  return (
                    <div key={seg.id} className="cut-result-card">
                      <label className="cut-result-check">
                        <input
                          type="checkbox"
                          checked={checkedResults.has(i)}
                          onChange={() => toggleResultCheck(i)}
                        />
                        <span>
                          片段 {String(seg.index).padStart(2, "0")}
                        </span>
                      </label>
                      <button
                        type="button"
                        className="cut-result-thumb"
                        disabled={!playable || !src}
                        onClick={() => {
                          if (!src) return;
                          setResultPreview({
                            title: `片段 ${String(seg.index).padStart(2, "0")}`,
                            url: src,
                            range: `${formatTime(seg.start_time)} – ${formatTime(seg.end_time)}`,
                          });
                        }}
                        title={playable ? "预览播放切割成品" : "片段未就绪"}
                      >
                        {src || thumb ? (
                          <OutputSegCover
                            thumbUrl={thumb}
                            videoUrl={src}
                            aspectRatio={aspectRatio}
                          />
                        ) : (
                          <span className="cut-result-ph">…</span>
                        )}
                        <span className="cut-result-play">▶ 预览播放</span>
                      </button>
                      <div className="cut-result-meta">
                        {formatTime(seg.start_time)} – {formatTime(seg.end_time)}
                      </div>
                      <div className="cut-result-actions">
                        <button
                          type="button"
                          className="secondary"
                          disabled={!playable || !src}
                          onClick={() => {
                            if (!src) return;
                            setResultPreview({
                              title: `片段 ${String(seg.index).padStart(2, "0")}`,
                              url: src,
                              range: `${formatTime(seg.start_time)} – ${formatTime(seg.end_time)}`,
                            });
                          }}
                        >
                          预览播放
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
              <div className="library-save-actions manual-save-actions">
                <button
                  type="button"
                  className="full-width btn-primary-lg btn-confirm-cut"
                  onClick={() => void publishToLibrary()}
                  disabled={!taskId || results.length === 0 || publishing}
                >
                  {publishing
                    ? "保存中…"
                    : checkedResults.size > 0 &&
                        checkedResults.size < results.length
                      ? `返回选中 OSS 地址（${checkedResults.size}）`
                      : `返回全部 OSS 地址（${results.length}）`}
                </button>
              </div>
            </>
          )}
        </section>
      </div>
    );

    return (
      <div className={`panel panel-manual${active ? " active" : ""}`}>
        <div className="editor-deck manual-editor-deck design" id="manual-card">
          {!timelineMount && (
            <div className="timeline-fallback">{timelineNode}</div>
          )}

          <div className="control-dashboard design selection-console">
            <header className="selection-console-head">
              <div className="selection-console-title">
                <span className="selection-console-title-icon" aria-hidden>
                  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
                    <rect
                      x="2.5"
                      y="2.5"
                      width="15"
                      height="15"
                      rx="2.5"
                      stroke="currentColor"
                      strokeWidth="1.6"
                    />
                    <path
                      d="M6 10h8M10 6v8"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                    />
                  </svg>
                </span>
                当前选区
              </div>
              <div
                className="current-frame-badge"
                title="当前播放位置：时间 · 从片头起的帧序号"
              >
                <span className="current-frame-label">当前位置</span>
                <strong className="current-frame-tc">
                  {formatTime(player.currentTime)}
                </strong>
                <span className="current-frame-idx">
                  · 第 {currentFrame} 帧
                </span>
              </div>
            </header>

            <div className="selection-summary-card">
              <div className="summary-metric in-point">
                <div className="summary-metric-label">
                  <i className="metric-dot purple" aria-hidden />
                  入点
                </div>
                <div className="summary-metric-value tone-blue">
                  {selection.start != null
                    ? formatSmpte(selection.start, rate)
                    : "--:--:--:--"}
                </div>
                <div className="summary-metric-sub">
                  {inFrame != null ? `(${inFrame})` : "—"}
                </div>
              </div>
              <div className="summary-arrow" aria-hidden>
                →
              </div>
              <div className="summary-metric out-point">
                <div className="summary-metric-label">
                  <i className="metric-dot purple" aria-hidden />
                  出点
                </div>
                <div className="summary-metric-value tone-purple">
                  {selection.end != null
                    ? formatSmpte(selection.end, rate)
                    : "--:--:--:--"}
                </div>
                <div className="summary-metric-sub">
                  {outFrame != null ? `(${outFrame})` : "—"}
                </div>
              </div>
              <div className="summary-metric duration">
                <div className="summary-metric-label">
                  <i className="metric-icon clock" aria-hidden />
                  时长
                </div>
                <div className="summary-metric-value tone-green">
                  {selectionActive
                    ? formatSmpte(selectionDuration, rate)
                    : "--:--:--:--"}
                </div>
                <div className="summary-metric-sub">
                  {selectionActive
                    ? formatClockMs(selectionDuration)
                    : "—"}
                </div>
              </div>
              <div className="summary-metric frames">
                <div className="summary-metric-label">
                  <i className="metric-icon frames" aria-hidden />
                  帧数
                </div>
                <div className="summary-metric-value tone-orange">
                  {selectionActive ? `${selectionFrames} 帧` : "—"}
                </div>
                <div className="summary-metric-sub">
                  {rate.toFixed(2)} fps
                </div>
              </div>
            </div>

            <div className="control-card-actions design mark-actions">
              <button
                type="button"
                className="secondary btn-mark-point"
                onClick={setStartAtCurrent}
                title="将当前播放头位置标记为入点（快捷键 I）"
              >
                标记入点 [I]
              </button>
              <button
                type="button"
                className="secondary btn-mark-point"
                onClick={setEndAtCurrent}
                title="将当前播放头位置标记为出点（快捷键 O）"
              >
                标记出点 [O]
              </button>
            </div>

            <div className="selection-console-body">
              <section className="control-card design fine-tune-card">
                <div className="control-card-title">微调控制</div>

                <div className="fine-tune-group">
                  <div className="fine-tune-label">
                    {fineTuningSegment
                      ? "帧步进（推动较近的入/出点）"
                      : "帧步进（移动播放头）"}
                  </div>
                  <div
                    className="frame-jogger"
                    role="group"
                    aria-label={
                      fineTuningSegment
                        ? "按帧微调较近的入点或出点"
                        : "按帧移动播放头"
                    }
                  >
                    <button
                      type="button"
                      className="frame-jogger-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点后退 1 帧（←）"
                          : "后退一帧（←）"
                      }
                      aria-label="后退一帧"
                      onClick={() => stepFrameOrEdge(-1)}
                    >
                      <span className="frame-jogger-arrow" aria-hidden>
                        {"<"}
                      </span>
                    </button>
                    <div
                      className="frame-jogger-readout"
                      title="当前播放位置：时间 · 从片头起的帧序号"
                    >
                      <strong className="frame-jogger-tc">
                        {formatTime(player.currentTime)}
                      </strong>
                      <span className="frame-jogger-idx">
                        第 {currentFrame} 帧
                      </span>
                    </div>
                    <button
                      type="button"
                      className="frame-jogger-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点前进 1 帧（→）"
                          : "前进一帧（→）"
                      }
                      aria-label="前进一帧"
                      onClick={() => stepFrameOrEdge(1)}
                    >
                      <span className="frame-jogger-arrow" aria-hidden>
                        {">"}
                      </span>
                    </button>
                  </div>
                </div>

                <div className="fine-tune-group">
                  <div className="fine-tune-label">
                    {fineTuningSegment
                      ? "时间微调（推动较近的入/出点）"
                      : "时间微调（移动播放头）"}
                  </div>
                  <div className="fine-tune-row cols-4">
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点后退 5 秒"
                          : "播放头后退 5 秒"
                      }
                      onClick={() => seekBySeconds(-5)}
                    >
                      −5s
                    </button>
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点后退 1 秒"
                          : "播放头后退 1 秒"
                      }
                      onClick={() => seekBySeconds(-1)}
                    >
                      −1s
                    </button>
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点前进 1 秒"
                          : "播放头前进 1 秒"
                      }
                      onClick={() => seekBySeconds(1)}
                    >
                      +1s
                    </button>
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title={
                        fineTuningSegment
                          ? "将较近的入/出点前进 5 秒"
                          : "播放头前进 5 秒"
                      }
                      onClick={() => seekBySeconds(5)}
                    >
                      +5s
                    </button>
                  </div>
                </div>

                <div className="fine-tune-group">
                  <div className="fine-tune-label">快速跳转</div>
                  <div className="fine-tune-row">
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title="跳到整段视频开头"
                      onClick={() => player.seekPlayer(0, true)}
                    >
                      跳到片头
                    </button>
                    <button
                      type="button"
                      className="secondary fine-btn"
                      title="跳到整段视频结尾"
                      onClick={() => player.seekPlayer(duration, true)}
                    >
                      跳到片尾
                    </button>
                  </div>
                </div>

                <div className="fine-tune-group edge-nudge">
                  <div className="fine-tune-label">
                    入点 / 出点 精细微调 ±1帧
                  </div>
                  <div className="edge-nudge-row">
                    <div className="edge-nudge-pair">
                      <button
                        type="button"
                        className="secondary fine-btn edge"
                        disabled={!selectionActive}
                        title={
                          selectionActive
                            ? "将入点前移 1 帧，并跳到新入点预览"
                            : "请先在时间轴建立选区"
                        }
                        onClick={() => nudgeSelection("start", -frameSec)}
                      >
                        <i className="metric-dot purple" aria-hidden />
                        入点 −1帧
                      </button>
                      <button
                        type="button"
                        className="secondary fine-btn edge"
                        disabled={!selectionActive}
                        title={
                          selectionActive
                            ? "将入点后移 1 帧，并跳到新入点预览"
                            : "请先在时间轴建立选区"
                        }
                        onClick={() => nudgeSelection("start", frameSec)}
                      >
                        <i className="metric-dot purple" aria-hidden />
                        入点 +1帧
                      </button>
                    </div>
                    <div className="edge-nudge-divider" aria-hidden />
                    <div className="edge-nudge-pair">
                      <button
                        type="button"
                        className="secondary fine-btn edge"
                        disabled={!selectionActive}
                        title={
                          selectionActive
                            ? "将出点前移 1 帧，并跳到新出点预览"
                            : "请先在时间轴建立选区"
                        }
                        onClick={() => nudgeSelection("end", -frameSec)}
                      >
                        <i className="metric-dot purple" aria-hidden />
                        出点 −1帧
                      </button>
                      <button
                        type="button"
                        className="secondary fine-btn edge"
                        disabled={!selectionActive}
                        title={
                          selectionActive
                            ? "将出点后移 1 帧，并跳到新出点预览"
                            : "请先在时间轴建立选区"
                        }
                        onClick={() => nudgeSelection("end", frameSec)}
                      >
                        <i className="metric-dot purple" aria-hidden />
                        出点 +1帧
                      </button>
                    </div>
                  </div>
                </div>
              </section>

              <section className="control-card design manual-ops-card">
                <div className="control-card-title">操作</div>
                <div className="ops-primary-row design">
                  <button
                    type="button"
                    className="btn-add-segment"
                    onClick={addCurrentSegment}
                  >
                    + 添加片段
                  </button>
                  <button
                    type="button"
                    className="btn-play-selection"
                    onClick={playSelection}
                  >
                    <span className="btn-play-ico" aria-hidden>
                      ▶
                    </span>
                    播放选区
                  </button>
                </div>
                <div className="ops-play-row design">
                  <label className="loop-toggle design">
                    <input
                      type="checkbox"
                      checked={loopOn}
                      onChange={toggleLoop}
                    />
                    <span>循环播放选区</span>
                    <span
                      className="loop-tip"
                      title="开启后，播放选区会在入点与出点之间循环"
                    >
                      i
                    </span>
                  </label>
                </div>
                <div className="ops-main-row design">
                  <button
                    type="button"
                    className="danger secondary btn-clear-selection"
                    title="清理当前时间轴上的临时选区"
                    onClick={clearSelection}
                  >
                    清理临时选区
                  </button>
                </div>
              </section>
            </div>
          </div>

          <div className="shortcut-cheatsheet design">
            <span className="shortcut-cheatsheet-label">快捷键提示</span>
            <span>
              <kbd>Space</kbd> 播放选区 / 暂停
            </span>
            <span>
              <kbd>←</kbd> / <kbd>→</kbd>{" "}
              {fineTuningSegment ? "微调较近入/出点" : "帧步进"}
            </span>
            <span>
              <kbd>J</kbd> / <kbd>K</kbd>{" "}
              {fineTuningSegment ? "±1s 调较近入/出点" : "±1s"}
            </span>
            <span>
              <kbd>I</kbd> / <kbd>O</kbd> 标记入点/出点
            </span>
            <span>
              <kbd>Enter</kbd> 添加片段
            </span>
            <span>
              <kbd>L</kbd> 循环播放
            </span>
            <span>
              <kbd>Del</kbd> 删除选中片段 / 清理临时选区
            </span>
            <span>
              <kbd>Ctrl</kbd> + <kbd>Z</kbd> / <kbd>Y</kbd> 撤销/重做
            </span>
          </div>
        </div>
        {active && timelineMount && createPortal(timelineNode, timelineMount)}
        <PlayerSelectionOverlay
          mountId={playerMountId}
          active={active}
          duration={duration}
          currentTime={player.currentTime}
          start={previewStart ?? null}
          end={previewEnd ?? null}
        />
        {active && (resultMount ? createPortal(aside, resultMount) : aside)}
        {resultPreview &&
          createPortal(
            <ClipPreviewModal
              title={`预览 ${resultPreview.title}`}
              rangeText={resultPreview.range}
              src={resultPreview.url}
              onClose={() => setResultPreview(null)}
            />,
            document.body,
          )}
      </div>
    );
  },
);
