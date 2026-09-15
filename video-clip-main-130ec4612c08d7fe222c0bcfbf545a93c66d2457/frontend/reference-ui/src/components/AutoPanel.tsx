import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import type { Detector, SegmentResponse, Sensitivity, TaskResponse } from "../api/types";
import {
  confirmAutoTask,
  createAutoTask,
  getTaskStatus,
  publishSegments,
} from "../api/client";
import { SENSITIVITY_PRESETS, formatPresetValue, isPresetMinSceneLen, isPresetSampleFps, isPresetThreshold } from "../utils/presets";
import { formatTime } from "../utils/format";
import type { VideoPlayerApi } from "../hooks/useVideoPlayer";
import { useFilmstrip } from "../hooks/useFilmstrip";
import { autoWorkflowIndex } from "./WorkflowStepper";
import { SceneTimeline } from "./SceneTimeline";
import {
  TaskProgressBar,
  progressStyleForTask,
  DETECT_PIPELINE_PHASES,
} from "./TaskProgressBar";
import { PlayerSelectionOverlay } from "./PlayerSelectionOverlay";
import { ClipPreviewModal } from "./ClipPreviewModal";
import { OutputSegCover } from "./OutputSegCover";
import { canPreviewSegment, prefetchSegmentThumbs, segmentPreviewUrl, segmentThumbUrl } from "../utils/segmentMedia";

interface Props {
  videoId: string;
  duration: number;
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
  onStepChange?: (step: number) => void;
  onTaskChange?: (
    task: TaskResponse | null,
    opts?: { enterExecute?: boolean },
  ) => void;
  /** 从「执行切割」返回编辑时递增，用于恢复分镜列表、退出结果态 */
  editResumeKey?: number;
}

type PreviewScene = SegmentResponse;

function AlgoIconContent() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <rect x="4" y="8" width="10" height="16" rx="2" fill="currentColor" opacity="0.22" />
      <rect x="18" y="8" width="10" height="16" rx="2" fill="currentColor" opacity="0.22" />
      <path
        d="M16 6v20M11 11h10M11 16h10M11 21h10"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path d="M16 6l-2 3h4l-2-3z" fill="currentColor" />
    </svg>
  );
}

function AlgoIconAdaptive() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <circle cx="16" cy="16" r="5.5" stroke="currentColor" strokeWidth="1.75" fill="none" />
      <path
        d="M16 4v3M16 25v3M4 16h3M25 16h3M7.5 7.5l2.1 2.1M22.4 22.4l2.1 2.1M7.5 24.5l2.1-2.1M22.4 9.6l2.1-2.1"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path
        d="M20 22l4 4-2 1-1 2-4-4"
        fill="currentColor"
        opacity="0.85"
      />
    </svg>
  );
}

function AlgoIconSemantic() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path
        d="M10 22c0-3.3 2.7-6 6-6s6 2.7 6 6"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        fill="none"
      />
      <circle cx="16" cy="12" r="4" stroke="currentColor" strokeWidth="1.75" fill="none" />
      <path
        d="M8 8l1.5 1.5M24 8l-1.5 1.5M6 18h2.5M23.5 18H26"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
      />
      <path d="M22 6l1.2 2.4 2.6.4-1.9 1.8.5 2.6L22 11.8l-2.4 1.2.5-2.6-1.9-1.8 2.6-.4L22 6z" fill="currentColor" />
    </svg>
  );
}

const DETECTOR_ICONS: Record<Detector, () => JSX.Element> = {
  content: AlgoIconContent,
  adaptive: AlgoIconAdaptive,
  semantic: AlgoIconSemantic,
};

const DETECTOR_OPTIONS: {
  id: Detector;
  label: string;
  hint: string;
  badge?: string;
}[] = [
  {
    id: "content",
    label: "画面切镜",
    badge: "推荐",
    hint: "适合大多数视频，按画面变化自动找切点，本地计算、速度快。",
  },
  {
    id: "semantic",
    label: "语义分镜",
    badge: "AI大模型",
    hint: "按语义话题切分，需联网调用 AI 大模型，耗时会明显增加。",
  },
  {
    id: "adaptive",
    label: "抗闪切镜",
    hint: "适合闪光、曝光变化多的素材，减少误切。",
  },
];

function detectorHint(id: Detector): string {
  return DETECTOR_OPTIONS.find((o) => o.id === id)?.hint ?? "";
}

function segmentListLabel(
  detector: Detector,
  seg: PreviewScene,
  index: number,
): string {
  if (seg.summary?.trim()) return seg.summary.trim();
  if (detector === "semantic") return `场景 ${index + 1}`;
  return `切镜片段 ${String(index + 1).padStart(2, "0")}`;
}

function detectorDisplayName(id: Detector): string {
  return DETECTOR_OPTIONS.find((o) => o.id === id)?.label ?? "—";
}

interface ParamSliderProps {
  label: string;
  hint?: string;
  min: number;
  max: number;
  step: number;
  value: number;
  display: string;
  onChange: (value: number) => void;
}

function ParamSlider({
  label,
  hint,
  min,
  max,
  step,
  value,
  display,
  onChange,
}: ParamSliderProps) {
  return (
    <label className="auto-slider-field">
      <span className="auto-slider-label-row">
        <span>{label}</span>
        <em>{display}</em>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
      {hint ? <small className="auto-slider-hint">{hint}</small> : null}
    </label>
  );
}

function formatElapsed(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

type ListFilter = "all" | "selected" | "review" | "unselected";
type ListSort = "time" | "duration";

function sortByListSort<T extends PreviewScene>(
  items: T[],
  sort: ListSort,
): T[] {
  const copy = [...items];
  if (sort === "duration") {
    return copy.sort(
      (a, b) => b.end_time - b.start_time - (a.end_time - a.start_time),
    );
  }
  return copy.sort((a, b) => a.start_time - b.start_time);
}

function sortPreviewEntries(
  items: { seg: PreviewScene; globalIndex: number }[],
  sort: ListSort,
): { seg: PreviewScene; globalIndex: number }[] {
  const copy = [...items];
  if (sort === "duration") {
    return copy.sort(
      (a, b) =>
        b.seg.end_time -
        b.seg.start_time -
        (a.seg.end_time - a.seg.start_time),
    );
  }
  return copy.sort((a, b) => a.seg.start_time - b.seg.start_time);
}

function buildPreviewListItems(
  scenes: PreviewScene[],
  filter: ListFilter,
  checked: Set<number>,
  selectedIdx: number,
): { seg: PreviewScene; globalIndex: number }[] {
  const all = scenes.map((seg, globalIndex) => ({ seg, globalIndex }));
  switch (filter) {
    case "selected":
      return all.filter(
        ({ globalIndex }) =>
          checked.has(globalIndex) || globalIndex === selectedIdx,
      );
    case "review":
      return all.filter(
        ({ seg }) => seg.end_time - seg.start_time < 2.5,
      );
    case "unselected":
      return all.filter(({ globalIndex }) => !checked.has(globalIndex));
    default:
      return all;
  }
}

/** 平均段长低于该秒数则视为过碎（按时长动态判断，不再固定 >50） */
const FRAGMENT_AVG_SEC = 3;

function buildSceneWarn(count: number, videoDuration: number): string | null {
  if (count <= 1 || videoDuration <= 0) return null;
  const avg = videoDuration / count;
  if (avg >= FRAGMENT_AVG_SEC) return null;
  return (
    `⚠ 分镜较多（${count} 个，约 ${avg.toFixed(1)} 秒/段），可能过碎。` +
    `建议删除碎段、合并相邻分镜，或改用「粗略」预设后重新检测。`
  );
}

export function AutoPanel({
  videoId,
  duration,
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
  onStepChange,
  onTaskChange,
  editResumeKey = 0,
}: Props) {
  void playerRef;
  void previewUrl;
  void sourceFilename;
  const activeRef = useRef(active);
  activeRef.current = active;
  const [publishing, setPublishing] = useState(false);
  const [detector, setDetector] = useState<Detector>("content");
  /** 当前任务实际使用的算法（进度条/任务卡跟它走，不跟切换中的选项） */
  const [taskDetector, setTaskDetector] = useState<Detector | null>(null);
  const [sensitivity, setSensitivity] = useState<Sensitivity>("standard");
  const [threshold, setThreshold] = useState(35);
  const [sampleFps, setSampleFps] = useState(1);
  const [minSceneLen, setMinSceneLen] = useState(2);
  const [customSensitivity, setCustomSensitivity] = useState(false);
  const [listFilter, setListFilter] = useState<ListFilter>("all");
  const [listSort, setListSort] = useState<ListSort>("time");
  const [listPage, setListPage] = useState(0);
  const [listPageSize, setListPageSize] = useState(10);
  const [checkedScenes, setCheckedScenes] = useState<Set<number>>(new Set());
  const [taskElapsedSec, setTaskElapsedSec] = useState<number | null>(null);
  const [autoCutDirect, setAutoCutDirect] = useState(false);
  const [detectLoading, setDetectLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  const [cutFailed, setCutFailed] = useState(false);
  const [cutDoneOnce, setCutDoneOnce] = useState(false);

  const [taskId, setTaskId] = useState<string | null>(null);
  const [showStatus, setShowStatus] = useState(false);
  const [statusProgress, setStatusProgress] = useState(0);
  const [statusMessage, setStatusMessage] = useState("");

  const [previewScenes, setPreviewScenes] = useState<PreviewScene[]>([]);
  const [selectedSceneIndex, setSelectedSceneIndex] = useState(-1);
  const [showPreviewBanner, setShowPreviewBanner] = useState(false);
  const [sceneWarn, setSceneWarn] = useState<string | null>(null);

  const [results, setResults] = useState<SegmentResponse[]>([]);
  const [showResults, setShowResults] = useState(false);
  const [resultPreview, setResultPreview] = useState<{
    title: string;
    url: string;
    range: string;
  } | null>(null);

  const pollTimerRef = useRef<number | null>(null);
  const pollModeRef = useRef<"auto" | "auto-cut">("auto");
  const autoCutTriggeredRef = useRef(false);
  const taskStartedAtRef = useRef<number | null>(null);

  const isSemantic = detector === "semantic";

  const markTaskElapsed = useCallback(() => {
    const started = taskStartedAtRef.current;
    if (started == null) return;
    setTaskElapsedSec((Date.now() - started) / 1000);
  }, []);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current != null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // 离开编辑页时停轮询，避免与执行页双重刷新
  useEffect(() => {
    if (!active) stopPolling();
  }, [active, stopPolling]);

  const applyPreset = useCallback(
    (det: Detector, sens: Sensitivity) => {
      const preset = SENSITIVITY_PRESETS[det][sens];
      if (!preset) return;
      if (det !== "semantic") setThreshold(preset.threshold);
      setMinSceneLen(preset.minSceneLen);
      if (preset.sampleFps != null) setSampleFps(preset.sampleFps);
    },
    [],
  );

  // 切换算法时始终套用当前灵敏度预设，并退出自定义态
  useEffect(() => {
    setCustomSensitivity(false);
    applyPreset(detector, sensitivity);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅在算法切换时重置
  }, [detector]);

  // 点选「精准/标准/粗略」时套用预设；自定义拖动滑杆后不覆盖
  useEffect(() => {
    if (customSensitivity) return;
    applyPreset(detector, sensitivity);
  }, [applyPreset, customSensitivity, detector, sensitivity]);

  const resetAutoPreview = useCallback(() => {
    autoCutTriggeredRef.current = false;
    setShowPreviewBanner(false);
    setPreviewScenes([]);
    setSelectedSceneIndex(-1);
    setSceneWarn(null);
    setShowResults(false);
    setResults([]);
    setCutFailed(false);
    setCutDoneOnce(false);
    taskStartedAtRef.current = null;
    setTaskElapsedSec(null);

    player.stopMergedScenePreview();
    player.setScenePreviewFromSegment(null);
    player.stopPreviewGuard();
  }, [player]);

  // 检测进行中实时刷新耗时，避免一直显示 —
  useEffect(() => {
    if (!detectLoading && !showStatus) return;
    if (taskStartedAtRef.current == null) return;
    markTaskElapsed();
    const timer = window.setInterval(markTaskElapsed, 1000);
    return () => clearInterval(timer);
  }, [detectLoading, markTaskElapsed, showStatus]);

  useEffect(() => {
    resetAutoPreview();
    setShowStatus(false);
    setTaskId(null);
    setTaskDetector(null);
    stopPolling();
  }, [videoId]); // eslint-disable-line react-hooks/exhaustive-deps

  const detectButtonLabel = detectLoading
    ? autoCutDirect
      ? "正在切分并导出..."
      : "正在切分..."
    : autoCutDirect
      ? "切分并导出"
      : "开始自动切分";

  const selectPreviewScene = useCallback(
    (index: number) => {
      const seg = previewScenes[index];
      if (!seg) return;
      setSelectedSceneIndex(index);
      player.setScenePreviewFromSegment(seg);
      player.playSceneSegment(seg);
    },
    [player, previewScenes],
  );

  useEffect(() => {
    if (selectedSceneIndex >= 0 && previewScenes[selectedSceneIndex]) {
      player.setScenePreviewFromSegment(previewScenes[selectedSceneIndex]);
    } else if (selectedSceneIndex < 0) {
      player.setScenePreviewFromSegment(null);
    }
  }, [player, previewScenes, selectedSceneIndex]);

  const renderEditableScenesDone = useCallback(
    (scenes: PreviewScene[]) => {
      setPreviewScenes(scenes);
      setShowPreviewBanner(true);
      setSceneWarn(buildSceneWarn(scenes.length, duration));
      markTaskElapsed();
      if (scenes.length > 0) {
        setSelectedSceneIndex(0);
        const first = scenes[0];
        player.setScenePreviewFromSegment(first);
        player.playSceneSegment(first);
        toast(
          `切分完成，共 ${scenes.length} 个片段，已自动预览第 1 段，可点击列表切换`,
        );
      } else {
        toast("切分完成，未发现可用切点");
      }
    },
    [duration, markTaskElapsed, player, toast],
  );

  const pollTask = useCallback(
    (id: string, mode: "auto" | "auto-cut") => {
      stopPolling();
      pollModeRef.current = mode;
      pollTimerRef.current = window.setInterval(async () => {
        try {
          const task = await getTaskStatus(id);
          const raw = Number(task.progress) || 0;
          setStatusProgress(raw <= 1 ? raw * 100 : raw);
          if (task.message) setStatusMessage(task.message);
          onTaskChange?.(task);

          if (task.status === "preview" && mode === "auto") {
            stopPolling();
            setShowStatus(false);
            setStatusMessage("");
            setDetectLoading(false);
            markTaskElapsed();
            renderEditableScenesDone(
              task.segments.map((s) => ({ ...s })),
            );
          }

          if (
            task.status === "preview" &&
            mode === "auto-cut" &&
            !autoCutTriggeredRef.current
          ) {
            autoCutTriggeredRef.current = true;
            setConfirmLoading(true);
            setStatusMessage("检测完成，正在提交 OSS 流式切割...");
            const cutting = await confirmAutoTask(id, task.segments);
            onTaskChange?.(cutting);
          }

          if (task.status === "done") {
            stopPolling();
            setDetectLoading(false);
            setConfirmLoading(false);
            setCutFailed(false);
            setCutDoneOnce(true);
            setShowPreviewBanner(false);
            setStatusMessage("");
            markTaskElapsed();
            // 保留 previewScenes；若用户已在编辑页则不要切到结果态
            setResults(task.segments);
            setShowResults(!activeRef.current);
            setListPage(0);
            prefetchSegmentThumbs(task.segments);
            toast("切割完成！可预览并保存片段");
          }

          if (task.status === "failed") {
            stopPolling();
            setDetectLoading(false);
            setConfirmLoading(false);
            setShowStatus(false);
            markTaskElapsed();
            setStatusMessage(task.message || "");
            if (pollModeRef.current === "auto-cut") {
              setCutFailed(true);
            }
            toast(task.message || "任务失败", true);
          }
        } catch {
          stopPolling();
          setDetectLoading(false);
          setConfirmLoading(false);
          toast("任务状态刷新失败，请稍后重试", true);
        }
      }, mode === "auto" ? 800 : 1000);
    },
    [markTaskElapsed, onTaskChange, renderEditableScenesDone, stopPolling, toast],
  );

  const handleDetect = useCallback(async () => {
    if (!videoId) {
      toast("请先从 OSS 导入源片", true);
      return;
    }
    try {
      setDetectLoading(true);
      resetAutoPreview();
      setTaskDetector(detector);
      setShowStatus(true);
      setStatusProgress(0);
      setStatusMessage(
        detector === "semantic"
          ? "语义分镜已开始，AI大模型分析通常需要数分钟..."
          : "正在自动切分...",
      );
      const startedAt = Date.now();
      taskStartedAtRef.current = startedAt;
      setTaskElapsedSec(0);

      const payload = {
        video_id: videoId,
        detector,
        threshold: isSemantic ? sampleFps : threshold,
        min_scene_len: minSceneLen,
        auto_cut: autoCutDirect,
        ...(isSemantic ? { sample_fps: sampleFps || 1 } : {}),
      };

      const task = await createAutoTask(payload);
      setTaskId(task.id);
      // 检测阶段留在编辑页；仅「检测并切割」直达执行页
      onTaskChange?.(task, { enterExecute: autoCutDirect });
      pollTask(task.id, autoCutDirect ? "auto-cut" : "auto");

      const waitHint =
        detector === "semantic"
          ? "语义分镜（AI大模型）已开始，可能需数分钟"
          : "已开始自动切分，约需 1-2 分钟";
      toast(autoCutDirect ? "已开始切分并导出，请稍候..." : waitHint);
    } catch (e) {
      toast(e instanceof Error ? e.message : "创建任务失败", true);
      setDetectLoading(false);
    }
  }, [
    autoCutDirect,
    detector,
    isSemantic,
    minSceneLen,
    pollTask,
    resetAutoPreview,
    sampleFps,
    threshold,
    toast,
    videoId,
    onTaskChange,
  ]);

  const handleConfirm = useCallback(async () => {
    if (!taskId || previewScenes.length === 0) {
      toast("没有可切割的分镜", true);
      return;
    }
    const indices =
      checkedScenes.size > 0
        ? [...checkedScenes].sort((a, b) => a - b)
        : previewScenes.map((_, i) => i);
    const segmentsToCut = indices
      .map((i) => previewScenes[i])
      .filter((seg): seg is PreviewScene => Boolean(seg));
    if (segmentsToCut.length === 0) {
      toast("没有可切割的分镜", true);
      return;
    }
    try {
      setConfirmLoading(true);
      setCutFailed(false);
      const confirmed = await confirmAutoTask(taskId, segmentsToCut);
      setShowPreviewBanner(false);
      onTaskChange?.(confirmed, { enterExecute: true });

      pollTask(taskId, "auto-cut");
      toast(
        checkedScenes.size > 0
          ? `开始切割 ${segmentsToCut.length} 个选中分镜...`
          : `开始切割全部 ${segmentsToCut.length} 个分镜...`,
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : "确认失败", true);
      setConfirmLoading(false);
    }
  }, [checkedScenes, onTaskChange, pollTask, previewScenes, taskId, toast]);

  const sortedResults = sortByListSort(results, listSort);

  const handlePublishToLibrary = useCallback(async () => {
    if (!taskId || publishing || results.length === 0) return;
    // 勾选下标对应当前排序后的列表，不能用原始 results 下标
    const selectedIds = [...checkedScenes]
      .sort((a, b) => a - b)
      .map((i) => sortedResults[i]?.id)
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
  }, [
    checkedScenes,
    onTaskChange,
    publishing,
    results.length,
    sortedResults,
    taskId,
    toast,
  ]);

  const toggleSelectAllResults = useCallback(() => {
    if (checkedScenes.size >= sortedResults.length) {
      setCheckedScenes(new Set());
      return;
    }
    setCheckedScenes(new Set(sortedResults.map((_, i) => i)));
  }, [checkedScenes.size, sortedResults]);

  const toggleSelectAllScenes = useCallback(() => {
    setCheckedScenes((prev) => {
      const total = previewScenes.length;
      if (total === 0) return prev;
      if (prev.size >= total) return new Set();
      return new Set(previewScenes.map((_, i) => i));
    });
  }, [previewScenes]);

  const toggleSelectFilteredScenes = useCallback(
    (indices: number[]) => {
      if (indices.length === 0) return;
      setCheckedScenes((prev) => {
        const allOn = indices.every((i) => prev.has(i));
        if (allOn) {
          const next = new Set(prev);
          for (const i of indices) next.delete(i);
          return next;
        }
        const next = new Set(prev);
        for (const i of indices) next.add(i);
        return next;
      });
    },
    [],
  );

  const resizeSceneBounds = useCallback(
    (index: number, start: number, end: number) => {
      setCustomSensitivity(true);
      setPreviewScenes((prev) =>
        prev.map((seg, i) =>
          i === index ? { ...seg, start_time: start, end_time: end } : seg,
        ),
      );
    },
    [],
  );

  const deletePreviewScene = useCallback(
    (index: number) => {
      if (showResults || index < 0 || index >= previewScenes.length) return;
      const nextCount = previewScenes.length - 1;
      setPreviewScenes((prev) => prev.filter((_, i) => i !== index));
      setCheckedScenes((prev) => {
        const next = new Set<number>();
        for (const i of prev) {
          if (i < index) next.add(i);
          else if (i > index) next.add(i - 1);
        }
        return next;
      });
      setSelectedSceneIndex((cur) => {
        if (cur === index) {
          return nextCount <= 0 ? -1 : Math.min(index, nextCount - 1);
        }
        if (cur > index) return cur - 1;
        return cur;
      });
      setSceneWarn(buildSceneWarn(nextCount, duration));
      toast(`已删除片段 ${index + 1}，剩余 ${nextCount} 个场景`);
    },
    [duration, previewScenes.length, showResults, toast],
  );

  const mergeSceneWithPrev = useCallback(() => {
    const idx = selectedSceneIndex;
    const prevSeg = previewScenes[idx - 1];
    const current = previewScenes[idx];
    if (!prevSeg || !current || showResults || idx <= 0) return;

    player.stopMergedScenePreview();

    const combinedSummary = [prevSeg.summary?.trim(), current.summary?.trim()]
      .filter(Boolean)
      .join(" / ");

    const merged: PreviewScene = {
      ...prevSeg,
      end_time: current.end_time,
      end_frame: current.end_frame ?? prevSeg.end_frame,
      summary: combinedSummary || prevSeg.summary,
    };

    const nextCount = previewScenes.length - 1;
    setPreviewScenes((prev) => [
      ...prev.slice(0, idx - 1),
      merged,
      ...prev.slice(idx + 1),
    ]);
    setCheckedScenes((prev) => {
      const next = new Set<number>();
      for (const i of prev) {
        if (i < idx - 1) next.add(i);
        else if (i === idx - 1 || i === idx) next.add(idx - 1);
        else if (i > idx) next.add(i - 1);
      }
      return next;
    });
    setSelectedSceneIndex(idx - 1);
    setSceneWarn(buildSceneWarn(nextCount, duration));
    player.setScenePreviewFromSegment(merged);
    player.playSceneSegment(merged);
    toast(`已合并片段 ${idx} 与 ${idx + 1}，当前共 ${nextCount} 个场景`);
  }, [
    duration,
    player,
    previewScenes,
    selectedSceneIndex,
    showResults,
    toast,
  ]);




  useEffect(() => {
    setListPage(0);
  }, [previewScenes.length, results.length, showResults, listFilter, listSort, listPageSize]);

  useEffect(() => {
    if (showResults || selectedSceneIndex < 0 || previewScenes.length === 0) {
      return;
    }
    setListPage(
      Math.floor(selectedSceneIndex / listPageSize),
    );
  }, [selectedSceneIndex, showResults, previewScenes.length, listPageSize]);

  const selectedScene =
    selectedSceneIndex >= 0 ? previewScenes[selectedSceneIndex] : null;
  const filmstrip = useFilmstrip(videoId, duration, active);

  const playTimeline = useCallback(() => {
    if (selectedSceneIndex >= 0 && previewScenes[selectedSceneIndex]) {
      selectPreviewScene(selectedSceneIndex);
      return;
    }
    playerRef.current?.play().catch(() => {});
  }, [playerRef, previewScenes, selectPreviewScene, selectedSceneIndex]);

  const filmstripThumbStyle = useCallback(
    (time: number) => {
      if (!filmstrip.url || !duration) return undefined;
      const count = Math.max(1, filmstrip.count || 8);
      const idx = Math.min(
        count - 1,
        Math.max(0, Math.floor((time / duration) * count)),
      );
      const pct = count <= 1 ? 0 : (idx / (count - 1)) * 100;
      const style: CSSProperties = {
        backgroundImage: `url(${filmstrip.url})`,
        backgroundSize: `${count * 100}% 100%`,
        backgroundPosition: `${pct}% 0`,
        backgroundRepeat: "no-repeat",
      };
      if (aspectRatio && aspectRatio > 0) {
        style.aspectRatio = String(aspectRatio);
      }
      return style;
    },
    [aspectRatio, duration, filmstrip.count, filmstrip.url],
  );

  const toggleSceneCheck = useCallback((index: number) => {
    setCheckedScenes((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const workflowStep = autoWorkflowIndex({
    detecting: detectLoading || (showStatus && previewScenes.length === 0),
    sceneCount: previewScenes.length,
    cutting: confirmLoading,
    doneCount: showResults ? results.length : 0,
    modeSelected: active,
    uploaded: Boolean(videoId),
  });

  useEffect(() => {
    if (!active) return;
    onStepChange?.(workflowStep);
  }, [active, onStepChange, workflowStep]);

  // 从执行页返回 / 步骤回退：停轮询并退出检测/切割中 UI
  useEffect(() => {
    if (!editResumeKey) return;
    stopPolling();
    setShowResults(false);
    setConfirmLoading(false);
    setDetectLoading(false);
    setShowStatus(false);
    setPreviewScenes((prev) => {
      if (prev.length > 0) return prev;
      if (results.length === 0) return prev;
      return results.map((s) => ({
        ...s,
        start_time: s.start_time,
        end_time: s.end_time,
      }));
    });
    setShowPreviewBanner(true);
    setCheckedScenes(new Set());
    setSelectedSceneIndex(-1);
    setListPage(0);
  }, [editResumeKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const resumeSceneEditing = useCallback(() => {
    setShowResults(false);
    setConfirmLoading(false);
    setPreviewScenes((prev) => {
      if (prev.length > 0) return prev;
      if (results.length === 0) return prev;
      return results.map((s) => ({
        ...s,
        start_time: s.start_time,
        end_time: s.end_time,
      }));
    });
    setShowPreviewBanner(true);
    setCheckedScenes(new Set());
    setSelectedSceneIndex(-1);
    setListPage(0);
    toast("已返回场景列表，可调整后重新确认切分");
  }, [results, toast]);

  const [resultMount, setResultMount] = useState<HTMLElement | null>(null);
  const [timelineMount, setTimelineMount] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!resultMountId) {
      setResultMount(null);
      return;
    }
    setResultMount(document.getElementById(resultMountId));
  }, [
    resultMountId,
    active,
    previewScenes.length,
    showResults,
    showStatus,
    player.mergePreviewActive,
  ]);

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

  const selectedDuration =
    selectedScene != null
      ? selectedScene.end_time - selectedScene.start_time
      : 0;

  const taskBusy =
    detectLoading || confirmLoading || publishing || showStatus;
  const taskHasOutcome =
    Boolean(showResults || previewScenes.length > 0 || cutFailed);
  /** 未开始切分时不展示进度条 */
  const showTaskProgress = taskBusy || taskHasOutcome;
  /** 进度样式/任务卡算法：跟当前任务，不跟切换中的选项 */
  const progressDetector = taskDetector ?? detector;

  const sceneTimelineNode = (
    <div className="auto-timeline-block">
      <SceneTimeline
        videoId={videoId}
        duration={duration}
        currentTime={player.currentTime}
        scenes={previewScenes}
        selectedIndex={selectedSceneIndex}
        onSeek={player.seekPlayer}
        onSelectScene={selectPreviewScene}
        onPlay={playTimeline}
        showLegend={false}
        showSceneLabels={progressDetector === "semantic"}
        editable={!showResults && previewScenes.length > 0}
        onSceneBoundsChange={resizeSceneBounds}
        enableMedia={active}
      />
    </div>
  );

  const allCount = showResults ? results.length : previewScenes.length;
  const canConfirm = Boolean(taskId && previewScenes.length > 0 && !showResults);
  const selectedCount = checkedScenes.size;
  const checkedDownloadCount = showResults ? checkedScenes.size : 0;
  const reviewCount = previewScenes.filter(
    (s) => s.end_time - s.start_time < 2.5,
  ).length;
  const unselectedCount = Math.max(0, previewScenes.length - checkedScenes.size);

  const previewListItems = sortPreviewEntries(
    buildPreviewListItems(
      previewScenes,
      listFilter,
      checkedScenes,
      selectedSceneIndex,
    ),
    listSort,
  );
  const activeListLength = showResults
    ? sortedResults.length
    : previewListItems.length;
  const previewTotalPages = Math.max(
    1,
    Math.ceil(activeListLength / listPageSize),
  );
  const safeListPage = Math.min(listPage, previewTotalPages - 1);
  const pageOffset = safeListPage * listPageSize;

  const pagedList = showResults
    ? sortedResults.slice(pageOffset, pageOffset + listPageSize)
    : previewListItems.slice(pageOffset, pageOffset + listPageSize);

  const filteredIndices = showResults
    ? sortedResults.map((_, i) => i)
    : previewListItems.map((item) => item.globalIndex);
  const filteredAllChecked =
    filteredIndices.length > 0 &&
    filteredIndices.every((i) => checkedScenes.has(i));
  const filteredSomeChecked =
    !filteredAllChecked &&
    filteredIndices.some((i) => checkedScenes.has(i));
  const allScenesChecked =
    previewScenes.length > 0 && checkedScenes.size >= previewScenes.length;

  const taskStatusLabel = showResults
    ? "切分完成"
    : previewScenes.length > 0
      ? "切分完成"
      : detectLoading || showStatus
        ? "切分中"
        : "等待中";

  const progressPct = !showTaskProgress
    ? 0
    : showResults || (previewScenes.length > 0 && !detectLoading && !confirmLoading)
      ? 100
      : Math.min(100, Math.max(0, statusProgress));
  const progressStyle =
    publishing
      ? progressStyleForTask({ kind: "exporting" })
      : confirmLoading || (detectLoading && autoCutDirect)
        ? progressStyleForTask({ kind: "cutting" })
        : progressStyleForTask({
            kind:
              progressDetector === "semantic"
                ? "detect-semantic"
                : progressDetector === "adaptive"
                  ? "detect-adaptive"
                  : "detect-content",
          });
  const progressTone =
    cutFailed
      ? "fail"
      : !taskBusy && taskHasOutcome
        ? "ok"
        : "busy";
  const progressLabel = publishing
    ? "正在获取 OSS 地址…"
    : confirmLoading
      ? "正在生成片段…"
      : detectLoading || showStatus
        ? statusMessage ||
          (progressDetector === "semantic"
            ? "正在分析视频内容…"
            : progressDetector === "adaptive"
              ? "正在抗闪切分…"
              : "正在自动切分…")
        : showResults || previewScenes.length > 0
          ? "切分完成"
          : "等待开始";
  const progressMeta =
    detectLoading || confirmLoading || publishing || showStatus
      ? `耗时 ${formatElapsed(taskElapsedSec)}`
      : taskElapsedSec != null && taskHasOutcome
        ? `用时 ${formatElapsed(taskElapsedSec)}`
        : undefined;

  const goPrevScene = useCallback(() => {
    if (selectedSceneIndex > 0) selectPreviewScene(selectedSceneIndex - 1);
  }, [selectPreviewScene, selectedSceneIndex]);

  const goNextScene = useCallback(() => {
    if (selectedSceneIndex < previewScenes.length - 1) {
      selectPreviewScene(selectedSceneIndex + 1);
    }
  }, [previewScenes.length, selectPreviewScene, selectedSceneIndex]);

  const algoPickerNode = (
    <div className="auto-algo-picker">
      <div className="auto-algo-picker-label">检测算法</div>
      <div className="auto-algo-list" role="radiogroup" aria-label="检测算法">
        {DETECTOR_OPTIONS.map((opt) => {
          const Icon = DETECTOR_ICONS[opt.id];
          const active = detector === opt.id;
          const locked = detectLoading || confirmLoading || showStatus;
          return (
            <button
              key={opt.id}
              type="button"
              role="radio"
              aria-checked={active}
              className={`auto-algo-card${active ? " active" : ""}`}
              disabled={locked}
              title={
                locked
                  ? "切分进行中，请等待当前任务结束后再切换算法"
                  : undefined
              }
              onClick={() => setDetector(opt.id)}
            >
              <span className="auto-algo-card-icon">
                <Icon />
              </span>
              <span className="auto-algo-card-text">
                <span className="auto-algo-card-label">{opt.label}</span>
                {opt.badge ? (
                  <span
                    className={`auto-algo-card-badge${opt.badge === "AI大模型" ? " warn" : ""}`}
                  >
                    {opt.badge}
                  </span>
                ) : null}
              </span>
            </button>
          );
        })}
      </div>
      <p className="auto-algo-picker-hint">{detectorHint(detector)}</p>
    </div>
  );

  const settingsFlatPanel = (
    <div className="auto-settings-stack">
      <div className="auto-settings-block">
        <div className="auto-sensitivity-row" role="group" aria-label="检测灵敏度">
          <button
            type="button"
            className={`auto-sensitivity-btn${!customSensitivity && sensitivity === "fine" ? " active" : ""}`}
            title={
              isSemantic
                ? "分镜更细，最短片段约 1.5 秒，适合内容变化多的素材"
                : "切得更细，最短片段约 1 秒，适合镜头切换多的素材"
            }
            onClick={() => {
              setCustomSensitivity(false);
              setSensitivity("fine");
            }}
          >
            精准
          </button>
          <button
            type="button"
            className={`auto-sensitivity-btn${!customSensitivity && sensitivity === "standard" ? " active" : ""}`}
            title={
              isSemantic
                ? "均衡切分，最短片段约 3 秒，适合大多数视频"
                : "均衡切分，最短片段约 2 秒，适合大多数视频"
            }
            onClick={() => {
              setCustomSensitivity(false);
              setSensitivity("standard");
            }}
          >
            标准
          </button>
          <button
            type="button"
            className={`auto-sensitivity-btn${!customSensitivity && sensitivity === "coarse" ? " active" : ""}`}
            title={
              isSemantic
                ? "分镜更粗，最短片段约 6 秒，适合内容变化少的素材"
                : "切得更少，最短片段约 3 秒，适合镜头切换少的素材"
            }
            onClick={() => {
              setCustomSensitivity(false);
              setSensitivity("coarse");
            }}
          >
            粗略
          </button>
        </div>
        <p className="auto-sensitivity-hint">
          {customSensitivity
            ? "已按下方自定义参数调整，可点上方预设快速恢复"
            : isSemantic
              ? sensitivity === "fine"
                ? "精准：分镜更细，最短片段约 1.5 秒，适合内容变化多的素材"
                : sensitivity === "coarse"
                  ? "粗略：分镜更粗，最短片段约 6 秒，适合内容变化少的素材"
                  : "标准：均衡切分，最短片段约 3 秒，适合大多数视频"
              : sensitivity === "fine"
                ? "精准：切得更细，最短片段约 1 秒，适合镜头切换多的素材"
                : sensitivity === "coarse"
                  ? "粗略：切得更少，最短片段约 3 秒，适合镜头切换少的素材"
                  : "标准：均衡切分，最短片段约 2 秒，适合大多数视频"}
        </p>
      </div>

      <div className="auto-settings-block">
        <div className="auto-settings-block-label">自定义参数</div>
        {isSemantic ? (
          <div className="auto-slider-group">
            <ParamSlider
              label="抽帧密度"
              hint="调高会看更多帧、切得更细，但更慢；一般不用改"
              min={0.25}
              max={5}
              step={0.25}
              value={sampleFps}
              display={formatPresetValue(
                `${sampleFps.toFixed(2)} 帧/秒`,
                isPresetSampleFps("semantic", sampleFps),
              )}
              onChange={(v) => {
                setCustomSensitivity(true);
                setSampleFps(v);
              }}
            />
            <ParamSlider
              label="最短片段"
              hint={`不足 ${minSceneLen.toFixed(1)} 秒的检测结果会自动并入相邻片段，避免碎段单独成片。想保留更短镜头就调低，想更粗就调高。`}
              min={0.5}
              max={30}
              step={0.5}
              value={minSceneLen}
              display={formatPresetValue(
                `${minSceneLen.toFixed(1)} 秒`,
                isPresetMinSceneLen("semantic", minSceneLen),
              )}
              onChange={(v) => {
                setCustomSensitivity(true);
                setMinSceneLen(v);
              }}
            />
          </div>
        ) : (
          <div className="auto-slider-group">
            <ParamSlider
              label="切分敏感度"
              hint="越小剪辑拆分越细碎，易出现多余切镜；越大剪辑越粗放，容易缺失必要切镜。"
              min={1}
              max={100}
              step={1}
              value={threshold}
              display={formatPresetValue(
                String(threshold),
                isPresetThreshold(detector, threshold),
              )}
              onChange={(v) => {
                setCustomSensitivity(true);
                setThreshold(v);
              }}
            />
            <ParamSlider
              label="最短片段"
              hint={`不足 ${minSceneLen.toFixed(1)} 秒的检测结果会自动并入相邻片段，避免碎段单独成片。想保留更短镜头就调低，想更粗就调高。`}
              min={0.5}
              max={30}
              step={0.5}
              value={minSceneLen}
              display={formatPresetValue(
                `${minSceneLen.toFixed(1)} 秒`,
                isPresetMinSceneLen(detector, minSceneLen),
              )}
              onChange={(v) => {
                setCustomSensitivity(true);
                setMinSceneLen(v);
              }}
            />
          </div>
        )}
        {sceneWarn && <p className="preview-banner-warn compact">{sceneWarn}</p>}
      </div>

      <label className="auto-cut-toggle">
        <input
          type="checkbox"
          checked={autoCutDirect}
          onChange={(e) => setAutoCutDirect(e.target.checked)}
        />
        <span>切分后直接导出（跳过预览确认）</span>
      </label>
    </div>
  );

  const aside = (
    <div className="aside-stack auto-aside-mock auto-aside-design">
      <section className="aside-card auto-task-card compact">
        <div className="auto-task-card-head compact">
          <div className="aside-card-title">当前任务</div>
          <span
            className={`task-badge design${showResults || previewScenes.length > 0 ? " done" : detectLoading || showStatus ? " running" : " pending"}`}
          >
            {taskStatusLabel}
          </span>
        </div>
        <div className="auto-task-meta-inline">
          <span className="meta-chip">
            <em>算法</em>
            {detectorDisplayName(progressDetector)}
          </span>
          <span className="meta-chip">
            <em>结果</em>
            {previewScenes.length || results.length
              ? `${previewScenes.length || results.length} 段`
              : "—"}
          </span>
          {(detectLoading || showStatus || showResults || previewScenes.length > 0) && (
            <span className="meta-chip">
              <em>耗时</em>
              {formatElapsed(taskElapsedSec)}
            </span>
          )}
        </div>
        {showTaskProgress ? (
          <div className="auto-task-progress-row compact">
            <TaskProgressBar
              percent={progressPct}
              style={progressStyle}
              tone={progressTone}
              label={progressLabel}
              meta={progressMeta}
              compact
              phases={
                progressStyle === "phased" || progressStyle === "combo"
                  ? DETECT_PIPELINE_PHASES
                  : undefined
              }
            />
          </div>
        ) : (
          <p className="auto-task-idle-hint">选择算法与参数后，点击「开始自动切分」</p>
        )}
      </section>

      {selectedScene && (
        <section className="aside-card auto-selected-card design compact">
          <div className="auto-selected-head compact">
            <div className="aside-card-title">当前选中</div>
            <div className="auto-selected-nav compact">
              <button
                type="button"
                className="secondary icon-only"
                disabled={selectedSceneIndex <= 0}
                onClick={goPrevScene}
                title="上一片段"
              >
                ‹
              </button>
              <button
                type="button"
                className="secondary icon-only"
                disabled={selectedSceneIndex >= previewScenes.length - 1}
                onClick={goNextScene}
                title="下一片段"
              >
                ›
              </button>
            </div>
          </div>
          <div className="auto-selected-body design compact">
            <div className="selected-summary">
              <strong className="selected-range design">
                {formatTime(selectedScene.start_time)} –{" "}
                {formatTime(selectedScene.end_time)}
              </strong>
              <span className="selected-duration design">
                {formatTime(selectedDuration)}
              </span>
              <span className="auto-scene-desc design">
                {segmentListLabel(
                  progressDetector,
                  selectedScene,
                  selectedSceneIndex,
                )}
              </span>
            </div>
            <div className="auto-selected-actions">
              <button
                type="button"
                className="btn-detect btn-play-segment"
                onClick={() => selectPreviewScene(selectedSceneIndex)}
              >
                ▶ 播放
              </button>
              <button
                type="button"
                className="btn-detect secondary-outline btn-merge-prev"
                disabled={showResults || selectedSceneIndex <= 0}
                title={
                  selectedSceneIndex <= 0
                    ? "已是第一段，无法与上一段合并"
                    : "将当前片段与上一个片段合并成一段"
                }
                onClick={mergeSceneWithPrev}
              >
                并入上一段
              </button>
              <button
                type="button"
                className="btn-detect secondary-outline btn-delete-segment"
                disabled={showResults || previewScenes.length === 0}
                title="删除当前选中的片段"
                onClick={() => deletePreviewScene(selectedSceneIndex)}
              >
                删除本段
              </button>
            </div>
          </div>
        </section>
      )}

      <section className="aside-card aside-card-flex auto-list-card">
        <div className="auto-list-card-head">
          <div className="aside-card-title">
            {showResults ? "切割结果" : "场景 / 片段列表"} ({allCount})
          </div>
          <div className="auto-list-head-actions">
            {showResults && (previewScenes.length > 0 || results.length > 0) && (
              <button
                type="button"
                className="secondary linkish"
                onClick={resumeSceneEditing}
                title="返回场景列表，可调整分镜后重新切分"
              >
                返回场景列表
              </button>
            )}
            <select
              className="auto-list-sort"
              value={listSort}
              aria-label="排序"
              onChange={(e) => {
                setListSort(e.target.value as ListSort);
                setListPage(0);
              }}
            >
              <option value="time">按时间</option>
              <option value="duration">按时长</option>
            </select>
          </div>
        </div>
        {!showResults && (
          <div className="list-filter-tabs design">
            <button
              type="button"
              className={listFilter === "all" ? "active" : ""}
              onClick={() => setListFilter("all")}
            >
              全部 ({allCount})
            </button>
            <button
              type="button"
              className={listFilter === "selected" ? "active" : ""}
              onClick={() => setListFilter("selected")}
            >
              已选 ({selectedCount || (selectedSceneIndex >= 0 ? 1 : 0)})
            </button>
            <button
              type="button"
              className={listFilter === "review" ? "active" : ""}
              onClick={() => setListFilter("review")}
            >
              需检查 ({reviewCount})
            </button>
            <button
              type="button"
              className={listFilter === "unselected" ? "active" : ""}
              onClick={() => setListFilter("unselected")}
            >
              未选 ({unselectedCount})
            </button>
          </div>
        )}

        {!showResults && previewScenes.length > 0 && (
          <div className="auto-list-select-bar">
            <span className="auto-list-select-count">
              已勾选 {checkedScenes.size} / {previewScenes.length}
            </span>
            <div className="auto-list-select-actions">
              {listFilter !== "all" && filteredIndices.length > 0 && (
                <button
                  type="button"
                  className="secondary linkish"
                  onClick={() => toggleSelectFilteredScenes(filteredIndices)}
                >
                  {filteredAllChecked ? "取消当前筛选" : "勾选当前筛选"}
                </button>
              )}
              <button
                type="button"
                className="secondary linkish"
                onClick={toggleSelectAllScenes}
              >
                {allScenesChecked ? "取消全选" : "全选全部"}
              </button>
            </div>
          </div>
        )}

        {!showResults && previewScenes.length === 0 ? (
          <p className="hint-text result-preview-empty">
            切分完成后场景会显示在这里
          </p>
        ) : showResults && results.length === 0 ? (
          <p className="hint-text result-preview-empty">暂无结果</p>
        ) : !showResults && previewListItems.length === 0 ? (
          <p className="hint-text result-preview-empty">当前筛选下暂无条目</p>
        ) : (
          <>
            <div className="auto-scene-table-wrap design">
              <table className="auto-scene-table design">
                <thead>
                  <tr>
                    <th aria-label="播放" className="col-play" />
                    <th>#</th>
                    <th>缩略图</th>
                    <th>时间范围</th>
                    <th>时长</th>
                    <th aria-label="操作" />
                    <th aria-label="选择" className="col-check">
                      <input
                        type="checkbox"
                        title={
                          filteredAllChecked
                            ? "取消勾选当前列表"
                            : "勾选当前列表"
                        }
                        checked={filteredAllChecked}
                        ref={(el) => {
                          if (el) el.indeterminate = filteredSomeChecked;
                        }}
                        onChange={() => {
                          if (showResults) toggleSelectAllResults();
                          else toggleSelectFilteredScenes(filteredIndices);
                        }}
                      />
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {showResults
                    ? (pagedList as PreviewScene[]).map((seg, i) => {
                        const globalIndex = pageOffset + i;
                        const start = seg.start_time;
                        const end = seg.end_time;
                        const dur = end - start;
                        const src = segmentPreviewUrl(seg);
                        const thumb = segmentThumbUrl(seg);
                        const playable = canPreviewSegment(seg);
                        return (
                          <tr key={`${seg.id}-${globalIndex}`}>
                            <td className="col-play">
                              <button
                                type="button"
                                className="scene-row-play"
                                disabled={!playable || !src}
                                title={playable ? "预览播放" : "未就绪"}
                                onClick={() => {
                                  if (!src) return;
                                  setResultPreview({
                                    title: `片段 ${String(seg.index).padStart(2, "0")}`,
                                    url: src,
                                    range: `${formatTime(start)} – ${formatTime(end)}`,
                                  });
                                }}
                              >
                                ▶
                              </button>
                            </td>
                            <td>{String(seg.index).padStart(2, "0")}</td>
                            <td>
                              <button
                                type="button"
                                className="auto-scene-thumb cut-thumb-btn"
                                disabled={!playable || !src}
                                title={playable ? "预览播放切割成品" : "未就绪"}
                                onClick={() => {
                                  if (!src) return;
                                  setResultPreview({
                                    title: `片段 ${String(seg.index).padStart(2, "0")}`,
                                    url: src,
                                    range: `${formatTime(start)} – ${formatTime(end)}`,
                                  });
                                }}
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
                              </button>
                            </td>
                            <td className="mono-time">
                              {formatTime(start)} – {formatTime(end)}
                            </td>
                            <td>{formatTime(dur)}</td>
                            <td onClick={(e) => e.stopPropagation()}>
                              <div className="auto-result-actions">
                                <button
                                  type="button"
                                  className="secondary linkish"
                                  disabled={!playable || !src}
                                  onClick={() => {
                                    if (!src) return;
                                    setResultPreview({
                                      title: `片段 ${String(seg.index).padStart(2, "0")}`,
                                      url: src,
                                      range: `${formatTime(start)} – ${formatTime(end)}`,
                                    });
                                  }}
                                >
                                  预览播放
                                </button>
                              </div>
                            </td>
                            <td
                              className="col-check"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <input
                                type="checkbox"
                                checked={checkedScenes.has(globalIndex)}
                                onChange={() => toggleSceneCheck(globalIndex)}
                              />
                            </td>
                          </tr>
                        );
                      })
                    : (
                        pagedList as { seg: PreviewScene; globalIndex: number }[]
                      ).map(({ seg, globalIndex }) => {
                        const start = seg.start_time;
                        const end = seg.end_time;
                        const dur = end - start;
                        const selected = selectedSceneIndex === globalIndex;
                        return (
                          <tr
                            key={`${start}-${end}-${globalIndex}`}
                            className={selected ? "selected" : undefined}
                            onClick={() => selectPreviewScene(globalIndex)}
                          >
                            <td
                              className="col-play"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <button
                                type="button"
                                className="scene-row-play"
                                title="播放该片段"
                                onClick={() => selectPreviewScene(globalIndex)}
                              >
                                ▶
                              </button>
                            </td>
                            <td>{String(globalIndex + 1).padStart(2, "0")}</td>
                            <td>
                              <div
                                className="auto-scene-thumb"
                                style={filmstripThumbStyle(start)}
                              />
                            </td>
                            <td className="mono-time">
                              {formatTime(start)} – {formatTime(end)}
                            </td>
                            <td>{formatTime(dur)}</td>
                            <td onClick={(e) => e.stopPropagation()}>
                              <button
                                type="button"
                                className="scene-row-delete"
                                title="删除该片段"
                                onClick={() => deletePreviewScene(globalIndex)}
                              >
                                删除
                              </button>
                            </td>
                            <td
                              className="col-check"
                              onClick={(e) => e.stopPropagation()}
                            >
                              <input
                                type="checkbox"
                                checked={checkedScenes.has(globalIndex)}
                                onChange={() => toggleSceneCheck(globalIndex)}
                              />
                            </td>
                          </tr>
                        );
                      })}
                </tbody>
              </table>
            </div>
            {activeListLength > 0 && (
              <div className="auto-list-pagination design">
                <div className="auto-list-pagination-nav">
                  <button
                    type="button"
                    className="secondary page-btn"
                    disabled={safeListPage <= 0}
                    onClick={() => setListPage((p) => Math.max(0, p - 1))}
                  >
                    ‹
                  </button>
                  {Array.from({ length: previewTotalPages }, (_, n) => n)
                    .filter(
                      (n) =>
                        previewTotalPages <= 5 ||
                        Math.abs(n - safeListPage) <= 1 ||
                        n === 0 ||
                        n === previewTotalPages - 1,
                    )
                    .map((n, idx, arr) => (
                      <span key={n} className="auto-page-group">
                        {idx > 0 && arr[idx - 1] !== n - 1 && (
                          <span className="page-ellipsis">…</span>
                        )}
                        <button
                          type="button"
                          className={`secondary page-num${n === safeListPage ? " active" : ""}`}
                          onClick={() => setListPage(n)}
                        >
                          {n + 1}
                        </button>
                      </span>
                    ))}
                  <button
                    type="button"
                    className="secondary page-btn"
                    disabled={safeListPage >= previewTotalPages - 1}
                    onClick={() =>
                      setListPage((p) =>
                        Math.min(previewTotalPages - 1, p + 1),
                      )
                    }
                  >
                    ›
                  </button>
                </div>
                <label className="auto-page-size">
                  <select
                    value={listPageSize}
                    onChange={(e) => {
                      setListPageSize(Number(e.target.value));
                      setListPage(0);
                    }}
                  >
                    <option value={10}>10 条/页</option>
                    <option value={20}>20 条/页</option>
                    <option value={50}>50 条/页</option>
                  </select>
                </label>
              </div>
            )}
          </>
        )}
      </section>

      {!showResults && (
        <section className="aside-card confirm-actions-card design">
          <p className="confirm-actions-hint">
            {cutFailed
              ? "上次切割失败，可调整分镜后再次确认切分（将重新切割）"
              : cutDoneOnce
                ? "已切割过：调整分镜后可再次确认切分，将覆盖生成新结果"
                : checkedScenes.size > 0
                  ? `将切割已勾选的 ${checkedScenes.size} 个片段`
                  : previewScenes.length > 0
                    ? `未勾选时将切割全部 ${previewScenes.length} 个片段`
                    : "切分完成后可在此确认切割"}
          </p>
          <button
            type="button"
            className={`btn-detect btn-confirm confirm-stack-btn${confirmLoading ? " loading" : ""}`}
            disabled={!canConfirm || confirmLoading || detectLoading}
            onClick={handleConfirm}
          >
            {confirmLoading
              ? "切割中..."
              : cutFailed || cutDoneOnce
                ? checkedScenes.size > 0
                  ? `重新切分 (${checkedScenes.size})`
                  : "重新切分"
                : checkedScenes.size > 0
                  ? `确认切分 (${checkedScenes.size})`
                  : "确认切分"}
          </button>
        </section>
      )}

      {showResults && results.length > 0 && (
        <section className="aside-card download-actions-card design">
          <div className="aside-card-title">获取切片 OSS 地址</div>
          <p className="download-actions-hint">
            将片段推送到媒资切片区。勾选要保存的片段；未勾选则保存全部。也可返回场景列表重新拆分。
          </p>
          <button
            type="button"
            className="secondary full-width"
            style={{ marginBottom: 8 }}
            onClick={resumeSceneEditing}
          >
            返回场景列表重新拆分
          </button>
          <div className="download-select-row">
            <button
              type="button"
              className="secondary linkish"
              onClick={toggleSelectAllResults}
            >
              {checkedScenes.size >= sortedResults.length ? "取消全选" : "全选"}
            </button>
            <span className="download-selected-count">
              已选 {checkedDownloadCount} / {results.length}
            </span>
          </div>
          <div className="download-action-stack library-save-actions">
            <button
              type="button"
              className="btn-detect download-stack-btn"
              disabled={!taskId || results.length === 0 || publishing}
              onClick={() => void handlePublishToLibrary()}
            >
              {publishing
                ? "保存中…"
                : checkedDownloadCount > 0 &&
                    checkedDownloadCount < results.length
                  ? `返回选中 OSS 地址（${checkedDownloadCount}）`
                  : `返回全部 OSS 地址（${results.length}）`}
            </button>
          </div>
        </section>
      )}
    </div>
  );

  return (
    <div className={`panel panel-auto${active ? " active" : ""}`}>
      <div className="editor-deck detect-deck auto-deck-mock" id="auto-card">
        {!timelineMount && sceneTimelineNode}

        <div className="settings-card auto-settings-mock auto-settings-flat">
          {algoPickerNode}
          <div className="auto-settings-body">{settingsFlatPanel}</div>
          <div className="auto-settings-footer auto-settings-footer-compact">
            <button
              type="button"
              className={`btn-detect${detectLoading ? " loading" : ""}`}
              disabled={detectLoading || confirmLoading}
              onClick={handleDetect}
            >
              {detectButtonLabel}
            </button>
          </div>
        </div>

        {showPreviewBanner && (
          <div className="preview-banner compact">
            <div className="preview-banner-title">✓ 切分完成</div>
            <div className="preview-banner-desc">
              共 <strong>{previewScenes.length}</strong>{" "}
              个片段。可预览、合并或删除；勾选后「确认切分」仅切割选中项，未勾选则切割全部。
            </div>
          </div>
        )}
      </div>
      {active &&
        timelineMount &&
        createPortal(sceneTimelineNode, timelineMount)}
      <PlayerSelectionOverlay
        mountId={playerMountId}
        active={active}
        duration={duration}
        currentTime={player.currentTime}
        start={selectedScene?.start_time ?? null}
        end={selectedScene?.end_time ?? null}
        showBadge={Boolean(selectedScene)}
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
}
