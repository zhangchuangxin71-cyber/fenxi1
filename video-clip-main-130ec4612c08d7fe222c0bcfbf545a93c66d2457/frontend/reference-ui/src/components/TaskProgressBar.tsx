import { useId, useMemo, type CSSProperties } from "react";

/**
 * 进度条样式（对齐设计稿 01–07），按真实任务选用：
 * 01 flow     — 画面切镜检测
 * 02 particle — 语义 AI 分析
 * 03 combo    — 多阶段总览（含真实步骤）
 * 04 neon     — 切割 / 生成片段
 * 05 thin     — 保存媒资 / 导出
 * 06 phased   — 分阶段流水线
 * 07 wave     — 抗闪切镜
 */
export type ProgressStyle =
  | "flow"
  | "particle"
  | "combo"
  | "neon"
  | "thin"
  | "phased"
  | "wave";

export type ProgressTone = "busy" | "ok" | "fail";

export interface ProgressPhase {
  id: string;
  label: string;
}

export interface TaskProgressBarProps {
  percent: number;
  style?: ProgressStyle;
  tone?: ProgressTone;
  /** 真实任务状态文案，如「正在切割片段…」 */
  label?: string;
  /** 次要信息：耗时、就绪数等（来自真实任务，非设计稿文案） */
  meta?: string;
  /** combo / phased 的阶段（调用方传入真实步骤） */
  phases?: ProgressPhase[];
  phaseIndex?: number;
  compact?: boolean;
  className?: string;
}

/** 执行切割页阶段 */
export const CUT_EXEC_PHASES: ProgressPhase[] = [
  { id: "prep", label: "准备" },
  { id: "cut", label: "切割" },
  { id: "done", label: "完成" },
];

/** 自动检测 → 确认切割流水线 */
export const DETECT_PIPELINE_PHASES: ProgressPhase[] = [
  { id: "parse", label: "解析" },
  { id: "detect", label: "检测" },
  { id: "cut", label: "切割" },
  { id: "done", label: "完成" },
];

function clampPct(n: number): number {
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

function derivePhaseIndex(percent: number, count: number): number {
  if (count <= 0) return 0;
  if (percent >= 100) return count - 1;
  return Math.min(count - 1, Math.floor((percent / 100) * count));
}

/** 按真实任务类型选择进度条样式 */
export function progressStyleForTask(task: {
  kind:
    | "detect-content"
    | "detect-adaptive"
    | "detect-semantic"
    | "cutting"
    | "exporting"
    | "pipeline"
    | "idle";
}): ProgressStyle {
  switch (task.kind) {
    case "detect-content":
      return "flow";
    case "detect-adaptive":
      return "wave";
    case "detect-semantic":
      return "particle";
    case "cutting":
      return "neon";
    case "exporting":
      return "thin";
    case "pipeline":
      return "combo";
    default:
      return "flow";
  }
}

function ParticleBars({ pct, count = 36 }: { pct: number; count?: number }) {
  return (
    <div className="task-prog-particle" aria-hidden>
      {Array.from({ length: count }).map((_, i) => {
        const filled = (i + 1) / count <= pct / 100;
        const h = 28 + ((i * 47) % 72);
        return (
          <span
            key={i}
            className={filled ? "is-on" : undefined}
            style={{
              height: `${h}%`,
              animationDelay: `${(i % 9) * 0.07}s`,
            }}
          />
        );
      })}
    </div>
  );
}

function WaveBars({ pct, count = 40 }: { pct: number; count?: number }) {
  return (
    <div className="task-prog-wavebars" aria-hidden>
      {Array.from({ length: count }).map((_, i) => {
        const filled = (i + 1) / count <= pct / 100;
        const h = 22 + Math.abs(Math.sin(i * 0.55)) * 78;
        return (
          <span
            key={i}
            className={filled ? "is-on" : undefined}
            style={{
              height: `${h}%`,
              animationDelay: `${(i % 8) * 0.06}s`,
            }}
          />
        );
      })}
    </div>
  );
}

export function TaskProgressBar({
  percent,
  style = "flow",
  tone = "busy",
  label,
  meta,
  phases = CUT_EXEC_PHASES,
  phaseIndex,
  compact = false,
  className = "",
}: TaskProgressBarProps) {
  const pct = clampPct(percent);
  const activePhase = phaseIndex ?? derivePhaseIndex(pct, phases.length);
  const uid = useId().replace(/:/g, "");
  const cssVars = useMemo(
    () =>
      ({
        ["--tp-pct" as string]: `${pct}%`,
        ["--tp-phase-count" as string]: String(Math.max(1, phases.length)),
      }) as CSSProperties,
    [pct, phases.length],
  );

  const showPctBeside =
    style === "flow" ||
    style === "particle" ||
    style === "neon" ||
    style === "thin" ||
    style === "wave";

  return (
    <div
      className={`task-prog task-prog-${style} task-prog-${tone}${
        compact ? " is-compact" : ""
      }${className ? ` ${className}` : ""}`}
      style={cssVars}
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label || "任务进度"}
    >
      {style === "flow" && (
        <div className="task-prog-row">
          <div className="task-prog-track">
            <div className="task-prog-fill">
              <i className="task-prog-comet" aria-hidden />
            </div>
          </div>
          {showPctBeside && (
            <strong className="task-prog-pct">{Math.round(pct)}%</strong>
          )}
        </div>
      )}

      {style === "particle" && (
        <div className="task-prog-row">
          <ParticleBars pct={pct} />
          <strong className="task-prog-pct">{Math.round(pct)}%</strong>
        </div>
      )}

      {style === "combo" && (
        <div className="task-prog-combo">
          <div
            className="task-prog-ring"
            style={{ ["--ring" as string]: `${pct}` } as CSSProperties}
          >
            <strong>{Math.round(pct)}</strong>
            <span>%</span>
          </div>
          <div className="task-prog-combo-main">
            <div className="task-prog-track">
              <div className="task-prog-fill" />
            </div>
            <ol className="task-prog-steps">
              {phases.slice(0, 4).map((p, i) => {
                const allDone = pct >= 100 || tone === "ok";
                const state = allDone
                  ? "done"
                  : i < activePhase
                    ? "done"
                    : i === activePhase
                      ? "active"
                      : "todo";
                return (
                  <li key={p.id} className={state}>
                    <i aria-hidden>{state === "done" ? "✓" : i + 1}</i>
                    <span>{p.label}</span>
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      )}

      {style === "neon" && (
        <div className="task-prog-row">
          <div className="task-prog-track task-prog-neon-track">
            <div className="task-prog-fill">
              <i className="task-prog-neon-head" aria-hidden />
            </div>
          </div>
          <strong className="task-prog-pct">{Math.round(pct)}%</strong>
        </div>
      )}

      {style === "thin" && (
        <div className="task-prog-row">
          <div className="task-prog-thin">
            <div className="task-prog-thin-line">
              <i className="task-prog-thin-dot" aria-hidden />
            </div>
          </div>
          <strong className="task-prog-pct">{Math.round(pct)}%</strong>
        </div>
      )}

      {style === "phased" && (
        <div className="task-prog-phases">
          {phases.map((p, i) => {
            const state =
              i < activePhase ? "done" : i === activePhase ? "active" : "todo";
            const localPct =
              state === "done"
                ? 100
                : state === "active"
                  ? Math.max(
                      10,
                      Math.min(
                        100,
                        ((pct / 100) * phases.length - i) * 100,
                      ),
                    )
                  : 0;
            return (
              <div key={p.id} className={`task-prog-phase ${state}`}>
                <div className="task-prog-phase-bar">
                  <span style={{ width: `${localPct}%` }} />
                  {state === "active" && (
                    <b className="task-prog-phase-bubble">
                      {Math.round(pct)}%
                    </b>
                  )}
                </div>
                <em>
                  {state === "done" ? "✓ " : ""}
                  {p.label}
                </em>
              </div>
            );
          })}
        </div>
      )}

      {style === "wave" && (
        <div className="task-prog-row">
          <WaveBars pct={pct} />
          <strong className="task-prog-pct">{Math.round(pct)}%</strong>
        </div>
      )}

      {(label || meta) && (
        <div className="task-prog-foot">
          {label ? <span className="task-prog-label">{label}</span> : <span />}
          {meta ? <em className="task-prog-meta-em">{meta}</em> : null}
        </div>
      )}

      {/* SVG defs id uniqueness unused after wavebars; keep uid for future */}
      <span className="task-prog-uid" hidden data-uid={uid} />
    </div>
  );
}
