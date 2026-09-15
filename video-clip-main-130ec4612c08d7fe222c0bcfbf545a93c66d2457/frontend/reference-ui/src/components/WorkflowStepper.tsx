interface Step {
  id: string;
  label: string;
  note?: string;
}

interface Props {
  steps: Step[];
  current: number;
  /** 点击可回退的步骤（通常为已完成的步骤） */
  onStepClick?: (index: number) => void;
  /** 允许点击的最大步骤下标（默认 current，含当前步） */
  maxClickable?: number;
}

export function WorkflowStepper({
  steps,
  current,
  onStepClick,
  maxClickable,
}: Props) {
  const clickLimit =
    maxClickable !== undefined ? maxClickable : onStepClick ? current : -1;

  return (
    <ol
      className="workflow-stepper workflow-stepper-wide design-stepper"
      data-steps={steps.length}
      style={{ ["--step-count" as string]: steps.length }}
      aria-label="流程进度"
    >
      {steps.map((step, i) => {
        const state =
          i < current ? "done" : i === current ? "active" : "todo";
        const canClick = Boolean(onStepClick) && i <= clickLimit && i !== current;
        return (
          <li
            key={step.id}
            className={`workflow-step ${state}${canClick ? " is-clickable" : ""}`}
          >
            <button
              type="button"
              className="workflow-step-hit"
              disabled={!canClick}
              aria-current={state === "active" ? "step" : undefined}
              aria-label={`${step.label}${state === "done" ? "（已完成，点击返回）" : ""}`}
              onClick={() => {
                if (canClick) onStepClick?.(i);
              }}
            >
              <span className="workflow-step-index">
                {state === "done" ? "✓" : i + 1}
              </span>
              <div className="workflow-step-text">
                <span className="workflow-step-label">{step.label}</span>
                {step.note ? (
                  <span className="workflow-step-note">{step.note}</span>
                ) : null}
              </div>
            </button>
            {i < steps.length - 1 && (
              <span className="workflow-step-connector" aria-hidden />
            )}
          </li>
        );
      })}
    </ol>
  );
}

/**
 * 四步流程（与界面页面对齐）：
 * 0 选择源片 → 1 选择切片模式 → 2 预览与编辑 → 3 切割与保存
 */
export const DESIGN_STEPS: Step[] = [
  { id: "import", label: "选择源片", note: "粘贴 Key/URL 并确认" },
  { id: "mode", label: "选择切片模式", note: "手动切片或自动分镜" },
  { id: "edit", label: "预览与编辑", note: "预览并调整片段" },
  { id: "cut", label: "切割与保存", note: "任务处理与结果" },
];

/** @deprecated 使用 DESIGN_STEPS */
export const MANUAL_STEPS: Step[] = DESIGN_STEPS;

export function buildAutoSteps(_opts?: {
  uploaded?: boolean;
  detecting?: boolean;
  sceneCount?: number;
  pendingCount?: number;
  cutting?: boolean;
  doneCount?: number;
}): Step[] {
  return DESIGN_STEPS;
}

export function autoWorkflowIndex(opts: {
  detecting: boolean;
  sceneCount: number;
  cutting: boolean;
  doneCount: number;
  /** 已选模式进入编辑 */
  modeSelected?: boolean;
  uploaded?: boolean;
}): number {
  if (opts.cutting || opts.doneCount > 0) return 3;
  if (opts.sceneCount > 0 || opts.modeSelected) return 2;
  if (opts.detecting) return 2;
  if (opts.uploaded) return 1;
  return 0;
}

export function manualWorkflowIndex(opts: {
  uploaded: boolean;
  modeSelected: boolean;
  hasSegments: boolean;
  cutting: boolean;
  done: boolean;
}): number {
  if (opts.cutting || opts.done) return 3;
  if (opts.modeSelected || opts.hasSegments) return 2;
  if (opts.uploaded) return 1;
  return 0;
}
