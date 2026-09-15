import { useEffect, useState } from "react";

export interface WaveformInfo {
  peaks: number[];
  hasAudio: boolean;
  loading: boolean;
  error: string | null;
}

/** 拉取整段音频波形峰值，用于时间轴绘制 */
export function useWaveform(
  videoId: string | null | undefined,
  duration = 0,
  enabled = true,
): WaveformInfo {
  const [peaks, setPeaks] = useState<number[]>([]);
  const [hasAudio, setHasAudio] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !videoId) {
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    const endpoint = `/api/v1/videos/${videoId}`;

    fetch(endpoint, { signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail || "波形生成失败");
        }
        const body = (await res.json()) as {
          code?: number;
          data?: {
            waveform?: { peaks?: number[]; has_audio?: boolean } | null;
            has_audio?: boolean;
          };
          peaks?: number[];
          has_audio?: boolean;
        };
        if (cancelled) return;
        const wf = body.data?.waveform;
        const peaks = wf?.peaks ?? body.peaks;
        const hasAudio = wf?.has_audio ?? body.data?.has_audio ?? body.has_audio;
        setPeaks(Array.isArray(peaks) ? peaks.map(Number) : []);
        setHasAudio(Boolean(hasAudio));
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled || (e instanceof DOMException && e.name === "AbortError")) {
          return;
        }
        setError(e instanceof Error ? e.message : "波形生成失败");
        setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [videoId, duration, enabled]);

  return { peaks, hasAudio, loading, error };
}
