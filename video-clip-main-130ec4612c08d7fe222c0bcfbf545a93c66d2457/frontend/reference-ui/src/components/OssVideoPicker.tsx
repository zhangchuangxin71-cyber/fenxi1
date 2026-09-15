import { useState } from "react";

interface Props {
  busy?: boolean;
  onPick: (ref: string) => void | Promise<void>;
  hint?: string;
  pickLabel?: string;
}

/**
 * 粘贴 OSS 对象键或完整 URL 导入源片（后端已无目录浏览）。
 */
export function OssVideoPicker({
  busy = false,
  onPick,
  hint = "粘贴 oss_key 或完整 HTTPS URL，导入后显示预览与基础信息。",
  pickLabel = "导入",
}: Props) {
  const [value, setValue] = useState("");

  const trimmed = value.trim();
  const canSubmit = Boolean(trimmed) && !busy;

  const submit = () => {
    if (!canSubmit) return;
    void onPick(trimmed);
  };

  return (
    <div className="oss-picker oss-picker-paste">
      {hint ? <p className="hint-text">{hint}</p> : null}

      <label className="oss-paste-label" htmlFor="oss-source-ref">
        OSS Key 或 URL
      </label>
      <textarea
        id="oss-source-ref"
        className="oss-paste-input"
        rows={3}
        placeholder={
          "例如：\nprod/.../video.mp4\n或\nhttps://bucket.oss-cn-xxx.aliyuncs.com/path/to/video.mp4"
        }
        value={value}
        disabled={busy}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submit();
          }
        }}
      />

      <div className="oss-picker-footer">
        <span className="hint-text">
          {trimmed
            ? /^https?:\/\//i.test(trimmed)
              ? "将按 oss_url 导入"
              : "将按 oss_key 导入"
            : "请粘贴对象键或 URL"}
        </span>
        <button
          type="button"
          className="btn-primary-upload"
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? "导入中…" : pickLabel}
        </button>
      </div>
    </div>
  );
}
