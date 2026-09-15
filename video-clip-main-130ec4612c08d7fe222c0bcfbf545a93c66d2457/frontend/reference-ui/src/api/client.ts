import type {
  SegmentResponse,
  TaskResponse,
  UploadResponse,
  VideoInfo,
  TimeRange,
  Detector,
  PublishSegmentsResponse,
} from "./types";

async function readError(res: Response, fallback: string): Promise<string> {
  try {
    const err = (await res.json()) as {
      message?: string;
      detail?: string | { message?: string };
      data?: { detail?: unknown };
    };
    if (typeof err.message === "string" && err.message) return err.message;
    if (typeof err.detail === "string" && err.detail) return err.detail;
    if (err.detail && typeof err.detail === "object" && err.detail.message) {
      return String(err.detail.message);
    }
    return fallback;
  } catch {
    return fallback;
  }
}

type Envelope<T> = { code: number; message: string; data: T };

/**
 * 规范化媒体 URL。
 * - 剥掉误配 API_PUBLIC_BASE==http://... 产生的前导 '='
 * - 后端生成的任意绝对 /api/... 地址都改写为当前前端同源路径（走 Vite/反代）
 * - OSS/CDN 绝对媒体地址保持直连
 */
export function toProxyMediaUrl(url: string | null | undefined): string {
  let raw = (url || "").trim();
  while (raw.startsWith("=")) raw = raw.slice(1).trim();
  if (!raw) return "";
  if (raw.startsWith("/")) return raw;
  try {
    const u = new URL(raw);
    if (u.pathname.startsWith("/api/")) {
      return `${u.pathname}${u.search}${u.hash}`;
    }
  } catch {
    /* ignore */
  }
  return raw;
}

/** 源片播放地址：优先规范化后端 preview_url，否则按 video_id 拼同源路径 */
export function videoPlayUrl(
  videoId: string,
  previewUrl?: string | null,
): string {
  const fromApi = toProxyMediaUrl(previewUrl);
  if (fromApi) return fromApi;
  return `/api/v1/videos/${videoId}/media`;
}

async function requestData<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) throw new Error(await readError(res, `请求失败 (${res.status})`));
  const body = (await res.json()) as Envelope<T> | T;
  if (
    body &&
    typeof body === "object" &&
    "code" in body &&
    "data" in body &&
    typeof (body as Envelope<T>).code === "number"
  ) {
    const env = body as Envelope<T>;
    if (env.code !== 0) {
      throw new Error(env.message || `业务错误 (${env.code})`);
    }
    return env.data;
  }
  return body as T;
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

/** v1 Video → 前端 VideoInfo（id = video_id） */
function mapVideo(data: {
  video_id: string;
  filename: string;
  duration?: number | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  codec?: string | null;
  size_bytes?: number | null;
  preview_url?: string | null;
  has_audio?: boolean;
  oss_key?: string | null;
  oss_url?: string | null;
  status?: string;
}): VideoInfo {
  return {
    id: data.video_id,
    filename: data.filename,
    duration: Number(data.duration || 0),
    width: Number(data.width || 0),
    height: Number(data.height || 0),
    fps: Number(data.fps || 0),
    codec: data.codec || "",
    size_bytes: Number(data.size_bytes || 0),
    preview_url: toProxyMediaUrl(data.preview_url),
    has_audio: data.has_audio,
    oss_key: data.oss_key,
  };
}

export async function importFromOss(payload: {
  oss_key?: string;
  oss_url?: string;
}): Promise<UploadResponse> {
  const submitted = await requestData<{
    video_id: string;
    status: string;
    oss_key?: string | null;
  }>("/api/v1/videos/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const videoId = submitted.video_id;
  const deadline = Date.now() + 10 * 60 * 1000;
  while (Date.now() < deadline) {
    const video = await requestData<{
      video_id: string;
      status: string;
      message?: string;
      filename: string;
      preview_url?: string | null;
    }>(`/api/v1/videos/${videoId}`);
    if (video.status === "ready") {
      return {
        video_id: video.video_id,
        filename: video.filename,
        preview_url: toProxyMediaUrl(video.preview_url),
      };
    }
    if (video.status === "failed") {
      throw new Error(video.message || "源片导入失败");
    }
    await sleep(1500);
  }
  throw new Error("导入超时，请稍后重试");
}

export async function getVideoInfo(videoId: string): Promise<VideoInfo> {
  const data = await requestData<{
    video_id: string;
    filename: string;
    duration?: number | null;
    width?: number | null;
    height?: number | null;
    fps?: number | null;
    codec?: string | null;
    size_bytes?: number | null;
    preview_url?: string | null;
    has_audio?: boolean;
    oss_key?: string | null;
    status?: string;
  }>(`/api/v1/videos/${videoId}`);
  if (data.status && data.status !== "ready") {
    throw new Error(
      data.status === "importing"
        ? "视频仍在导入中"
        : `视频状态不可用: ${data.status}`,
    );
  }
  return mapVideo(data);
}

export async function deleteVideo(videoId: string): Promise<void> {
  const res = await fetch(`/api/v1/videos/${videoId}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) {
    throw new Error(await readError(res, "删除视频失败"));
  }
}

function mapSegment(seg: {
  segment_id: string;
  job_id: string;
  index: number;
  start_time: number;
  end_time: number;
  start_frame?: number | null;
  end_frame?: number | null;
  oss_key?: string | null;
  oss_url?: string | null;
  download_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  download_filename?: string | null;
  source: string;
  confidence?: number | null;
  summary?: string | null;
  status: string;
}): SegmentResponse {
  return {
    id: seg.segment_id,
    task_id: seg.job_id,
    index: seg.index,
    start_time: seg.start_time,
    end_time: seg.end_time,
    start_frame: seg.start_frame,
    end_frame: seg.end_frame,
    oss_key: seg.oss_key,
    oss_url: toProxyMediaUrl(seg.oss_url) || null,
    download_url: toProxyMediaUrl(seg.download_url) || null,
    preview_url: toProxyMediaUrl(seg.preview_url) || null,
    thumb_url: toProxyMediaUrl(seg.thumb_url) || null,
    download_filename: seg.download_filename,
    source: seg.source,
    confidence: seg.confidence,
    summary: seg.summary,
    status: seg.status,
  };
}

function mapJob(data: {
  job_id: string;
  video_id: string;
  mode: string;
  status: string;
  progress?: number;
  message?: string;
  created_at?: string;
  segments?: Parameters<typeof mapSegment>[0][];
}): TaskResponse {
  return {
    id: data.job_id,
    video_id: data.video_id,
    mode: data.mode,
    status: data.status,
    progress: Number(data.progress || 0),
    message: data.message || "",
    created_at: data.created_at || "",
    segments: (data.segments || []).map(mapSegment),
  };
}

export async function createManualTask(
  videoId: string,
  segments: TimeRange[],
): Promise<TaskResponse> {
  const submitted = await requestData<{
    job_id: string;
    video_id: string;
    mode: string;
    status: string;
  }>("/api/v1/jobs/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ video_id: videoId, segments }),
  });
  return mapJob({ ...submitted, progress: 0, message: "", segments: [] });
}

export async function createAutoTask(payload: {
  video_id: string;
  detector: Detector;
  threshold: number;
  min_scene_len: number;
  auto_cut: boolean;
  sample_fps?: number;
}): Promise<TaskResponse> {
  const submitted = await requestData<{
    job_id: string;
    video_id: string;
    mode: string;
    status: string;
  }>("/api/v1/jobs/auto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      video_id: payload.video_id,
      detector: payload.detector,
      threshold: payload.threshold,
      min_scene_len: payload.min_scene_len,
      sample_fps: payload.sample_fps,
    }),
  });
  return mapJob({ ...submitted, progress: 0, message: "", segments: [] });
}

export async function getTaskStatus(taskId: string): Promise<TaskResponse> {
  const data = await requestData<Parameters<typeof mapJob>[0]>(
    `/api/v1/jobs/${taskId}`,
  );
  return mapJob(data);
}

export async function downloadSelectedSegments(
  _taskId: string,
  _segmentIds: string[],
): Promise<Blob> {
  throw new Error("已取消本机打包下载，请使用「获取 OSS 地址」");
}

export async function publishSegments(
  taskId: string,
  options?: {
    segmentIds?: string[] | null;
    /** 兼容旧调用方；后端不再使用目标目录 */
    ossKey?: string;
  },
): Promise<PublishSegmentsResponse> {
  const segmentIds = options?.segmentIds;
  const ossKey = options?.ossKey;
  const body: Record<string, unknown> = {};
  if (ossKey?.trim()) body.oss_key = ossKey.trim();
  if (segmentIds?.length) body.segment_ids = segmentIds;
  return requestData<PublishSegmentsResponse>(
    `/api/v1/jobs/${taskId}/publish`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}


export async function confirmAutoTask(
  taskId: string,
  segments: SegmentResponse[],
): Promise<TaskResponse> {
  await requestData(`/api/v1/jobs/${taskId}/cut`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      segments: segments.map((s) => ({
        start: s.start_time,
        end: s.end_time,
      })),
    }),
  });
  return getTaskStatus(taskId);
}
