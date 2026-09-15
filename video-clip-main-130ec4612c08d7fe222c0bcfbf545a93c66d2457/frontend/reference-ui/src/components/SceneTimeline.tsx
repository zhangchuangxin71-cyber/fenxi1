import { useCallback, useEffect, useRef, useState } from "react";
import { useTimelineFilmstrip } from "../hooks/useFilmstrip";
import { useTimelineZoom } from "../hooks/useTimelineZoom";
import { useWaveform } from "../hooks/useWaveform";
import { formatTime, roundTime } from "../utils/format";
import { playheadLeftCss, timeToPct } from "../utils/timelineScale";
import { WaveformLane } from "./WaveformLane";

export interface SceneRange {
  start_time: number;
  end_time: number;
  summary?: string | null;
}

interface Props {
  videoId?: string | null;
  duration: number;
  currentTime: number;
  scenes: SceneRange[];
  selectedIndex: number;
  onSeek: (time: number, pause?: boolean) => void;
  onSelectScene: (index: number) => void;
  onPlay?: () => void;
  /** @deprecated 改用 videoId，由时间轴按可视区拉取 */
  filmstripUrl?: string;
  filmstripLoading?: boolean;
  showLegend?: boolean;
  /** 语义分镜等场景较少时显示「场景 N」；画面/抗闪检测场景多时仅显示切分边界 */
  showSceneLabels?: boolean;
  /** 预览阶段允许拖动选中片段起止边界 */
  editable?: boolean;
  onSceneBoundsChange?: (index: number, start: number, end: number) => void;
  /** 仅当前激活面板才拉取胶片/波形 */
  enableMedia?: boolean;
}

type HandleDragState = {
  mode: "start" | "end";
  index: number;
  origStart: number;
  origEnd: number;
};

function findSceneIndexAtTime(scenes: SceneRange[], t: number): number {
  if (!scenes.length) return -1;
  for (let i = 0; i < scenes.length; i++) {
    const seg = scenes[i];
    const isLast = i === scenes.length - 1;
    if (t >= seg.start_time && (t < seg.end_time || (isLast && t <= seg.end_time))) {
      return i;
    }
  }
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < scenes.length; i++) {
    const mid = (scenes[i].start_time + scenes[i].end_time) / 2;
    const d = Math.abs(t - mid);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

export function SceneTimeline({
  videoId,
  duration,
  currentTime,
  scenes,
  selectedIndex,
  onSeek,
  onSelectScene,
  onPlay,
  filmstripUrl: filmstripUrlProp,
  filmstripLoading: filmstripLoadingProp,
  showLegend = true,
  showSceneLabels = false,
  editable = false,
  onSceneBoundsChange,
  enableMedia = true,
}: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [scrubbing, setScrubbing] = useState(false);
  const [handleDrag, setHandleDrag] = useState<HandleDragState | null>(null);
  const scrubRef = useRef<{
    active: boolean;
    moved: boolean;
    startX: number;
    sceneIndex: number;
  } | null>(null);
  const handleDragRef = useRef<HandleDragState | null>(null);
  handleDragRef.current = handleDrag;

  const {
    scrollRef,
    zoom,
    viewLabel,
    atMin,
    atMax,
    ticks,
    fitZoom,
    zoomIn,
    zoomOut,
    refreshTickRange,
    ensureRangeVisible,
  } = useTimelineZoom(duration, currentTime);

  // 选中场景变化时，浅蓝选区带动底部横向滚动条跟随
  const selectedScene = selectedIndex >= 0 ? scenes[selectedIndex] : null;
  useEffect(() => {
    if (!selectedScene) return;
    ensureRangeVisible(selectedScene.start_time, selectedScene.end_time);
  }, [
    selectedIndex,
    selectedScene?.start_time,
    selectedScene?.end_time,
    ensureRangeVisible,
  ]);

  const filmstrip = useTimelineFilmstrip(videoId, duration, enableMedia);
  const waveform = useWaveform(videoId, duration, enableMedia);
  const filmstripUrl = filmstrip.url || filmstripUrlProp || "";
  const filmstripLoading =
    !filmstripUrl && (filmstrip.loading || Boolean(filmstripLoadingProp));
  const cellCount = Math.max(1, filmstrip.count || 12);

  const pctToTime = useCallback(
    (clientX: number) => {
      const rect = trackRef.current?.getBoundingClientRect();
      if (!rect || !duration) return 0;
      const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return roundTime(pct * duration);
    },
    [duration],
  );

  useEffect(() => {
    if (!scrubbing) return;

    const onMove = (e: MouseEvent) => {
      const s = scrubRef.current;
      if (!s?.active) return;
      if (!s.moved && Math.abs(e.clientX - s.startX) > 3) {
        s.moved = true;
      }
      onSeek(pctToTime(e.clientX), true);
    };

    const onUp = (e: MouseEvent) => {
      const s = scrubRef.current;
      scrubRef.current = null;
      setScrubbing(false);
      if (!s) return;
      const t = pctToTime(e.clientX);
      onSeek(t, true);
      if (scenes.length > 0) {
        const idx =
          s.sceneIndex >= 0 ? s.sceneIndex : findSceneIndexAtTime(scenes, t);
        if (idx >= 0) onSelectScene(idx);
      } else if (!s.moved && s.sceneIndex >= 0) {
        onSelectScene(s.sceneIndex);
      }
    };

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [scrubbing, onSeek, onSelectScene, pctToTime, scenes]);

  useEffect(() => {
    if (!handleDrag || !onSceneBoundsChange) return;

    const onMove = (e: MouseEvent) => {
      const d = handleDragRef.current;
      if (!d || !duration) return;
      const seg = scenes[d.index];
      if (!seg) return;
      const minStart =
        d.index > 0 ? scenes[d.index - 1].end_time : 0;
      const maxEnd =
        d.index < scenes.length - 1
          ? scenes[d.index + 1].start_time
          : duration;
      const t = pctToTime(e.clientX);
      if (d.mode === "start") {
        const newStart = roundTime(
          Math.max(minStart, Math.min(t, d.origEnd - 0.1)),
        );
        onSceneBoundsChange(d.index, newStart, d.origEnd);
        onSeek(newStart, true);
      } else {
        const newEnd = roundTime(
          Math.min(maxEnd, Math.max(t, d.origStart + 0.1)),
        );
        onSceneBoundsChange(d.index, d.origStart, newEnd);
        onSeek(newEnd, true);
      }
    };

    const onUp = () => setHandleDrag(null);

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [duration, handleDrag, onSceneBoundsChange, onSeek, pctToTime, scenes]);

  const onHandleDown = (
    e: React.MouseEvent,
    index: number,
    mode: "start" | "end",
  ) => {
    if (!editable || !onSceneBoundsChange || e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    const seg = scenes[index];
    if (!seg) return;
    setHandleDrag({
      mode,
      index,
      origStart: seg.start_time,
      origEnd: seg.end_time,
    });
  };

  const onScrubAreaDown = (e: React.MouseEvent) => {
    if (!duration || e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest(".scene-handle")) return;
    e.preventDefault();
    const block = target.closest(".scene-block") as HTMLElement | null;
    const sceneIndex = block ? Number(block.dataset.index) : -1;
    scrubRef.current = {
      active: true,
      moved: false,
      startX: e.clientX,
      sceneIndex: Number.isFinite(sceneIndex) ? sceneIndex : -1,
    };
    setScrubbing(true);
    onSeek(pctToTime(e.clientX), true);
  };

  const playheadLeft = playheadLeftCss(currentTime, duration);

  return (
    <div className={`scene-timeline scene-timeline-mock${scrubbing ? " scrubbing" : ""}`}>
      <div className="scene-timeline-toolbar">
        <span className="scene-timeline-title">
          时间轴
          {scenes.length > 0 && (
            <em className="scene-timeline-hint">
              {showSceneLabels
                ? ` · ${scenes.length} 个场景`
                : ` · ${scenes.length} 个切点，点击缩略图或右侧列表预览`}
            </em>
          )}
        </span>
        <div className="scene-timeline-tools">
          <div className="timeline-zoom">
            <button
              type="button"
              className="secondary timeline-zoom-btn"
              title="缩小（看到更长时间）"
              disabled={atMax}
              onClick={zoomOut}
            >
              −
            </button>
            <button
              type="button"
              className="secondary timeline-zoom-btn timeline-zoom-label"
              title="显示全片"
              onClick={fitZoom}
            >
              {viewLabel}
            </button>
            <button
              type="button"
              className="secondary timeline-zoom-btn"
              title="放大（Ctrl+滚轮）"
              disabled={atMin}
              onClick={zoomIn}
            >
              +
            </button>
          </div>
          <button
            type="button"
            className="scene-timeline-play-btn"
            title="播放"
            onClick={() => onPlay?.()}
          >
            ▶ 播放
          </button>
        </div>
      </div>

      <div
        className="scene-timeline-scroll"
        ref={scrollRef}
        onScroll={refreshTickRange}
      >
        <div
          className="scene-timeline-inner"
          style={{ width: `${zoom * 100}%` }}
        >
          <div className="scene-ruler" aria-hidden>
            {ticks.map((t) => (
              <span
                key={t}
                className="scene-ruler-mark"
                style={{ left: `${timeToPct(t, duration)}%` }}
              >
                {formatTime(t)}
              </span>
            ))}
          </div>

          <div
            className="scene-scrub-area"
            ref={trackRef}
            onMouseDown={onScrubAreaDown}
            title="按住拖动可实时定位原片"
          >
            <div
              className="scene-playhead-line"
              style={{ left: playheadLeft }}
            />

            {showSceneLabels && scenes.length > 0 && (
              <div className="scene-label-lane" aria-hidden>
                {scenes.map((seg, i) => {
                  const left = timeToPct(seg.start_time, duration);
                  const width = timeToPct(
                    seg.end_time - seg.start_time,
                    duration,
                  );
                  const selected = i === selectedIndex;
                  const label = seg.summary?.trim()
                    ? seg.summary.trim().slice(0, 12)
                    : `场景 ${i + 1}`;
                  return (
                    <span
                      key={`label-${seg.start_time}-${i}`}
                      className={`scene-label-tag${selected ? " selected" : ""}`}
                      style={{
                        left: `${left}%`,
                        width: `${Math.max(width, 1.2)}%`,
                      }}
                    >
                      {label}
                    </span>
                  );
                })}
              </div>
            )}

            <div className={`scene-lane${showSceneLabels ? "" : " scene-lane-compact"}`}>
              {scenes.length === 0 ? (
                <p className="scene-lane-empty">切分完成后将在此显示场景分段</p>
              ) : (
                scenes.map((seg, i) => {
                  const left = timeToPct(seg.start_time, duration);
                  const width = timeToPct(
                    seg.end_time - seg.start_time,
                    duration,
                  );
                  const selected = i === selectedIndex;
                  return (
                    <div
                      key={`${seg.start_time}-${seg.end_time}-${i}`}
                      role="button"
                      tabIndex={0}
                      data-index={i}
                      className={`scene-block${selected ? " selected" : ""}`}
                      style={{
                        left: `${left}%`,
                        width: `${Math.max(width, 0.45)}%`,
                      }}
                      title={
                        seg.summary
                          ? `${formatTime(seg.start_time)} – ${formatTime(seg.end_time)}: ${seg.summary}`
                          : `${formatTime(seg.start_time)} – ${formatTime(seg.end_time)}`
                      }
                    >
                      {selected && editable && onSceneBoundsChange && (
                        <>
                          <span
                            className="scene-handle scene-handle-start"
                            onMouseDown={(e) => onHandleDown(e, i, "start")}
                          />
                          <span
                            className="scene-handle scene-handle-end"
                            onMouseDown={(e) => onHandleDown(e, i, "end")}
                          />
                        </>
                      )}
                      {showSceneLabels && (
                        <span className="scene-block-label">
                          {selected
                            ? seg.summary?.trim()
                              ? seg.summary.trim().slice(0, 8)
                              : `场景 ${i + 1}`
                            : String(i + 1).padStart(2, "0")}
                        </span>
                      )}
                    </div>
                  );
                })
              )}
            </div>

            <div
              className={`timeline scene-filmstrip-track${filmstripUrl ? " has-filmstrip" : ""}`}
            >
              <div className="timeline-media-stack scene-media-stack">
                <div className="timeline-track timeline-video-track">
                  <div className="timeline-lane-label" aria-hidden>
                    视频
                  </div>
                  {filmstripUrl ? (
                    <div
                      className="timeline-filmstrip timeline-filmstrip-window"
                      style={{
                        left: "0%",
                        width: "100%",
                        backgroundImage: `url(${filmstripUrl})`,
                        ["--filmstrip-cells" as string]: String(cellCount),
                      }}
                      aria-hidden
                    />
                  ) : (
                    filmstripLoading && (
                      <div className="timeline-filmstrip-loading" aria-hidden>
                        生成缩略图…
                      </div>
                    )
                  )}
                  {!showSceneLabels &&
                    scenes.map((seg, i) =>
                      i > 0 ? (
                        <div
                          key={`cut-${seg.start_time}-${i}`}
                          className="scene-cut-marker"
                          style={{
                            left: `${timeToPct(seg.start_time, duration)}%`,
                          }}
                          aria-hidden
                        />
                      ) : null,
                    )}
                  {selectedIndex >= 0 && scenes[selectedIndex] && (
                    <div
                      className="timeline-selection scene-selection"
                      style={{
                        left: `${timeToPct(scenes[selectedIndex].start_time, duration)}%`,
                        width: `${timeToPct(
                          scenes[selectedIndex].end_time -
                            scenes[selectedIndex].start_time,
                          duration,
                        )}%`,
                      }}
                    />
                  )}
                </div>
                <div className="timeline-track timeline-audio-track">
                  <div className="timeline-lane-label" aria-hidden>
                    音频
                  </div>
                  <WaveformLane
                    peaks={waveform.peaks}
                    loading={waveform.loading}
                    hasAudio={waveform.hasAudio}
                    error={waveform.error}
                  />
                  {selectedIndex >= 0 && scenes[selectedIndex] && (
                    <div
                      className="timeline-selection audio-selection"
                      style={{
                        left: `${timeToPct(scenes[selectedIndex].start_time, duration)}%`,
                        width: `${timeToPct(
                          scenes[selectedIndex].end_time -
                            scenes[selectedIndex].start_time,
                          duration,
                        )}%`,
                      }}
                    />
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {showLegend && (
        <div className="scene-timeline-legend">
          <span>
            <i className="legend-dot boundary" /> 检测边界
          </span>
          <span>
            <i className="legend-dot selected" /> 当前选中
          </span>
          <span>
            <i className="legend-dot manual-add" /> 手动添加
          </span>
          <span>
            <i className="legend-dot manual-del" /> 手动删除
          </span>
        </div>
      )}
    </div>
  );
}
