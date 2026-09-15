import { useCallback, useEffect, useRef, useState } from "react";
import type { TimeRange } from "../api/types";
import { useTimelineFilmstrip } from "../hooks/useFilmstrip";
import { useTimelineZoom } from "../hooks/useTimelineZoom";
import { useWaveform } from "../hooks/useWaveform";
import { formatTime, roundTime } from "../utils/format";
import { playheadLeftCss, timeToPct } from "../utils/timelineScale";
import { WaveformLane } from "./WaveformLane";

interface Props {
  videoId?: string | null;
  duration: number;
  currentTime: number;
  selection: { start: number | null; end: number | null };
  savedSegments: TimeRange[];
  selectedSegmentIndex?: number;
  segmentStatuses?: Array<"pending" | "done" | "deleted" | "cutting" | "exported">;
  onSeek: (time: number, pause?: boolean) => void;
  onSelectionChange: (start: number, end: number) => void;
  onSelectSegment?: (index: number) => void;
  /** @deprecated 改用 videoId，由时间轴按可视区拉取 */
  filmstripUrl?: string;
  filmstripLoading?: boolean;
  showLegend?: boolean;
  /** 仅当前激活面板才拉取胶片/波形，避免双面板同时打爆 API */
  enableMedia?: boolean;
}

type DragState =
  | { mode: "create"; anchor: number; moved: boolean }
  | { mode: "start"; origStart: number; origEnd: number }
  | { mode: "end"; origStart: number; origEnd: number };

export function Timeline({
  videoId,
  duration,
  currentTime,
  selection,
  savedSegments,
  selectedSegmentIndex = -1,
  segmentStatuses,
  onSeek,
  onSelectionChange,
  onSelectSegment,
  filmstripUrl: filmstripUrlProp,
  filmstripLoading: filmstripLoadingProp,
  showLegend = true,
  enableMedia = true,
}: Props) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const dragRef = useRef<DragState | null>(null);
  dragRef.current = drag;

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
    zoomPct,
    setZoomPct,
    refreshTickRange,
    ensureTimeVisible,
    ensureRangeVisible,
  } = useTimelineZoom(duration, currentTime);

  // 浅蓝选区 / 入点标线变化时，带动底部横向滚动条跟随
  useEffect(() => {
    if (selection.start == null) return;
    if (selection.end != null && selection.end > selection.start) {
      ensureRangeVisible(selection.start, selection.end);
    } else {
      ensureTimeVisible(selection.start);
    }
  }, [
    selection.start,
    selection.end,
    ensureRangeVisible,
    ensureTimeVisible,
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

  const hasSelection =
    selection.start != null &&
    selection.end != null &&
    selection.start < selection.end;

  const pendingInPoint =
    selection.start != null && selection.end == null;

  useEffect(() => {
    if (!drag) return;

    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d || !duration) return;
      if (d.mode === "create") {
        const current = pctToTime(e.clientX);
        const start = Math.min(d.anchor, current);
        const end = Math.max(d.anchor, current);
        setDrag({ ...d, moved: true });
        onSelectionChange(start, end);
        onSeek(current, true);
      } else if (d.mode === "start") {
        const t = pctToTime(e.clientX);
        const newStart = Math.min(t, d.origEnd - 0.1);
        onSelectionChange(newStart, d.origEnd);
        onSeek(newStart, true);
      } else if (d.mode === "end") {
        const t = pctToTime(e.clientX);
        const newEnd = Math.max(t, d.origStart + 0.1);
        onSelectionChange(d.origStart, newEnd);
        onSeek(newEnd, true);
      }
    };

    const onUp = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d) {
        setDrag(null);
        return;
      }
      if (d.mode === "create" && !d.moved) {
        onSeek(pctToTime(e.clientX));
      } else if (d.mode === "create" && d.moved) {
        const current = pctToTime(e.clientX);
        onSeek(Math.min(d.anchor, current), true);
      } else if (d.mode === "start") {
        onSeek(Math.min(pctToTime(e.clientX), d.origEnd - 0.1), true);
      } else if (d.mode === "end") {
        const end = Math.max(pctToTime(e.clientX), d.origStart + 0.1);
        onSeek(Math.max(d.origStart, end - 0.04), true);
      }
      setDrag(null);
    };

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [drag, duration, onSeek, onSelectionChange, pctToTime]);

  const onMouseDown = (e: React.MouseEvent) => {
    if (!duration) return;
    const target = e.target as HTMLElement;
    if (target.classList.contains("handle-start") && hasSelection) {
      setDrag({
        mode: "start",
        origStart: selection.start!,
        origEnd: selection.end!,
      });
      onSeek(selection.start!, false);
    } else if (target.classList.contains("handle-end") && hasSelection) {
      setDrag({
        mode: "end",
        origStart: selection.start!,
        origEnd: selection.end!,
      });
      onSeek(selection.end!, false);
    } else if (!target.closest(".timeline-handle")) {
      const t = pctToTime(e.clientX);
      setDrag({ mode: "create", anchor: t, moved: false });
      onSeek(t);
    }
    e.preventDefault();
  };

  return (
    <div
      className={`timeline timeline-workbench${filmstripUrl ? " has-filmstrip" : ""}`}
    >
      <div className="timeline-toolbar">
        <span className="timeline-toolbar-label">时间轴 · {viewLabel}</span>
        <div className="timeline-zoom timeline-zoom-slider">
          <button
            type="button"
            className="secondary timeline-zoom-btn"
            title="缩小（看到更长时间）"
            disabled={atMax}
            onClick={zoomOut}
            aria-label="缩小"
          >
            −
          </button>
          <input
            type="range"
            className="timeline-zoom-range"
            min={0}
            max={100}
            step={1}
            value={zoomPct}
            title={`缩放 ${viewLabel}（Ctrl+滚轮）`}
            onChange={(e) => setZoomPct(Number(e.target.value))}
          />
          <button
            type="button"
            className="secondary timeline-zoom-btn"
            title="放大（Ctrl+滚轮）"
            disabled={atMin}
            onClick={zoomIn}
            aria-label="放大"
          >
            +
          </button>
          <button
            type="button"
            className="secondary timeline-zoom-btn timeline-zoom-label"
            title="显示全片"
            onClick={fitZoom}
          >
            Fit
          </button>
        </div>
      </div>

      <div
        className="timeline-scroll"
        ref={scrollRef}
        onScroll={refreshTickRange}
      >
        <div className="timeline-zoom-inner" style={{ width: `${zoom * 100}%` }}>
          <div className="timeline-ruler" aria-hidden>
            {ticks.map((t) => (
              <span
                key={t}
                className="timeline-tick"
                style={{ left: `${timeToPct(t, duration)}%` }}
              >
                {formatTime(t)}
              </span>
            ))}
            <span
              className="timeline-playhead-tick"
              style={{ left: playheadLeftCss(currentTime, duration) }}
            />
          </div>

          {savedSegments.length > 0 && (
            <div className="timeline-seg-lane">
              {savedSegments.map((seg, i) => {
                const status = segmentStatuses?.[i] ?? "pending";
                const selected = selectedSegmentIndex === i;
                return (
                  <button
                    key={`${seg.start}-${seg.end}-${i}`}
                    type="button"
                    className={`timeline-seg-chip status-${status}${selected ? " selected" : ""}`}
                    style={{
                      left: `${timeToPct(seg.start, duration)}%`,
                      width: `${timeToPct(seg.end - seg.start, duration)}%`,
                    }}
                    title={`片段 ${String(i + 1).padStart(2, "0")}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectSegment?.(i);
                    }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </button>
                );
              })}
            </div>
          )}

          <div
            className="timeline-media-stack"
            ref={trackRef}
            onMouseDown={onMouseDown}
          >
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
              {!hasSelection && (
                <div
                  className="timeline-progress"
                  style={{ width: `${timeToPct(currentTime, duration)}%` }}
                />
              )}
              <div className="timeline-segments-layer">
                {savedSegments.map((seg, i) => {
                  const status = segmentStatuses?.[i] ?? "pending";
                  return (
                    <div
                      key={i}
                      className={`timeline-segment saved status-${status}${selectedSegmentIndex === i ? " selected" : ""}`}
                      style={{
                        left: `${timeToPct(seg.start, duration)}%`,
                        width: `${timeToPct(seg.end - seg.start, duration)}%`,
                      }}
                    />
                  );
                })}
              </div>
              {pendingInPoint && (
                <div
                  className="timeline-marker timeline-marker-in"
                  style={{ left: `${timeToPct(selection.start!, duration)}%` }}
                  title={`入点 ${formatTime(selection.start!)}`}
                />
              )}
              {hasSelection && (
                <div
                  className="timeline-selection"
                  style={{
                    left: `${timeToPct(selection.start!, duration)}%`,
                    width: `${timeToPct(
                      selection.end! - selection.start!,
                      duration,
                    )}%`,
                  }}
                >
                  <div
                    className="timeline-handle handle-start"
                    title="拖拽调整起点"
                  />
                  <div
                    className="timeline-handle handle-end"
                    title="拖拽调整终点"
                  />
                </div>
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
              {hasSelection && (
                <div
                  className="timeline-selection audio-selection"
                  style={{
                    left: `${timeToPct(selection.start!, duration)}%`,
                    width: `${timeToPct(
                      selection.end! - selection.start!,
                      duration,
                    )}%`,
                  }}
                />
              )}
            </div>

            {/* 跨视频+音频轨的寻址线：放在 overflow:hidden 轨道外，避免被裁糊/被把手挡住 */}
            <div
              className="timeline-playhead timeline-playhead-overlay"
              style={{ left: playheadLeftCss(currentTime, duration) }}
              aria-hidden
            />
          </div>
        </div>
      </div>

      {showLegend && (
        <div className="timeline-legend">
          <span>
            <i className="legend-dot selected" /> 当前选区
          </span>
          <span>
            <i className="legend-dot done" /> 已完成
          </span>
          <span>
            <i className="legend-dot pending" /> 待处理
          </span>
          <span>
            <i className="legend-dot exported" /> 已导出
          </span>
          <span>
            <i className="legend-dot deleted" /> 已删除
          </span>
        </div>
      )}
    </div>
  );
}
