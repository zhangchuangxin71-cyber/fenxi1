/** 片源宽高比元数据（供 data-preview-fit 等钩子；不再驱动预览舞台外框尺寸） */

export type PreviewFit = "landscape" | "portrait" | "ultrawide";

export interface PreviewLayout {
  /** CSS aspect-ratio 字符串，仅作元数据 */
  aspect: string;
  /** 无单位宽高比 w/h */
  ratio: number;
  fit: PreviewFit;
}

const DEFAULT: PreviewLayout = {
  aspect: "16 / 9",
  ratio: 16 / 9,
  fit: "landscape",
};

/**
 * 按片源分辨率分档。舞台外框由 CSS 流体槽位决定，
 * 视频在槽位内 object-fit: contain，本函数不再决定舞台宽高。
 */
export function computePreviewLayout(
  width?: number | null,
  height?: number | null,
): PreviewLayout {
  const w = Number(width);
  const h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
    return DEFAULT;
  }
  const ratio = w / h;
  let fit: PreviewFit = "landscape";
  if (ratio < 0.9) fit = "portrait";
  else if (ratio > 2.0) fit = "ultrawide";
  return {
    aspect: `${Math.round(w)} / ${Math.round(h)}`,
    ratio,
    fit,
  };
}
