interface Props {
  title: string;
  rangeText?: string;
  src: string;
  onClose: () => void;
  onSave?: () => void;
  saveLabel?: string;
}

export function ClipPreviewModal({
  title,
  rangeText,
  src,
  onClose,
  onSave,
  saveLabel = "下载该视频",
}: Props) {
  return (
    <div
      className="clip-preview-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
    >
      <div
        className="clip-preview-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="clip-preview-head">
          <h3>{title}</h3>
          <button
            type="button"
            className="guide-close"
            onClick={onClose}
            aria-label="关闭预览"
          >
            ×
          </button>
        </div>
        {rangeText ? (
          <p className="clip-preview-range">{rangeText}</p>
        ) : null}
        <video
          key={src}
          className="clip-preview-video"
          src={src}
          controls
          autoPlay
          playsInline
          preload="metadata"
        />
        <div className="clip-preview-actions">
          {onSave ? (
            <button type="button" className="btn-primary-upload" onClick={onSave}>
              {saveLabel}
            </button>
          ) : null}
          <button type="button" className="header-ghost" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
