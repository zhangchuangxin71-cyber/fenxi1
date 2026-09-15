/** 与后端 backend/utils/naming.py 对齐的下载文件名规则 */

const INVALID = /[\\/:*?"<>|\s]+/g;

export function sanitizeStem(filename?: string | null, maxLen = 40): string {
  const raw = (filename || "video").replace(/\.[^.]+$/, "").trim() || "video";
  const cleaned = raw.replace(INVALID, "_").replace(/^[._]+|[._]+$/g, "") || "video";
  return cleaned.slice(0, maxLen);
}

export function segmentDownloadFilename(opts: {
  sourceFilename?: string | null;
  taskId: string;
  index: number;
  startTime: number;
  endTime: number;
  /** 后端下发时优先使用 */
  downloadFilename?: string | null;
}): string {
  if (opts.downloadFilename) return opts.downloadFilename;
  const stem = sanitizeStem(opts.sourceFilename);
  const tid = (opts.taskId || "task").slice(0, 12);
  const startMs = Math.max(0, Math.round(opts.startTime * 1000));
  const endMs = Math.max(startMs, Math.round(opts.endTime * 1000));
  return `${stem}__${tid}__seg${String(opts.index).padStart(3, "0")}__${startMs}-${endMs}.mp4`;
}

export function taskZipFilename(opts: {
  sourceFilename?: string | null;
  taskId: string;
  selected?: boolean;
}): string {
  const stem = sanitizeStem(opts.sourceFilename);
  const tid = (opts.taskId || "task").slice(0, 12);
  const suffix = opts.selected ? "clips_selected" : "clips";
  return `${stem}__${tid}__${suffix}.zip`;
}
