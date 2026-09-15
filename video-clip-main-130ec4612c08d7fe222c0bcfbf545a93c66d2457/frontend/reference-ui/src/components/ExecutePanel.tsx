import { useCallback, useEffect, useMemo, useState } from "react";
import type { SegmentResponse, TaskResponse, VideoInfo } from "../api/types";
import { formatSize, formatTime } from "../utils/format";
import {
  canPreviewSegment,
  prefetchSegmentThumbs,
  segmentPreviewUrl,
  segmentThumbUrl,
} from "../utils/segmentMedia";
import { segmentDownloadFilename } from "../utils/clipNaming";
import { ClipPreviewModal } from "./ClipPreviewModal";
import { OutputSegCover } from "./OutputSegCover";
import { TaskProgressBar } from "./TaskProgressBar";

interface Props {
  videoInfo: VideoInfo | null;
  task: TaskResponse | null;
  modeLabel: string;
  onRefresh: () => void;
  onSaveAll: () => void | Promise<void>;
  onSaveSelected: (segmentIds: string[]) => void | Promise<void>;
  onSaveSegment: (seg: SegmentResponse) => void | Promise<void>;
}

function normalizeStatus(status: string | undefined): string {
  return (status || "").toLowerCase();
}

export function ExecutePanel({
  videoInfo,
  task,
  modeLabel,
  onRefresh,
  onSaveAll,
  onSaveSelected,
  onSaveSegment,
}: Props) {
  const [previewSeg, setPreviewSeg] = useState<SegmentResponse | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(() => new Set());
  const [savingSelected, setSavingSelected] = useState(false);

  const status = normalizeStatus(task?.status);
  const progress = Math.round(
    (task?.progress ?? 0) * (task?.progress && task.progress <= 1 ? 100 : 1),
  );
  const segments = task?.segments ?? [];
  const readySegs = useMemo(
    () =>
      segments.filter(
        (s) =>
          s.status === "done" ||
          Boolean(s.download_url) ||
          canPreviewSegment(s),
      ),
    [segments],
  );
  const totalDur = useMemo(
    () =>
      segments.reduce(
        (acc, s) => acc + Math.max(0, s.end_time - s.start_time),
        0,
      ),
    [segments],
  );

  const isDone = status === "done" || status === "completed" || status === "success";
  const isFailed = status === "failed" || status === "error";
  const statusLabel = isDone
    ? "已完成"
    : isFailed
      ? "失败"
      : status === "pending"
        ? "排队中"
        : status === "processing" ||
            status === "cutting" ||
            status === "running"
          ? "切割中"
          : task?.status || "—";
  const visibleSegs = segments;
  const previewSrc = previewSeg ? segmentPreviewUrl(previewSeg) : null;

  // 片段就绪后立刻预热封面，避免卡片逐张才开始抽帧
  useEffect(() => {
    if (readySegs.length === 0) return;
    prefetchSegmentThumbs(readySegs);
  }, [readySegs]);

  // 任务或片段列表变化时，去掉已不存在的勾选
  useEffect(() => {
    const valid = new Set(segments.map((s) => s.id).filter(Boolean));
    setCheckedIds((prev) => {
      const next = new Set<string>();
      for (const id of prev) {
        if (valid.has(id)) next.add(id);
      }
      return next.size === prev.size ? prev : next;
    });
  }, [segments]);

  // 任务处理中自动刷新，避免执行页进度长期过时
  useEffect(() => {
    if (!task?.id) return;
    const s = (task.status || "").toLowerCase();
    if (s !== "processing" && s !== "pending" && s !== "cutting" && s !== "running") {
      return;
    }
    const timer = window.setInterval(() => {
      onRefresh();
    }, 1500);
    return () => clearInterval(timer);
  }, [task?.id, task?.status, onRefresh]);

  const toggleCheck = useCallback((id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const allReadyChecked =
    readySegs.length > 0 && readySegs.every((s) => checkedIds.has(s.id));

  const toggleSelectAllReady = useCallback(() => {
    if (allReadyChecked) {
      setCheckedIds(new Set());
      return;
    }
    setCheckedIds(new Set(readySegs.map((s) => s.id)));
  }, [allReadyChecked, readySegs]);

  const handleSaveToLibrary = useCallback(async () => {
    if (!isDone || !task || readySegs.length === 0) return;
    const ids = readySegs
      .filter((s) => checkedIds.has(s.id))
      .map((s) => s.id);
    setSavingSelected(true);
    try {
      if (ids.length === 0 || ids.length >= readySegs.length) {
        await onSaveAll();
      } else {
        await onSaveSelected(ids);
      }
    } finally {
      setSavingSelected(false);
    }
  }, [checkedIds, isDone, onSaveAll, onSaveSelected, readySegs, task]);

  return (
    <div className="execute-page">
      <div className="execute-toolbar">
        <span className="execute-toolbar-title">切割与保存</span>
      </div>

      <div className="execute-top-grid">
        <section className="home-card execute-current-card">
          <div className="exec-current-head">
            <div>
              <h3 className="exec-filename">
                {videoInfo?.filename || "未命名视频"}
              </h3>
              <span className="exec-mode-tag">{modeLabel}</span>
            </div>
            <button
              type="button"
              className="btn-refresh-status"
              onClick={onRefresh}
            >
              ↻ 刷新状态
            </button>
          </div>
          {task && (
            <p className="exec-task-id">
              Task ID：<code>{task.id}</code>
            </p>
          )}
          <div className="exec-progress-block">
            <TaskProgressBar
              percent={Math.min(100, Math.max(0, progress))}
              style="neon"
              tone={isFailed ? "fail" : isDone ? "ok" : "busy"}
              label={
                isDone
                  ? "切割完成"
                  : isFailed
                    ? "任务失败"
                    : task?.message || "正在切割片段…"
              }
              meta={
                isDone
                  ? `${readySegs.length} 个片段`
                  : `已就绪 ${readySegs.length} / ${segments.length || "—"}`
              }
            />
            <p className="exec-progress-msg">
              {task?.message ||
                (isDone
                  ? "切割完成，可预览并获取 OSS 切片地址"
                  : isFailed
                    ? "任务失败"
                    : `正在执行：切割片段 ${readySegs.length} / ${segments.length || "—"}`)}
            </p>
          </div>
          <div className="exec-stat-row">
            <div>
              <span className="muted">片段数</span>
              <strong>{segments.length}</strong>
            </div>
            <div>
              <span className="muted">状态</span>
              <strong>{statusLabel}</strong>
            </div>
            <div>
              <span className="muted">创建时间</span>
              <strong>
                {task?.created_at
                  ? new Date(task.created_at).toLocaleString()
                  : "—"}
              </strong>
            </div>
          </div>
        </section>

        <section className="home-card execute-result-overview">
          <h3 className="home-card-title">结果总览</h3>
          <div className="result-stat-grid">
            <div className="result-stat">
              <strong>{segments.length || "—"}</strong>
              <span>总片段数</span>
            </div>
            <div className="result-stat">
              <strong>{segments.length ? formatTime(totalDur) : "—"}</strong>
              <span>总时长</span>
            </div>
            <div className="result-stat">
              <strong>MP4</strong>
              <span>输出格式</span>
            </div>
            <div className="result-stat">
              <strong>
                {videoInfo ? formatSize(videoInfo.size_bytes) : "—"}
              </strong>
              <span>源文件大小</span>
            </div>
          </div>
          <ul className="result-meta-list">
            <li>
              分辨率：
              {videoInfo
                ? `${videoInfo.width}×${videoInfo.height}`
                : "原始"}
            </li>
            <li>帧率：{videoInfo ? `${videoInfo.fps} FPS` : "—"}</li>
            <li>编码：{videoInfo?.codec || "H.264 / AAC"}</li>
          </ul>
        </section>
      </div>

      <div className="execute-mid-grid execute-mid-grid-solo">
        <section className="home-card">
          <div className="output-seg-header">
            <h3 className="home-card-title">输出片段</h3>
            {readySegs.length > 0 && (
              <div className="download-select-row">
                <button
                  type="button"
                  className="secondary linkish"
                  disabled={!isDone}
                  onClick={toggleSelectAllReady}
                >
                  {allReadyChecked ? "取消全选" : "全选就绪"}
                </button>
                <span className="download-selected-count">
                  已选 {checkedIds.size} / {readySegs.length}
                </span>
              </div>
            )}
          </div>
          <div className="output-seg-grid">
            {visibleSegs.map((seg) => {
              const src = segmentPreviewUrl(seg);
              const thumb = segmentThumbUrl(seg);
              const ready =
                canPreviewSegment(seg) ||
                seg.status === "done" ||
                Boolean(seg.download_url);
              const checked = checkedIds.has(seg.id);
              return (
                <article
                  key={seg.id}
                  className={`output-seg-card${!ready ? " processing" : ""}${checked ? " selected" : ""}`}
                >
                  <label className="output-seg-check">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!ready}
                      onChange={() => toggleCheck(seg.id)}
                    />
                    <span>选择保存</span>
                  </label>
                  <button
                    type="button"
                    className="output-seg-thumb"
                    disabled={!ready || !src}
                    onClick={() => ready && src && setPreviewSeg(seg)}
                    title={ready ? "点击预览播放" : "处理中"}
                  >
                    {ready ? (
                      <OutputSegCover
                        thumbUrl={thumb}
                        videoUrl={src}
                        aspectRatio={
                          videoInfo && videoInfo.height > 0
                            ? videoInfo.width / videoInfo.height
                            : undefined
                        }
                      />
                    ) : (
                      <div className="output-seg-ph">…</div>
                    )}
                    {ready && <span className="output-seg-play">▶ 预览播放</span>}
                  </button>
                  <div className="output-seg-meta">
                    <strong title={segmentDownloadFilename({
                      sourceFilename: videoInfo?.filename,
                      taskId: task?.id || seg.task_id,
                      index: seg.index,
                      startTime: seg.start_time,
                      endTime: seg.end_time,
                      downloadFilename: seg.download_filename,
                    })}>
                      {segmentDownloadFilename({
                        sourceFilename: videoInfo?.filename,
                        taskId: task?.id || seg.task_id,
                        index: seg.index,
                        startTime: seg.start_time,
                        endTime: seg.end_time,
                        downloadFilename: seg.download_filename,
                      })}
                    </strong>
                    <span>
                      {formatTime(seg.start_time)} – {formatTime(seg.end_time)}
                    </span>
                  </div>
                  <div className="output-seg-actions">
                    <button
                      type="button"
                      className="secondary"
                      disabled={!ready || !src}
                      onClick={() => setPreviewSeg(seg)}
                    >
                      预览播放
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={!ready}
                      onClick={() => onSaveSegment(seg)}
                    >
                      保存视频
                    </button>
                  </div>
                </article>
              );
            })}
            {!segments.length && (
              <p className="hint-text">切割开始后，输出片段将显示在这里</p>
            )}
          </div>
        </section>
      </div>

      <div className="execute-bottom-grid">
        <section className="home-card zip-download-hero library-save-hero">
          <div>
            <h3>获取切片 OSS 地址</h3>
            <p>
              将片段推送到媒资切片区。勾选要保存的片段；未勾选则保存全部。
            </p>
            <p className="hint-text">
              已选 {checkedIds.size} / {readySegs.length} 个就绪片段
            </p>
          </div>
          <div className="library-save-actions">
            <button
              type="button"
              className="btn-download-zip primary"
              disabled={!isDone || !task || readySegs.length === 0 || savingSelected}
              onClick={() => void handleSaveToLibrary()}
            >
              {savingSelected
                ? "保存中…"
                : checkedIds.size > 0 && checkedIds.size < readySegs.length
                  ? `返回选中 OSS 地址（${checkedIds.size}）`
                  : `返回全部 OSS 地址（${readySegs.length}）`}
            </button>
          </div>
        </section>
      </div>

      {previewSeg && previewSrc && (
        <ClipPreviewModal
          title={`预览 ${segmentDownloadFilename({
            sourceFilename: videoInfo?.filename,
            taskId: task?.id || previewSeg.task_id,
            index: previewSeg.index,
            startTime: previewSeg.start_time,
            endTime: previewSeg.end_time,
            downloadFilename: previewSeg.download_filename,
          })}`}
          rangeText={`${formatTime(previewSeg.start_time)} – ${formatTime(previewSeg.end_time)} · 时长 ${formatTime(Math.max(0, previewSeg.end_time - previewSeg.start_time))}`}
          src={previewSrc}
          onClose={() => setPreviewSeg(null)}
          onSave={() => onSaveSegment(previewSeg)}
          saveLabel="保存该视频"
        />
      )}
    </div>
  );
}
