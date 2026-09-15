import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { formatTime } from "../utils/format";
import { playheadLeftCss, timeToPct } from "../utils/timelineScale";

interface Props {
  mountId: string;
  active: boolean;
  duration: number;
  currentTime: number;
  start: number | null;
  end: number | null;
  showBadge?: boolean;
}

export function PlayerSelectionOverlay({
  mountId,
  active,
  duration,
  currentTime,
  start,
  end,
  showBadge = true,
}: Props) {
  const [mount, setMount] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!active || !mountId) {
      setMount(null);
      return;
    }
    const sync = () => {
      setMount(document.getElementById(mountId));
    };
    sync();
    // transform 层可能稍晚于面板挂载
    const t = window.setTimeout(sync, 0);
    return () => window.clearTimeout(t);
  }, [active, mountId]);

  const hasRange =
    start != null && end != null && duration > 0 && start < end;
  if (!mount || !hasRange) return null;

  const leftPct = timeToPct(start!, duration);
  const widthPct = timeToPct(end! - start!, duration);
  const rightPct = leftPct + widthPct;

  return createPortal(
    <div className="player-selection-overlay" aria-hidden>
      {showBadge && (
        <span className="player-selection-badge">
          选区 {formatTime(start!)} → {formatTime(end!)}
        </span>
      )}
      <div className="player-selection-track">
        <div
          className="player-selection-range"
          style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
        />
        <div
          className="player-selection-mark start"
          style={{ left: `${leftPct}%` }}
        />
        <div
          className="player-selection-mark end"
          style={{ left: `${rightPct}%` }}
        />
        <div
          className="player-selection-playhead"
          style={{ left: playheadLeftCss(currentTime, duration) }}
        />
      </div>
    </div>,
    mount,
  );
}
