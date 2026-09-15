# `llm_decide` 排版主题选择烟测

## 运行信息

- 日期：2026-08-21
- 模式：`HTML_LAYOUT_RENDERER=llm_decide`
- 数据：无参考文档，验证通识生成路径
- 运行 session：`layout-smoke-20260821053148`
- 结果：`completed`
- 交互轮次：5（素材方向、任务书、大纲、正文各一次审批）

## 关键结果

- 任务书、Markdown 大纲、未排版正文、5 个图片 URL 和最终 HTML 均生成成功。
- 最终 HTML 长度约 15,486 字符，HTML validator 报告有效且无 warning。
- 主题目录工具返回 8 个已注册主题；主题选择 LLM 使用 strict 动态 enum，最终选择
  `professional-clean`。
- 主题选择 LLM 的 `html_layout_theme` 调用成功，向用户输出了专业的主题选择解释文本。
- SSE 中观察到 `wechat_layout.describe_supported_themes`、
  `wechat_layout.select_theme_with_llm`、`wechat_layout.render_markdown` 和
  `wechat_layout.validate_html` 等工具/skill activity；debug trace 中保留了工具参数、结果和
  `html_layout_theme` 的原始输入输出。
- 未出现结构化输出修复、排版降级或服务错误。

完整原始 SSE、debug trace 和产物摘要保存在本机临时文件：
`/tmp/wechat-article-llm-layout-smoke.json`。该文件可能包含模型输入和生成内容，不提交到仓库。

## 模型工具契约探针

在业务代码提交后，新增了独立契约测试
`tests/contract/test_ark_general_tool_contract.py`，用于验证未来通用 Agent Engine 所依赖的
function tool 请求和 SSE 事件边界。该测试不修改业务代码。

探针确认：

- 当前 Ark 网关能够按 Responses 请求格式发送 `tools`、`tool_choice` 和
  `max_tool_calls`。
- 当前流解析器仅处理项目既有的文本、reasoning、usage 和特定搜索事件；尚未把通用
  `function_call` 与参数增量转换为可供 ReAct 循环消费的结构化事件。
- 本次 `llm_decide` 不依赖通用 function tool 调用。主题目录由代码预先生成并注入提示词，
  模型通过 strict structured output 选择动态 enum，因此当前实现不受上述边界影响。
- 后续实现 Agent Engine 时，必须先扩展 function-call 事件解析、工具执行结果回填和下一轮
  模型调用契约，不能只依赖请求中已经能够发送 `tools` 这一点。
