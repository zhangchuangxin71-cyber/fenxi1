export function formatTime(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec)) return "--";
  const m = Math.floor(sec / 60);
  const s = (sec % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

/** 秒 → 帧号（四舍五入） */
export function timeToFrame(sec: number, fps = 25): number {
  const f = Math.max(1, fps);
  return Math.round(sec * f);
}

/** SMPTE 时码 HH:MM:SS:FF */
export function formatSmpte(
  sec: number | null | undefined,
  fps = 25,
): string {
  if (sec == null || Number.isNaN(sec) || !Number.isFinite(sec)) {
    return "--:--:--:--";
  }
  const rate = Math.max(1, Math.round(fps));
  const totalFrames = Math.max(0, Math.round(sec * rate));
  const ff = totalFrames % rate;
  const totalSecs = Math.floor(totalFrames / rate);
  const ss = totalSecs % 60;
  const totalMins = Math.floor(totalSecs / 60);
  const mm = totalMins % 60;
  const hh = Math.floor(totalMins / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hh)}:${pad(mm)}:${pad(ss)}:${pad(ff)}`;
}

/** 时长副标：MM:SS.mmm */
export function formatClockMs(sec: number | null | undefined): string {
  if (sec == null || Number.isNaN(sec) || !Number.isFinite(sec)) {
    return "--:--.---";
  }
  const m = Math.floor(Math.max(0, sec) / 60);
  const s = Math.max(0, sec) % 60;
  return `${String(m).padStart(2, "0")}:${s.toFixed(3).padStart(6, "0")}`;
}

/** 解析 `717.8` / `11:57.8` / `1:11:57.8` 为秒 */
export function parseTimeInput(str: string): number | null {
  const s = str.trim();
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) {
    const n = parseFloat(s);
    return Number.isFinite(n) ? n : null;
  }
  const hm = s.match(/^(\d+):(\d{1,2}(?:\.\d+)?)$/);
  if (hm) {
    return parseInt(hm[1], 10) * 60 + parseFloat(hm[2]);
  }
  const hms = s.match(/^(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/);
  if (hms) {
    return (
      parseInt(hms[1], 10) * 3600 +
      parseInt(hms[2], 10) * 60 +
      parseFloat(hms[3])
    );
  }
  return null;
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function roundTime(t: number): number {
  return Math.round(t * 1000) / 1000;
}

export function escapeHtml(text: string): string {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
