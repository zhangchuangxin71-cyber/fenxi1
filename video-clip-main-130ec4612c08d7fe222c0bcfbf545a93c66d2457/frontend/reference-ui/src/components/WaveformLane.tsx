import { useEffect, useRef } from "react";

interface Props {
  peaks: number[];
  loading?: boolean;
  hasAudio?: boolean;
  error?: string | null;
  className?: string;
}

/** 在 canvas 上绘制居中镜像音频波形；无数据时也给出明确状态文案 */
export function WaveformLane({
  peaks,
  loading = false,
  hasAudio = true,
  error = null,
  className = "",
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const statusText = (() => {
    if (loading) return "正在分析音轨并生成波形…";
    if (error) return "波形接口失败（后端需包含 /waveform，请重启 API）";
    if (!hasAudio) return "源文件无声音，无法显示波形";
    if (!peaks.length) return "波形数据为空";
    // 全 0 峰值：有音轨但几乎静音
    if (peaks.every((p) => !p || p < 1e-4)) return "检测到音轨，但几乎为静音";
    return null;
  })();

  const statusTitle = (() => {
    if (!hasAudio && !loading && !error) {
      return "上传的源文件本身不含音轨，不是切割丢失；切割结果也会是无声视频。";
    }
    return statusText ?? undefined;
  })();

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const cssW = Math.max(1, wrap.clientWidth);
      const cssH = Math.max(1, wrap.clientHeight);
      canvas.width = Math.floor(cssW * dpr);
      canvas.height = Math.floor(cssH * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;

      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      ctx.fillStyle = "#0b1220";
      ctx.fillRect(0, 0, cssW, cssH);

      ctx.strokeStyle = "rgba(96, 165, 250, 0.25)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, cssH / 2);
      ctx.lineTo(cssW, cssH / 2);
      ctx.stroke();

      // 有状态文案时只画底轨，文案由 DOM 显示，避免与 canvas 叠字重复
      if (statusText) return;

      const mid = cssH / 2;
      const amp = cssH * 0.42;
      const n = peaks.length;
      const barW = Math.max(cssW / n, 0.5);

      const gradient = ctx.createLinearGradient(0, 0, 0, cssH);
      gradient.addColorStop(0, "rgba(96, 165, 250, 0.95)");
      gradient.addColorStop(0.5, "rgba(59, 130, 246, 0.85)");
      gradient.addColorStop(1, "rgba(37, 99, 235, 0.95)");
      ctx.fillStyle = gradient;

      for (let i = 0; i < n; i++) {
        const p = Math.max(0, Math.min(1, peaks[i] || 0));
        const h = Math.max(1.5, p * amp);
        const x = (i / n) * cssW;
        const w = Math.max(barW * 0.85, 0.6);
        ctx.fillRect(x, mid - h, w, h * 2);
      }
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(wrap);
    return () => ro.disconnect();
  }, [peaks, loading, hasAudio, error, statusText]);

  return (
    <div
      ref={wrapRef}
      className={`waveform-lane${className ? ` ${className}` : ""}`}
      role="img"
      aria-label={statusText || "音频波形"}
      title={statusTitle}
    >
      <canvas ref={canvasRef} className="waveform-canvas" />
      {statusText ? (
        <span className="waveform-status-fallback">{statusText}</span>
      ) : null}
    </div>
  );
}
