/** 分镜 end 是下一镜起点（半开 [start,end)）。源片预览需提前刹停。 */

export function sceneExclusiveEnd(
  end: number,
  endFrame: number | null | undefined,
  fps: number,
): number {
  if (endFrame != null && Number.isFinite(endFrame)) return endFrame / fps;
  return end;
}

export function sceneHoldTime(
  start: number,
  end: number,
  startFrame: number | null | undefined,
  endFrame: number | null | undefined,
  fps: number,
): number {
  const fd = 1 / fps;
  const exclusive = sceneExclusiveEnd(end, endFrame, fps);
  if (endFrame != null && startFrame != null && endFrame > startFrame) {
    return Math.max(start, (endFrame - 2.5) / fps);
  }
  return Math.max(start, exclusive - 2.5 * fd);
}

export function sceneTriggerTime(
  start: number,
  end: number,
  startFrame: number | null | undefined,
  endFrame: number | null | undefined,
  fps: number,
): number {
  const fd = 1 / fps;
  const exclusive = sceneExclusiveEnd(end, endFrame, fps);
  if (endFrame != null && startFrame != null && endFrame > startFrame) {
    return Math.max(start, (endFrame - 3.5) / fps);
  }
  return Math.max(start, exclusive - 3.5 * fd);
}
