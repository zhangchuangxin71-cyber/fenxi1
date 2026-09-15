export type Detector = "content" | "adaptive" | "semantic";
export type Sensitivity = "fine" | "standard" | "coarse";
export type TabMode = "manual" | "auto";

export interface VideoInfo {
  id: string;
  filename: string;
  duration: number;
  width: number;
  height: number;
  fps: number;
  codec: string;
  size_bytes: number;
  preview_url: string;
  has_audio?: boolean;
  oss_key?: string | null;
}

export interface UploadResponse {
  video_id: string;
  filename: string;
  preview_url: string;
}

export interface TimeRange {
  start: number;
  end: number;
}

export interface SegmentResponse {
  id: string;
  task_id: string;
  index: number;
  start_time: number;
  end_time: number;
  start_frame?: number | null;
  end_frame?: number | null;
  output_path?: string | null;
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
}

export interface TaskResponse {
  id: string;
  video_id: string;
  mode: string;
  status: string;
  progress: number;
  message: string;
  created_at: string;
  segments: SegmentResponse[];
}

export interface SegmentObjectItem {
  segment_id: string;
  index: number;
  oss_key: string;
  oss_url?: string | null;
}

export interface PublishSegmentsResponse {
  all: SegmentObjectItem[];
  selected: SegmentObjectItem[];
}

export interface ScenePreviewState {
  start: number;
  end: number;
  exclusiveEnd: number;
  holdAt: number;
  triggerAt: number;
}
