# Skill 排版烟测报告

> 烟测日期：2026-08-21
>
> 目标：确认 `skill_driven` 确实使用迁入的 `gzh-design` 与 `xiaowan` 固定资产和受限 ReAct 工具循环生成富组件 HTML，而不是调用旧 `professional-clean` 排版器。

## 结论

本次实现通过。Skill 成功路径的 HTML 使用 `skill-component-assembler-v1`，由 `SkillLayoutPlan` 决定主题和组件，经过 GZH 安全校验、正文冻结、图片证据和 Xiaowan policy 后交付。该路径不导入、不调用旧 `LayoutEngine`。

人工查看截图时，Skill 结果与 `professional-clean` 对照有明显差异：首屏、导读、章节编号、强调卡、引用、列表、图片容器和结尾组件均有主题化结构；教程、制度解读、故事三类文章分别呈现绿色卡片、红白社论、留白叙事风格。

## 自动化验证

| 项目 | 结果 |
| --- | --- |
| 全量单元/集成回归 | `204 passed, 1 skipped` |
| Ruff | 通过 |
| Mypy | 通过，100 个源文件 |
| 六套 GZH 主题 | 全部生成 HTML、桌面截图和 390px 截图 |
| 三类固定文章 fixture | 教程、制度解读、故事全部通过 |
| 正文保真 | 每个 fixture 的 source/rendered 字符数一致 |
| 移动端 | 390px 无横向溢出 |
| Skill 组件签名 | 每个 fixture 至少 7 类语义，主题专属语义至少 7 类 |
| 工具循环 | fake model、候选伪造、跨主题组件和无 H1 边界测试通过 |
| 开发面板 | 桌面/移动端无横向溢出、无 console error、折叠状态保持 |

六主题画廊入口：`docs/smoke-artifacts/skill-rendering-gallery.json`。三类文章结果：`docs/smoke-artifacts/skill-rendering-article-cases.json`。

## 真实 Ark Skill 烟测

脚本：`tests/manual/live_skill_rendering_smoke.py`。

第二次真实调用在预算调整后完成：

- 状态：`completed`
- Agent 步数：9
- 工具调用：12
- 主题：`moyu-ticket`
- 输入正文字符：2743
- 输出正文字符：2743
- 图片：4 张，URL、顺序和标注一致
- 主题专属组件语义：8 类
- 阻断错误：0
- 过程曾出现一次 Xiaowan 装饰预算警告，Agent 返工后通过

结果：`docs/smoke-artifacts/skill-rendering-live-20260821-132604.html`，摘要：`docs/smoke-artifacts/skill-rendering-live-20260821-132604.json`。

第一次真实调用因原默认预算只有 8 步/12 工具，在完成最终校验前降级；该失败案例保留在 `skill-rendering-live-20260821-132354.json`，用于说明预算边界，随后将默认预算调整为 12 步/16 工具。

## 完整工作流与开发面板

脚本：

- `tests/manual/live_llm_layout_smoke.py`
- `tests/manual/verify_skill_rendering_panel.py`

完整 Responses-like 工作流自动审批 5 轮后完成，生成：

- 文本增量事件：117 个
- 排版 Agent LLM trace：16 条
- Agent 工具 trace：11 条
- 必需 Skill/tool 活动：全部存在
- 最终 HTML：`docs/smoke-artifacts/skill-rendering-workflow-20260821-133704.html`

开发面板回放在桌面和移动端均通过：

- 5 个独立响应轮次
- 16 张可交互工具/Skill 活动卡
- 15 张 LLM Trace 卡、19 张工具 Trace 卡
- 折叠状态在重绘后保持
- 最终 HTML artifact 可展开渲染
- `console_errors=[]`，无横向溢出

截图：`docs/smoke-artifacts/skill-rendering-panel-desktop-20260821-134304.png`、`skill-rendering-panel-mobile-20260821-134304.png`。

## `llm_decide` 回归烟测

脚本：`tests/manual/live_llm_decide_smoke.py`。

真实 Ark 调用从动态 enum 的 8 个既有主题中选择 `moyu-green`，选择器显式传入 `thinking=False`；随后由 `deterministic-v1` 生成并校验 14133 字符 HTML。结果：

- `docs/smoke-artifacts/llm-decide-smoke-20260821-134438.html`
- `docs/smoke-artifacts/llm-decide-smoke-20260821-134438.json`

这证明 `skill_driven` 的重构没有破坏 `llm_decide` 独立模式；两者职责和结果来源不同。

## 当前边界

- 当前生产 Skill 白名单只有 `gzh_design` 和 `xiaowan_layout`；其他开源 Skill 只保留调研结论，未进入运行时。
- Skill Agent 不写临时文件、不联网、不下载图片、不使用 MCP；所有 HTML 组件在内存中装配。
- `skill_driven` 失败时仍按 `llm_decide -> deterministic -> legacy` 降级。
- 真实烟测 HTML 中的图片 URL 来自固定 artifact；URL 失效时截图可能显示图片占位，但不会改变 URL、顺序和标注校验。
