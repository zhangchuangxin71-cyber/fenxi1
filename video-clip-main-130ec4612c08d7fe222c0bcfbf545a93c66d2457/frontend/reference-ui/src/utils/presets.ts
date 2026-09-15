import type { Detector, Sensitivity } from "../api/types";

export const SENSITIVITY_PRESETS: Record<
  Detector,
  Record<Sensitivity, { threshold: number; minSceneLen: number; sampleFps?: number }>
> = {
  content: {
    fine: { threshold: 22, minSceneLen: 1.0 },
    standard: { threshold: 35, minSceneLen: 2.0 },
    coarse: { threshold: 45, minSceneLen: 3.0 },
  },
  adaptive: {
    fine: { threshold: 22, minSceneLen: 1.0 },
    standard: { threshold: 35, minSceneLen: 2.0 },
    coarse: { threshold: 45, minSceneLen: 3.0 },
  },
  semantic: {
    fine: { threshold: 1, minSceneLen: 1.5, sampleFps: 1.5 },
    standard: { threshold: 1, minSceneLen: 3.0, sampleFps: 1.0 },
    coarse: { threshold: 1, minSceneLen: 6.0, sampleFps: 0.5 },
  },
};

const PRESET_KEYS: Sensitivity[] = ["fine", "standard", "coarse"];

function near(a: number, b: number, eps = 0.001): boolean {
  return Math.abs(a - b) < eps;
}

/** 数值是否落在该算法的精准/标准/粗略任一预设上 */
export function isPresetThreshold(detector: Detector, value: number): boolean {
  const presets = SENSITIVITY_PRESETS[detector];
  return PRESET_KEYS.some((k) => presets[k].threshold === value);
}

export function isPresetMinSceneLen(detector: Detector, value: number): boolean {
  const presets = SENSITIVITY_PRESETS[detector];
  return PRESET_KEYS.some((k) => near(presets[k].minSceneLen, value));
}

export function isPresetSampleFps(detector: Detector, value: number): boolean {
  const presets = SENSITIVITY_PRESETS[detector];
  return PRESET_KEYS.some((k) => {
    const fps = presets[k].sampleFps;
    return fps != null && near(fps, value);
  });
}

export function formatPresetValue(
  raw: string,
  isRecommended: boolean,
): string {
  return isRecommended ? `推荐 ${raw}` : raw;
}
