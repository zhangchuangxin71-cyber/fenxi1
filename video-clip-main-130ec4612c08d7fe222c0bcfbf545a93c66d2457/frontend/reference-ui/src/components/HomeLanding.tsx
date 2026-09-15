import { useCallback, useMemo, type ReactNode } from "react";
import type { TabMode, VideoInfo } from "../api/types";
import { formatSize, formatTime } from "../utils/format";
import { ModePickPage } from "./ModePickPage";
import { OssVideoPicker } from "./OssVideoPicker";

interface Props {
  videoInfo: VideoInfo | null;
  previewUrl?: string | null;
  ossImporting?: boolean;
  /** 0=选择源片（含导入后确认），1=选择切片模式 */
  landingStep: 0 | 1;
  /** 已有源片时仍强制显示媒资列表（更换源片） */
  forcePickSource?: boolean;
  onSelectMode: (mode: TabMode) => void;
  onImportOss: (ref: string) => void | Promise<void>;
  /** 源片确认后进入选模式 */
  onContinueToMode?: () => void;
  /** 回到选片列表 */
  onChangeSource?: () => void;
  /** 取消更换源片 */
  onCancelChangeSource?: () => void;
  toast?: (msg: string, isError?: boolean) => void;
}

function gcd(a: number, b: number): number {
  let x = Math.abs(a);
  let y = Math.abs(b);
  while (y) {
    const t = y;
    y = x % y;
    x = t;
  }
  return x || 1;
}

function aspectLabel(w: number, h: number): string {
  if (!w || !h) return "";
  const g = gcd(w, h);
  return `${Math.round(w / g)}:${Math.round(h / g)}`;
}

function formatDurationHms(sec: number): string {
  const total = Math.max(0, Math.floor(sec));
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hh)}:${pad(mm)}:${pad(ss)}`;
}

function formatCodec(codec: string | undefined): string {
  const c = (codec || "").toLowerCase();
  if (!c) return "--";
  if (c.includes("h264") || c.includes("avc")) return "H.264 / AVC";
  if (c.includes("h265") || c.includes("hevc")) return "H.265 / HEVC";
  if (c.includes("vp9")) return "VP9";
  if (c.includes("av1")) return "AV1";
  return codec || "--";
}

function formatBitrate(sizeBytes: number, duration: number): string {
  if (!sizeBytes || !duration || duration <= 0) return "--";
  const mbps = (sizeBytes * 8) / duration / 1_000_000;
  if (mbps >= 10) return `${mbps.toFixed(1)} Mbps`;
  return `${mbps.toFixed(2)} Mbps`;
}

function MetaRow({
  icon,
  label,
  value,
  mono,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="confirm-meta-row">
      <span className="confirm-meta-icon" aria-hidden>
        {icon}
      </span>
      <span className="confirm-meta-label">{label}</span>
      <span className={`confirm-meta-value${mono ? " is-mono" : ""}`} title={value}>
        {value}
      </span>
    </div>
  );
}

const IconClock = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.5" />
    <path d="M10 6.5V10l2.5 1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);
const IconRes = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <rect x="3.5" y="5" width="13" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
    <path d="M7 15v1.5M13 15v1.5M6 16.5h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);
const IconFps = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <path d="M4 14V6l6 4-6 4Z" fill="currentColor" />
    <path d="M12 6.5h4M12 10h4M12 13.5h4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);
const IconVideo = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <rect x="2.5" y="5" width="11" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
    <path d="M13.5 8.5 17 6.5v7l-3.5-2v-3Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
  </svg>
);
const IconAudio = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <path d="M9 4.5v11l-4-3H3.5A1.5 1.5 0 0 1 2 11V9a1.5 1.5 0 0 1 1.5-1.5H5l4-3Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M12.5 7.5a3 3 0 0 1 0 5M14.5 5.5a5.5 5.5 0 0 1 0 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);
const IconSize = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <path d="M5 16.5h10a1.5 1.5 0 0 0 1.5-1.5V8.2L12.8 3.5H5A1.5 1.5 0 0 0 3.5 5v10A1.5 1.5 0 0 0 5 16.5Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    <path d="M12.5 3.5V8h4.5" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
  </svg>
);
const IconBitrate = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <path d="M3.5 14.5 7 8l3 4.5 2.5-3.5 4 5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
  </svg>
);
const IconPath = (
  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
    <path d="M4 7.5h5l1.5 1.5H16A1.5 1.5 0 0 1 17.5 10.5v4A1.5 1.5 0 0 1 16 16H4A1.5 1.5 0 0 1 2.5 14.5v-5A1.5 1.5 0 0 1 4 8Z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
  </svg>
);

export function HomeLanding({
  videoInfo,
  previewUrl,
  ossImporting = false,
  landingStep,
  forcePickSource = false,
  onSelectMode,
  onImportOss,
  onContinueToMode,
  onChangeSource,
  onCancelChangeSource,
  toast,
}: Props) {
  const hasPreview = Boolean(videoInfo && previewUrl);
  const showSourceConfirm = hasPreview && !forcePickSource && landingStep === 0;
  const showModePick = hasPreview && !forcePickSource && landingStep === 1;

  const metaText = useMemo(() => {
    if (!videoInfo) return "";
    const ratio = aspectLabel(videoInfo.width, videoInfo.height);
    const lines = [
      `文件名：${videoInfo.filename}`,
      `时长：${formatDurationHms(videoInfo.duration)}`,
      `分辨率：${videoInfo.width} × ${videoInfo.height}${ratio ? ` (${ratio})` : ""}`,
      `帧率：${videoInfo.fps.toFixed(2)} FPS`,
      `视频编码：${formatCodec(videoInfo.codec)}`,
      `音频：${videoInfo.has_audio === false ? "无音轨" : "已包含音轨"}`,
      `文件大小：${formatSize(videoInfo.size_bytes)}`,
      `比特率：${formatBitrate(videoInfo.size_bytes, videoInfo.duration)}`,
      `路径：${videoInfo.oss_key || videoInfo.filename}`,
    ];
    return lines.join("\n");
  }, [videoInfo]);

  const copyMeta = useCallback(async () => {
    if (!metaText) return;
    try {
      await navigator.clipboard.writeText(metaText);
      toast?.("已复制视频信息");
    } catch {
      toast?.("复制失败，请手动选择文本", true);
    }
  }, [metaText, toast]);

  if (showModePick) {
    return <ModePickPage onSelectMode={onSelectMode} />;
  }

  if (showSourceConfirm && videoInfo && previewUrl) {
    const ratio = aspectLabel(videoInfo.width, videoInfo.height);
    return (
      <div className="home-landing home-landing-confirm-source confirm-source-v2">
        <div className="home-landing-main">
          <section className="confirm-source-grid">
            <div className="home-card confirm-preview-card">
              <div className="confirm-preview-head">
                <h3 className="confirm-section-title">视频预览</h3>
                <div className="confirm-preview-head-right">
                  <span className="confirm-filename-pill" title={videoInfo.filename}>
                    {videoInfo.filename}
                  </span>
                  <button
                    type="button"
                    className="confirm-link-btn"
                    disabled={ossImporting}
                    onClick={() => onChangeSource?.()}
                  >
                    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden>
                      <path
                        d="M3 8a5 5 0 0 1 8.3-3.7M13 3.5V6H10.5"
                        stroke="currentColor"
                        strokeWidth="1.4"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                      <path
                        d="M13 8a5 5 0 0 1-8.3 3.7M3 12.5V10h2.5"
                        stroke="currentColor"
                        strokeWidth="1.4"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                    更换源片
                  </button>
                </div>
              </div>

              <div className="confirm-preview-stage">
                <video
                  key={previewUrl}
                  src={previewUrl}
                  controls
                  playsInline
                  preload="metadata"
                />
                {ossImporting && (
                  <div className="home-video-replace-mask">
                    <span>正在从 OSS 拉取源片…</span>
                  </div>
                )}
              </div>

              <div className="confirm-tip-box">
                <span className="confirm-tip-icon" aria-hidden>
                  <svg viewBox="0 0 20 20" width="16" height="16" fill="none">
                    <path
                      d="M8.2 14.5h3.6M10 3.5a4.5 4.5 0 0 1 2.4 8.3V13.5H7.6v-1.7A4.5 4.5 0 0 1 10 3.5Z"
                      stroke="currentColor"
                      strokeWidth="1.4"
                      strokeLinejoin="round"
                    />
                  </svg>
                </span>
                <p>
                  提示：请确认视频内容和画质无误，为获得最佳切片效果，建议使用分辨率≥720P的视频。
                </p>
              </div>
            </div>

            <div className="home-card confirm-meta-card">
              <div className="confirm-meta-head">
                <h3 className="confirm-section-title">视频信息</h3>
                <button type="button" className="confirm-link-btn" onClick={() => void copyMeta()}>
                  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden>
                    <rect x="5.5" y="5.5" width="7" height="8" rx="1.2" stroke="currentColor" strokeWidth="1.3" />
                    <path
                      d="M3.5 10.5V3.8A1.3 1.3 0 0 1 4.8 2.5h5.7"
                      stroke="currentColor"
                      strokeWidth="1.3"
                      strokeLinecap="round"
                    />
                  </svg>
                  复制信息
                </button>
              </div>

              <div className="confirm-meta-list">
                <MetaRow
                  icon={IconClock}
                  label="时长"
                  value={formatDurationHms(videoInfo.duration)}
                />
                <MetaRow
                  icon={IconRes}
                  label="分辨率"
                  value={`${videoInfo.width} × ${videoInfo.height}${ratio ? ` (${ratio})` : ""}`}
                />
                <MetaRow
                  icon={IconFps}
                  label="帧率"
                  value={`${videoInfo.fps.toFixed(2)} FPS`}
                />
                <MetaRow
                  icon={IconVideo}
                  label="视频编码"
                  value={formatCodec(videoInfo.codec)}
                />
                <MetaRow
                  icon={IconAudio}
                  label="音频编码"
                  value={
                    videoInfo.has_audio === false
                      ? "无音轨"
                      : "已包含音轨"
                  }
                />
                <MetaRow
                  icon={IconSize}
                  label="文件大小"
                  value={formatSize(videoInfo.size_bytes)}
                />
                <MetaRow
                  icon={IconBitrate}
                  label="比特率"
                  value={formatBitrate(videoInfo.size_bytes, videoInfo.duration)}
                />
                <MetaRow
                  icon={IconPath}
                  label="文件路径"
                  value={videoInfo.oss_key || videoInfo.filename}
                  mono
                />
              </div>

              <div className="confirm-ready-banner">
                <span className="confirm-ready-check" aria-hidden>
                  ✓
                </span>
                视频解析完成，可以继续下一步操作
              </div>
            </div>
          </section>

          <div className="confirm-source-footer">
            <button
              type="button"
              className="confirm-next-btn"
              onClick={() => onContinueToMode?.()}
            >
              下一步：选择切片模式
              <span aria-hidden>→</span>
            </button>
            <p className="confirm-footer-note">
              <span aria-hidden>🔒</span>
              已成功导入并解析视频
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="home-landing home-landing-pick-source">
      <div className="home-landing-main">
        <section className="home-card oss-import-card oss-import-hero">
          <div className="home-video-preview-head">
            <div>
              <h3 className="home-card-title">导入源片</h3>
              <p className="home-pick-sub">
                {hasPreview
                  ? "粘贴新的 oss_key / URL 后将替换当前视频"
                  : "粘贴 oss_key 或完整 URL 导入后，将显示预览与基础信息"}
              </p>
            </div>
            {hasPreview && (
              <button
                type="button"
                className="header-ghost"
                disabled={ossImporting}
                onClick={() => onCancelChangeSource?.()}
              >
                取消
              </button>
            )}
          </div>
          <OssVideoPicker
            busy={ossImporting}
            onPick={onImportOss}
            pickLabel={hasPreview ? "更换并导入" : "导入源片"}
            hint="切割成品会保存到媒资切片区。导入为异步，较大文件可能需等待数十秒。"
          />
        </section>

        {!hasPreview && (
          <section className="home-upload-grid home-upload-grid-solo">
            <div className="home-card oss-import-placeholder">
              <div className="upload-illus" aria-hidden>
                <div className="upload-box-art">
                  <span className="art-play">▶</span>
                  <span className="art-chip film">☁</span>
                </div>
              </div>
              <p className="upload-lead">请在上方粘贴 OSS Key 或 URL</p>
              <p className="upload-formats">
                支持 MP4 / MOV / MKV / AVI 等常见格式
                <br />
                导入后可预览画面并核对元数据
              </p>
            </div>
          </section>
        )}

        {hasPreview && forcePickSource && (
          <section className="home-card home-current-source-hint">
            <div className="home-current-source-meta">
              <strong title={videoInfo?.filename}>{videoInfo?.filename}</strong>
              <span>
                {videoInfo
                  ? `${formatTime(videoInfo.duration)} · ${videoInfo.width}×${videoInfo.height} · ${formatSize(videoInfo.size_bytes)}`
                  : ""}
              </span>
            </div>
            <p className="hint-text">当前源片如上；导入新片后将覆盖并回到确认页。</p>
          </section>
        )}
      </div>
    </div>
  );
}
