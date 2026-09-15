import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { formatTime } from "../utils/format";

interface Props {
  mountId: string;
  active: boolean;
  duration: number;
  currentTime: number;
}

function timeToPct(time: number, duration: number): number {
  if (!duration) return 0;
  return Math.max(0, Math.min(100, (time / duration) * 100));
}

export function PlayerTimeScrubber({
  mountId,
  active,
  duration,
  currentTime,
}: Props) {
  const [mount, setMount] = useState<HTMLElement | null>(null);

  useEffect(() => {
    if (!active || !mountId) {
      setMount(null);
      return;
    }
    setMount(document.getElementById(mountId));
  }, [active, mountId]);

  if (!mount || !duration) return null;

  const pct = timeToPct(currentTime, duration);

  return createPortal(
    <div className="player-time-scrubber" aria-hidden>
      <div className="player-time-track">
        <div className="player-time-fill" style={{ width: `${pct}%` }} />
        <div className="player-time-thumb" style={{ left: `${pct}%` }} />
      </div>
      <div className="player-time-label">
        {formatTime(currentTime)} / {formatTime(duration)}
      </div>
    </div>,
    mount,
  );
}
