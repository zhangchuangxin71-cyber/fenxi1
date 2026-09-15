import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import type { TabMode, TaskResponse, VideoInfo } from "./api/types";
import { deleteVideo, getTaskStatus, getVideoInfo, importFromOss, publishSegments, videoPlayUrl } from "./api/client";
import { suggestTimelineFilmstripCount } from "./hooks/useFilmstrip";
import { useToast } from "./hooks/useToast";
import { useVideoPlayer } from "./hooks/useVideoPlayer";
import { computePreviewLayout } from "./utils/previewLayout";
import { useSplitPane, useRowSplit, useLeftPaneSplit } from "./hooks/useSplitPane";
import { ToastList } from "./components/Toast";
import { ManualPanel, type ManualPanelHandle } from "./components/ManualPanel";
import { AutoPanel } from "./components/AutoPanel";
import { DESIGN_STEPS, WorkflowStepper } from "./components/WorkflowStepper";
import { HomeLanding } from "./components/HomeLanding";
import { ExecutePanel } from "./components/ExecutePanel";
import { OperationGuide } from "./components/OperationGuide";
import { BRAND_NAME } from "./brand";

type WorkPhase = "landing" | "editing" | "execute";

export default function App() {
  const { toasts, toast } = useToast();
  const videoRef = useRef<HTMLVideoElement>(null);
  const freezeCanvasRef = useRef<HTMLCanvasElement>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const mediaShellRef = useRef<HTMLDivElement>(null);
  const playbackStackRef = useRef<HTMLDivElement>(null);
  const manualPanelRef = useRef<ManualPanelHandle>(null);

  const [phase, setPhase] = useState<WorkPhase>("landing");
  const [tab, setTab] = useState<TabMode>("manual");
  const [stepIndex, setStepIndex] = useState(0);
  const [editResumeKey, setEditResumeKey] = useState(0);
  const [activeTask, setActiveTask] = useState<TaskResponse | null>(null);

  const [videoId, setVideoId] = useState<string | null>(null);
  const [videoInfo, setVideoInfo] = useState<VideoInfo | null>(null);
  const [duration, setDuration] = useState(0);
  const [fps, setFps] = useState(25);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const [ossImporting, setOssImporting] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  /** 首页：强制回到选片屏（与选模式拆开） */
  const [landingPickSource, setLandingPickSource] = useState(false);
  const landingReturnStepRef = useRef<0 | 1>(0);

  const player = useVideoPlayer({
    videoRef,
    freezeCanvasRef,
    duration,
    fps,
  });

  const {
    asideWidth,
    dragging: splitDragging,
    splitterProps,
  } = useSplitPane(workspaceRef, {
    initialAsideWidth: tab === "auto" ? 340 : 300,
    minAsideWidth: 240,
    maxAsideWidth: 520,
    minMainWidth: 420,
    storageKey: `clipnova-ws-aside-${tab}`,
  });
  /** 预览区 ↔ 中间控制面板：直接拖左侧预览宽度 */
  const {
    leftWidth: mediaLeftWidth,
    dragging: shellSplitDragging,
    splitterProps: shellSplitterProps,
  } = useLeftPaneSplit(mediaShellRef, {
    initialLeftWidth: 680,
    minLeftWidth: 320,
    minRightWidth: 240,
    maxLeftWidth: 2200,
    storageKey: `clipnova-media-left-${tab}`,
  });
  const {
    topHeight: previewPaneHeight,
    dragging: rowSplitDragging,
    splitterProps: rowSplitterProps,
  } = useRowSplit(playbackStackRef, {
    initialTopHeight: 360,
    minTopHeight: 180,
    minBottomHeight: 160,
    storageKey: "clipnova-preview-pane-h",
  });

  useEffect(() => {
    player.clearPreviewFreeze();
    player.setScenePreviewFromSegment(null);
    player.stopLoopPreview();
  }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  const applyImportedVideo = useCallback(
    async (
      data: { video_id: string; preview_url: string },
      previousVideoId: string | null,
      successMsg: string,
    ) => {
      if (previousVideoId && previousVideoId !== data.video_id) {
        deleteVideo(previousVideoId).catch(() => {});
      }

      setVideoId(data.video_id);
      setActiveTask(null);
      setEditResumeKey((k) => k + 1);
      // 换源后一律回到第一步确认，避免卡在执行/编辑空态
      setPhase("landing");
      setStepIndex(0);
      setLandingPickSource(false);

      const info = await getVideoInfo(data.video_id);
      setVideoInfo(info);
      setDuration(info.duration);
      setFps(info.fps || 25);
      // 不要信任未规范化的 preview_url；公网绝对地址直连，避免 Vite 代理弄坏视频流
      const nextUrl = videoPlayUrl(
        data.video_id,
        info.preview_url || data.preview_url,
      );
      setPreviewUrl(nextUrl);

      if (videoRef.current) {
        videoRef.current.src = nextUrl;
        player.clearPreviewFreeze();
      }

      toast(successMsg);

      const warmCount = suggestTimelineFilmstripCount(info.duration);
      const previewCount = Math.min(40, warmCount);
      const warm = (count: number) => {
        const qs = new URLSearchParams({ v: "9", count: String(count) });
        return fetch(`/api/v1/videos/${data.video_id}/filmstrip?${qs}`).catch(
          () => {},
        );
      };
      void warm(previewCount).then(() => {
        if (warmCount > previewCount) void warm(warmCount);
      });
    },
    [player, toast],
  );

  const handleImportOss = useCallback(
    async (ref: string) => {
      const previousVideoId = videoId;
      const replacing = Boolean(previousVideoId);
      try {
        player.clearPreviewFreeze();
        player.setScenePreviewFromSegment(null);
        player.stopLoopPreview();
        videoRef.current?.pause();

        setOssImporting(true);
        const looksUrl = /^https?:\/\//i.test(ref);
        const data = await importFromOss(
          looksUrl ? { oss_url: ref } : { oss_key: ref },
        );
        await applyImportedVideo(
          data,
          previousVideoId,
          replacing ? "已从 OSS 更换源片" : "已从 OSS 导入源片",
        );
      } catch (e) {
        toast(e instanceof Error ? e.message : "从 OSS 导入失败", true);
      } finally {
        setOssImporting(false);
      }
    },
    [applyImportedVideo, player, toast, videoId],
  );

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      if (!videoId || tab !== "manual" || phase !== "editing") return;
      if (
        (e.ctrlKey || e.metaKey) &&
        (e.key === "z" || e.key === "Z" || e.key === "y" || e.key === "Y")
      ) {
        e.preventDefault();
      }
      if (e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
      }
      manualPanelRef.current?.handleKey(e);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [tab, videoId, phase]);

  const hasVideo = Boolean(videoId && videoInfo);
  const replacing = hasVideo && ossImporting;

  const handleSelectMode = useCallback(
    (mode: TabMode) => {
      if (!videoId || !videoInfo) {
        toast("请先从 OSS 导入源片", true);
        return;
      }
      setTab(mode);
      setPhase("editing");
      setStepIndex(2);
    },
    [toast, videoId, videoInfo],
  );

  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  const handleStepChange = useCallback((step: number) => {
    // 只同步步骤数字；进入执行页由 enterExecute 显式驱动
    setStepIndex(step);
  }, []);

  /** 顶栏步骤条：各阶段统一点击已完成步骤回退 */
  const handleWorkflowStepClick = useCallback(
    (step: number) => {
      const leaveWorkbench = () => {
        // 离开编辑/执行时递增，面板停轮询并重置切割中 UI 态
        if (
          phaseRef.current === "editing" ||
          phaseRef.current === "execute"
        ) {
          setEditResumeKey((k) => k + 1);
        }
      };

      if (step === 0) {
        leaveWorkbench();
        setPhase("landing");
        setLandingPickSource(false);
        setStepIndex(0);
        return;
      }
      if (step === 1) {
        if (!hasVideo) return;
        leaveWorkbench();
        setPhase("landing");
        setLandingPickSource(false);
        setStepIndex(1);
        return;
      }
      if (step === 2) {
        if (!hasVideo) return;
        setEditResumeKey((k) => k + 1);
        setPhase("editing");
        setStepIndex(2);
        return;
      }
      if (step === 3 && activeTask) {
        setPhase("execute");
        setStepIndex(3);
      }
    },
    [hasVideo, activeTask],
  );

  /** 任务进度回调。仅 opts.enterExecute 时跳转执行页；轮询更新不强迫切页。 */
  const handleTaskChange = useCallback(
    (
      task: TaskResponse | null,
      opts?: { enterExecute?: boolean },
    ) => {
      setActiveTask(task);
      if (opts?.enterExecute) {
        setPhase("execute");
        setStepIndex(3);
        return;
      }
      // 已在执行页时保持在「执行切割」；用户已返回编辑则不打断
      if (task?.status === "done" && phaseRef.current === "execute") {
        setStepIndex(3);
      }
    },
    [],
  );

  const openExecuteResults = useCallback(() => {
    if (!activeTask) {
      toast("暂无切割任务", true);
      return;
    }
    setPhase("execute");
    setStepIndex(3);
  }, [activeTask, toast]);

  const refreshTask = useCallback(async () => {
    if (!activeTask?.id) return;
    try {
      const t = await getTaskStatus(activeTask.id);
      setActiveTask(t);
      if (t.status === "done") setStepIndex(3);
    } catch (e) {
      toast(e instanceof Error ? e.message : "刷新失败", true);
    }
  }, [activeTask?.id, toast]);

  const saveToLibrary = useCallback(async () => {
    if (!activeTask?.id) return;
    try {
      const result = await publishSegments(activeTask.id, {
      });
      toast(`已返回 ${result.selected.length} 个选中切片 OSS 地址`);
      await refreshTask();
    } catch (e) {
      toast(e instanceof Error ? e.message : "获取 OSS 地址失败", true);
    }
  }, [activeTask?.id, refreshTask, toast]);

  const saveSelectedToLibrary = useCallback(
    async (segmentIds: string[]) => {
      if (!activeTask?.id) return;
      if (!segmentIds.length) {
        toast("请先勾选要保存的片段", true);
        return;
      }
      try {
        const result = await publishSegments(activeTask.id, {
          segmentIds,
        });
        toast(`已返回 ${result.selected.length} 个选中切片 OSS 地址`);
        await refreshTask();
      } catch (e) {
        toast(e instanceof Error ? e.message : "保存选中片段失败", true);
      }
    },
    [activeTask?.id, refreshTask, toast],
  );

  const saveSegment = useCallback(
    async (seg: {
      id?: string;
      download_url?: string | null;
      download_filename?: string | null;
      index: number;
      start_time?: number;
      end_time?: number;
      task_id?: string;
      status?: string;
      oss_key?: string | null;
    }) => {
      const taskId = seg.task_id || activeTask?.id;
      if (!taskId || !seg.id) {
        toast("该片段尚未就绪", true);
        return;
      }
      try {
        const result = await publishSegments(taskId, {
          segmentIds: [seg.id],
        });
        toast(result.selected[0] ? "已返回该片段 OSS 地址" : "未找到该片段 OSS 地址", !result.selected[0]);
        await refreshTask();
      } catch (e) {
        toast(e instanceof Error ? e.message : "保存失败", true);
      }
    },
    [activeTask?.id, refreshTask, toast],
  );

  const landingStep: 0 | 1 =
    landingPickSource || !hasVideo ? 0 : stepIndex >= 1 ? 1 : 0;
  const displayStep =
    phase === "landing"
      ? landingStep
      : phase === "execute"
        ? Math.max(stepIndex, 3)
        : Math.max(stepIndex, 2);
  // 有进行中/已完成任务时，即使当前在前几步也可点进「切割与保存」
  const stepperMaxClickable = activeTask
    ? 3
    : hasVideo
      ? Math.max(displayStep, 1)
      : displayStep;

  const previewLayout = useMemo(
    () => computePreviewLayout(videoInfo?.width, videoInfo?.height),
    [videoInfo?.width, videoInfo?.height],
  );

  // 仅注入比例元数据（data-preview-fit / 可选 CSS 钩子），不驱动舞台外框尺寸
  const previewMetaStyle = useMemo((): CSSProperties => {
    return {
      ["--preview-aspect" as string]: previewLayout.aspect,
      ["--preview-ratio" as string]: String(previewLayout.ratio),
    };
  }, [previewLayout]);

  return (
    <div className="app-shell">
      <div className="app-main">
        <div className="app-main-top">
          <div className="app-brand-mark" aria-label={BRAND_NAME}>
            <span className="app-brand-icon" aria-hidden>
              <svg viewBox="0 0 28 28" width="28" height="28" fill="none">
                <rect width="28" height="28" rx="8" fill="url(#brandGrad)" />
                <path
                  d="M10 8.5v11l9-5.5-9-5.5Z"
                  fill="#fff"
                />
                <defs>
                  <linearGradient id="brandGrad" x1="4" y1="2" x2="24" y2="26">
                    <stop stopColor="#7c5cfc" />
                    <stop offset="1" stopColor="#5b5cff" />
                  </linearGradient>
                </defs>
              </svg>
            </span>
            <span className="app-brand-name">{BRAND_NAME}</span>
          </div>
          <div id="stepper-dock" className="global-stepper workbench-stepper">
            <WorkflowStepper
              steps={DESIGN_STEPS}
              current={displayStep}
              maxClickable={stepperMaxClickable}
              onStepClick={handleWorkflowStepClick}
            />
          </div>
          <div className="workbench-top-actions">
            {phase === "editing" &&
              hasVideo &&
              activeTask &&
              (activeTask.status === "done" ||
                activeTask.status === "processing" ||
                activeTask.status === "failed") && (
                <button
                  type="button"
                  className="header-ghost"
                  onClick={openExecuteResults}
                >
                  {activeTask.status === "done"
                    ? "查看切割结果"
                    : "查看切割进度"}
                </button>
              )}
            <button
              type="button"
              className="header-ghost header-guide-btn"
              onClick={() => setShowGuide(true)}
            >
              <span aria-hidden>💡</span>
              操作指南
            </button>
          </div>
        </div>

        <div className="app-main-body">
          {phase === "landing" && (
                <HomeLanding
                  videoInfo={videoInfo}
                  previewUrl={previewUrl}
                  ossImporting={ossImporting}
                  landingStep={landingStep}
                  forcePickSource={landingPickSource}
                  onSelectMode={handleSelectMode}
                  onImportOss={handleImportOss}
                  onContinueToMode={() => setStepIndex(1)}
                  onChangeSource={() => {
                    landingReturnStepRef.current = landingStep;
                    setLandingPickSource(true);
                    setStepIndex(0);
                  }}
                  onCancelChangeSource={() => {
                    setLandingPickSource(false);
                    setStepIndex(hasVideo ? landingReturnStepRef.current : 0);
                  }}
                  toast={toast}
                />
          )}

          {phase === "execute" && (
            <ExecutePanel
              videoInfo={videoInfo}
              task={activeTask}
              modeLabel={tab === "auto" ? "自动分镜模式" : "手动切片模式"}
              onRefresh={() => void refreshTask()}
              onSaveAll={saveToLibrary}
              onSaveSelected={saveSelectedToLibrary}
              onSaveSegment={saveSegment}
            />
          )}

          {/* Keep panels mounted so polling / portals survive phase switches */}
          {hasVideo && videoInfo && videoId && previewUrl && (
                <div
                  ref={workspaceRef}
                  className={`workspace layout-split mock-workbench split-resizable${
                    tab === "auto" ? " mock-auto" : " mock-manual"
                  }${phase !== "editing" ? " is-hidden-phase" : ""}${
                    splitDragging ? " is-splitting" : ""
                  }`}
                  id="workspace"
                  hidden={phase !== "editing"}
                  style={
                    {
                      ["--ws-aside-w" as string]: `${asideWidth}px`,
                    } as CSSProperties
                  }
                >
                  <div className="workspace-main">
                    <div
                      ref={mediaShellRef}
                      className={`card media-shell${
                        tab === "auto"
                          ? " media-shell-auto"
                          : " media-shell-manual"
                      }${shellSplitDragging ? " is-shell-splitting" : ""}`}
                      data-preview-fit={previewLayout.fit}
                      style={
                        {
                          ...previewMetaStyle,
                          ["--media-left-w" as string]: `${mediaLeftWidth}px`,
                        } as CSSProperties
                      }
                    >
                      <div className="workbench-nav">
                        <div className="workbench-title-row">
                          <h2>
                            {tab === "manual"
                              ? "手动切片工作台"
                              : "自动分镜工作台"}
                          </h2>
                          <p className="hint-text">
                            {tab === "manual"
                              ? "标记入出点，精确控制每一帧"
                              : "选择算法自动切分，预览后确认切割"}
                          </p>
                        </div>
                        <span
                          className={`mode-locked-badge${
                            tab === "auto" ? " purple" : " blue"
                          }`}
                        >
                          {tab === "manual" ? "手动切片" : "自动分镜"}
                        </span>
                      </div>

                      <div
                        ref={playbackStackRef}
                        className={`media-playback-stack${
                          rowSplitDragging ? " is-row-splitting" : ""
                        }`}
                        style={
                          {
                            ["--preview-h" as string]: `${previewPaneHeight}px`,
                            ["--timeline-h" as string]: "220px",
                          } as CSSProperties
                        }
                      >
                        <div className="player-stage" id="player-stage">
                          <video
                            ref={videoRef}
                            id="player"
                            controls
                            src={previewUrl}
                          />
                          <canvas
                            ref={freezeCanvasRef}
                            id="preview-freeze"
                            className="preview-freeze hidden"
                            aria-hidden
                          />
                          {replacing && (
                            <div className="player-replace-mask">
                              <div className="player-replace-card">
                                <span>正在从 OSS 更换源片…</span>
                              </div>
                            </div>
                          )}
                        </div>

                        <div
                          className="workspace-row-splitter"
                          title="拖拽调整预览区高度"
                          {...rowSplitterProps}
                        />

                        <div
                          id="timeline-dock"
                          className={`timeline-dock${
                            tab === "auto" || tab === "manual" ? " active" : ""
                          }`}
                        />
                      </div>

                      <div
                        className="workspace-splitter media-shell-splitter"
                        title="拖拽调整预览区宽度"
                        {...shellSplitterProps}
                      />

                      <ManualPanel
                        ref={manualPanelRef}
                        videoId={videoId}
                        duration={duration}
                        fps={fps}
                        previewUrl={previewUrl}
                        sourceFilename={videoInfo.filename}
                        aspectRatio={
                          videoInfo.height > 0
                            ? videoInfo.width / videoInfo.height
                            : undefined
                        }
                        playerMountId="player-stage"
                        playerRef={videoRef}
                        player={player}
                        toast={toast}
                        active={tab === "manual" && phase === "editing"}
                        resultMountId="result-preview-root"
                        timelineMountId="timeline-dock"
                        editResumeKey={editResumeKey}
                        onStepChange={handleStepChange}
                        onTaskChange={handleTaskChange}
                      />
                      <AutoPanel
                        videoId={videoId}
                        duration={duration}
                        previewUrl={previewUrl}
                        sourceFilename={videoInfo.filename}
                        aspectRatio={
                          videoInfo.height > 0
                            ? videoInfo.width / videoInfo.height
                            : undefined
                        }
                        playerRef={videoRef}
                        player={player}
                        toast={toast}
                        active={tab === "auto" && phase === "editing"}
                        resultMountId="result-preview-root"
                        timelineMountId="timeline-dock"
                        editResumeKey={editResumeKey}
                        onStepChange={handleStepChange}
                        onTaskChange={handleTaskChange}
                      />
                    </div>
                  </div>

                  <div
                    className="workspace-splitter"
                    title="拖拽调整片段列表宽度"
                    {...splitterProps}
                  />

                  <aside
                    className="workspace-aside"
                    id="result-preview-root"
                  >
                    <div className="aside-placeholder aside-stack">
                      <section className="aside-card">
                        <div className="aside-card-title">片段列表</div>
                        <p className="hint-text result-preview-empty">
                          标记或检测后的片段会出现在这里
                        </p>
                      </section>
                    </div>
                  </aside>
                </div>
              )}
            </div>
          </div>

      <OperationGuide open={showGuide} onClose={() => setShowGuide(false)} />

      <ToastList toasts={toasts} />
    </div>
  );
}
