/**
 * Flow B API types — mirrors docs/api-flow-b-production.md (v2.30).
 * Base URL: /api/v1
 *
 * v2.30: all stages async (POST .../run → GET .../jobs/{id}); compose delivers OSS URLs
 * and deletes workspace on success; global queue fields when GLOBAL_JOB_WORKERS saturated.
 *
 * Usage: copy into frontend repo or generate from OpenAPI when implemented.
 */

// ---------------------------------------------------------------------------
// Envelope & shared
// ---------------------------------------------------------------------------

export interface ApiResponse<T> {
  code: number;
  message: string;
  data: T;
}

export interface ApiErrorData {
  request_id?: string;
  [key: string]: unknown;
}

export interface WorkspaceFlags {
  split_ready: boolean;
  audio_ready: boolean;
  audio_confirmed: boolean;
  media_bound: boolean;
  visual_preview_ready: boolean;
  compose_ready: boolean;
}

export interface WorkspaceDigests {
  text_digest: string | null;
  audio_preview_digest: string | null;
  audio_confirmed_digest: string | null;
  media_binding_digest: string | null;
  render_style_digest: string | null;
}

export type ResolutionMode =
  | "1080x1920"
  | "720x1280"
  | "2160x3840"
  | "1920x1080"
  | "1280x720"
  | "1080x1080"
  | "source"
  | "source_largest"
  | "custom";

export type PublishPreset = "douyin" | "bilibili" | "square";

export type MediaType = "image" | "video";

export type SplitModeUsed = "llm" | "rule" | "rule_fallback";

export type StageJobStatus =
  | "queued"
  | "running"
  | "done"
  | "failed"
  | "cancelled"
  | "interrupted";

/** @deprecated alias — use StageJobStatus */
export type ComposeJobStatus = StageJobStatus;

export type ComposeProgressPhase =
  | "clip"
  | "subtitle"
  | "compose"
  | "finalize";

export type StageProgressPhase =
  | "split"
  | "fetch"
  | "preview"
  | "clip"
  | "subtitle"
  | "compose"
  | "finalize"
  | string;

// ---------------------------------------------------------------------------
// Shared Job envelope (all stages)
// ---------------------------------------------------------------------------

export interface JobRunResponse {
  job_id: string;
  status: "queued";
}

export interface JobQueueFields {
  /** 1-based position in global wait queue (only when status=queued and pool full) */
  queue_position?: number;
  queue_ahead?: number;
  queue_message?: string;
}

export interface JobProgress {
  phase: StageProgressPhase;
  percent: number;
  message: string;
}

export type JobErrorCategory =
  | "validation"
  | "conflict"
  | "upstream"
  | "process"
  | "cancelled"
  | "interrupted"
  | "timeout";

export interface JobError {
  code: number;
  message: string;
  category: JobErrorCategory;
  retryable: boolean;
  phase?: StageProgressPhase;
  data?: {
    cleaned_paths?: string[];
    [key: string]: unknown;
  };
}

export interface StageJobResponse<TResult = unknown> extends JobQueueFields {
  job_id: string;
  status: StageJobStatus;
  progress: JobProgress | null;
  result: TResult | null;
  error: JobError | null;
  created_at: string;
  updated_at: string;
}

export interface JobCancelResponse {
  job_id: string;
  status: "cancelled";
  cleaned_paths?: string[];
}

// ---------------------------------------------------------------------------
// POST /workspaces
// ---------------------------------------------------------------------------

export interface CreateWorkspaceRequest {
  label?: string;
  /** Client task id; server generates ws_xxx when omitted */
  workspace_id?: string;
}

export interface CreateWorkspaceResponse {
  workspace_id: string;
  label: string | null;
  created_at: string;
  status: WorkspaceFlags;
}

// ---------------------------------------------------------------------------
// DELETE /workspaces/{id}
// ---------------------------------------------------------------------------

export interface DeleteWorkspaceResponse {
  deleted: boolean;
  workspace_id: string;
}

// ---------------------------------------------------------------------------
// GET /health
// ---------------------------------------------------------------------------

export interface HealthCheckItem {
  ok: boolean;
  model?: string;
}

export interface HealthResponse {
  ok: boolean;
  checks: {
    ffmpeg: HealthCheckItem;
    ffprobe: HealthCheckItem;
    llm_split: HealthCheckItem;
  };
}

// ---------------------------------------------------------------------------
// GET /workspaces/{id}/status
// ---------------------------------------------------------------------------

export interface WorkspaceStatusResponse {
  workspace_id: string;
  label: string | null;
  split_ready: boolean;
  audio_ready: boolean;
  audio_confirmed: boolean;
  media_bound: boolean;
  visual_preview_ready: boolean;
  compose_ready: boolean;
  active_revision_id: string | null;
  active_revision_label: string | null;
  digests: WorkspaceDigests;
  warnings: string[];
  active_compose_job_id: string | null;
  active_split_job_id: string | null;
  active_audio_process_job_id: string | null;
  active_visual_preview_job_id: string | null;
}

// ---------------------------------------------------------------------------
// POST /split
// ---------------------------------------------------------------------------

export interface SplitRequest {
  text: string;
  include_ai_prompts?: boolean;
  global_style?: string;
  /** Label when split/run lazily creates the workspace */
  label?: string;
}

export interface AiPromptPair {
  positive_prompt: string;
  negative_prompt: string;
}

export interface SegmentAiPrompts {
  image: AiPromptPair;
  video: AiPromptPair;
}

export interface SplitSegmentBase {
  index: number;
  text: string;
  search_query: string;
  visual: true;
}

export interface SplitSegmentWithPrompts extends SplitSegmentBase {
  ai_prompts: SegmentAiPrompts;
}

export interface SplitSegmentWithoutPrompts extends SplitSegmentBase {}

export interface AiPromptsGlobal {
  video_global_negative_prompt: string;
  video_film_look: string;
}

export interface SplitResponseBase {
  text_digest: string;
  split_mode_used: SplitModeUsed;
  segment_count: number;
  warnings: string[];
}

export interface SplitResponseWithPrompts extends SplitResponseBase {
  segments: SplitSegmentWithPrompts[];
  ai_prompts_global: AiPromptsGlobal;
}

export interface SplitResponseWithoutPrompts extends SplitResponseBase {
  segments: SplitSegmentWithoutPrompts[];
}

export type SplitResponse =
  | SplitResponseWithPrompts
  | SplitResponseWithoutPrompts;

/** POST /split/run — same body as SplitRequest */
export type SplitRunRequest = SplitRequest;

export type SplitJobResponse = StageJobResponse<SplitResponse>;
export interface SplitRunResponse extends JobRunResponse {
  workspace_created?: true;
  workspace_id?: string;
}

// ---------------------------------------------------------------------------
// POST /audio/process/run & PUT /audio/confirm
// ---------------------------------------------------------------------------

export interface SegmentAudioInput {
  url: string;
}

export interface AudioProcessSegmentInput {
  index: number;
  text: string;
  audio: SegmentAudioInput;
}

export interface AudioProcessRequest {
  code: string;
  id: string;
  segments: AudioProcessSegmentInput[];
  force_refresh?: boolean;
  speed?: number;
}

export interface AudioSegmentResult {
  index: number;
  text: string;
  duration_sec: number;
  clip_duration_sec: number;
  audio_url: string;
}

export interface AudioProcessResponse {
  revision_id: string;
  status: "draft";
  audio_config_digest: string;
  cached: boolean;
  segment_count: number;
  segments: AudioSegmentResult[];
  gaps_sec: number[];
  total_speech_sec: number;
  total_with_gaps_sec: number;
  master_audio_url: string;
  subtitle_srt_url: string;
  warnings: string[];
  evicted_revision_ids?: string[];
}

/** POST /audio/process/run — same body as AudioProcessRequest */
export type AudioProcessRunRequest = AudioProcessRequest;

export type AudioProcessJobResponse = StageJobResponse<AudioProcessResponse>;
export type AudioProcessRunResponse = JobRunResponse;

export interface AudioRevisionSummary {
  revision_id: string;
  label: string;
  status: "draft" | "active";
  created_at?: string;
  audio_config_digest: string;
  is_active: boolean;
  master_audio_url: string;
  subtitle_srt_url: string;
  total_with_gaps_sec?: number;
}

export interface AudioRevisionsListResponse {
  active_revision_id: string | null;
  max_revisions: number;
  count: number;
  revisions: AudioRevisionSummary[];
}

export interface AudioConfirmRequest {
  revision_id?: string;
  audio_config_digest?: string;
}

export interface AudioConfirmResponse {
  revision_id: string;
  label: string;
  audio_confirmed: true;
  audio_config_digest: string;
  master_audio_url: string;
  subtitle_srt_url: string;
}

// ---------------------------------------------------------------------------
// Shared segment + media input (compose / visual/preview)
// ---------------------------------------------------------------------------

export interface MediaBindUrlInput {
  url: string;
  type: MediaType;
  start_sec?: number;
}

export interface MediaBindSegmentInput {
  index: number;
  text: string;
  media: MediaBindUrlInput;
}

// ---------------------------------------------------------------------------
// POST /maintenance/purge-workspaces
// ---------------------------------------------------------------------------

export interface PurgePolicyInput {
  ttl_empty_days?: number;
  ttl_abandoned_days?: number;
  ttl_composed_days?: number;
  grace_hours?: number;
}

export interface PurgeWorkspacesRequest {
  dry_run?: boolean;
  max_delete?: number;
  include_composed?: boolean;
  policy?: PurgePolicyInput;
}

export interface PurgeWorkspaceCandidate {
  workspace_id: string;
  label: string | null;
  state: "empty" | "in_progress" | "composed";
  last_activity_at: string;
  age_hours: number;
  size_bytes: number;
  eligible: boolean;
  skip_reason: string | null;
  reason: string | null;
  pinned: boolean;
  active_compose_job_id: string | null;
}

export interface PurgeWorkspacesResponse {
  dry_run: boolean;
  policy: PurgePolicyInput;
  scanned: number;
  eligible: number;
  deleted: number;
  deleted_ids: string[];
  skipped: Record<string, number>;
  candidates: PurgeWorkspaceCandidate[];
  mode?: string;
  all_evaluated?: PurgeWorkspaceCandidate[];
}

// ---------------------------------------------------------------------------
// POST /visual/preview
// ---------------------------------------------------------------------------

export interface ResolutionInput {
  width?: number;
  height?: number;
}

export interface SubtitleStyleInput {
  font_name: string;
  font_scale: number;
  /** 档位：1=上移1档，-1=下移1档，0=默认（非像素） */
  y_offset?: number;
}

/** GET /api/v1/subtitle/fonts — 前端字体下拉；提交 preview/compose 时传 name */
export interface SubtitleFontPickerChoice {
  name: string;
  ass_font_name: string;
}

export interface SubtitleFontListResponse {
  choices: SubtitleFontPickerChoice[];
}

export interface VisualPreviewRequest {
  code: string;
  id: string;
  media_url: string;
  media_type: MediaType;
  start_sec?: number;
  text: string;
  resolution: ResolutionInput;
  subtitle_style: SubtitleStyleInput;
}

export interface VisualPreviewResponse {
  render_style_digest: string;
  resolution: { width: number; height: number };
  subtitle_style_applied: {
    font_name: string;
    font_name_input?: string;
    font_size_px: number;
    font_scale: number;
    y_offset: number;
    y_offset_px?: number;
    y_offset_step_px?: number;
    y_offset_label?: string;
  };
  preview: {
    text: string;
    media_type: MediaType;
    preview_image_url: string;
  };
  preview_image_url?: string;
  preview_object_key?: string;
}

/** POST /visual/preview/run — same body as VisualPreviewRequest */
export type VisualPreviewRunRequest = VisualPreviewRequest;

export type VisualPreviewJobResponse = StageJobResponse<VisualPreviewResponse>;
export type VisualPreviewRunResponse = JobRunResponse;
// ---------------------------------------------------------------------------
// POST /compose/run & jobs
// ---------------------------------------------------------------------------

export interface ComposeRunRequest {
  /** Tenant code — required; used in OSS object key prefix */
  code: string;
  /** User id — required; used in OSS object key prefix */
  id: string;
  subtitle_mode?: "hard" | "soft";
  reuse_intermediates?: boolean;
  /** Default true: delete compose scratch; v2.30 also deletes entire workspace after OSS upload */
  cleanup_scratch_on_success?: boolean;
}

export type ComposeRunResponse = JobRunResponse;

export interface ComposeProgress {
  phase: ComposeProgressPhase;
  percent: number;
  message: string;
}

/** @deprecated alias — use JobError */
export type ComposeJobError = JobError;

export interface ComposeResult {
  /** OSS HTTPS URL for final video (primary deliverable) */
  output_video_url: string;
  output_video_object_key: string;
  output_audio_url: string;
  output_audio_object_key?: string;
  /** Final styled ASS generated by compose and uploaded before local cleanup */
  output_subtitle_ass_url: string;
  output_subtitle_ass_object_key: string;
  log_url?: string | null;
  log_object_key?: string | null;
  oss_prefix?: string;
  /** true after successful compose + workspace tree deleted */
  workspace_deleted: boolean;
  total_seconds: number;
  durations_sec?: number[];
  gaps_sec?: number[];
  intermediates_cleaned: boolean;
}

export interface ComposeJobResponse extends JobQueueFields {
  job_id: string;
  workspace_id?: string;
  status: StageJobStatus;
  progress: ComposeProgress | null;
  result: ComposeResult | null;
  error: JobError | null;
  created_at: string;
  updated_at: string;
  archived_at?: string;
}

export type ComposeCancelResponse = JobCancelResponse;

/** Job JSON archived under workspaces/_compose_results/{job_id}.json after compose success */
export interface ArchivedComposeJob extends ComposeJobResponse {
  archived_at: string;
  options?: {
    code: string;
    id: string;
    subtitle_mode?: string;
    reuse_intermediates?: boolean;
    cleanup_scratch_on_success?: boolean;
  };
}

// ---------------------------------------------------------------------------
// GET /maintenance/coord-status
// ---------------------------------------------------------------------------

export interface CoordJobSummary {
  workspace_id: string;
  job_id: string;
  kind: string;
  status: string;
  message?: string;
}

export interface CoordState {
  running: number;
  waiting: string[];
}

export interface CoordStatusResponse {
  global_job_workers: number;
  coord_state: CoordState;
  jobs_on_disk: {
    running_count: number;
    queued_count: number;
    running_jobs: CoordJobSummary[];
    queued_jobs: CoordJobSummary[];
  };
  desync: boolean;
  waiting_count: number;
}
