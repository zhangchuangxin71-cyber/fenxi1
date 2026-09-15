# 微信公众号文章 Agent 真实烟测报告

日期：2026-08-12（UTC）

## 1. 结论

真实火山方舟语言模型、Seedream 图片模型、检索服务、PostgreSQL、LangGraph Agent Server、
Responses-like adapter 和开发面板已经完成联调。烟测跑通了首次完整生成、四处 HITL、正文 revise、
完成后的新业务 revision、断线后继续、pending supersede、stale interrupt、运行中和审批中 cancel，
并生成了包含四张真实图片 URL 的最终 HTML。

最终 revise 产物：

- HTML：14,671 字符；历史 HTML 副本已随旧排版烟测产物统一清理。
- 标题：`平安银行2023年年报解读：经营表现、核心风险与未来看点`
- Markdown：8,979 字符
- HTML：14,671 字符（报告文件为了可独立查看包含完整样式，脱敏后文件大小 28,956 bytes）
- 图片：4 张；数据库只保存 `url/caption/insertion_position`，没有下载图片二进制

报告中的 HTML 副本已移除 Seedream 临时 URL 的 `X-Tos-*` 查询签名，只保留对象路径；真实运行的
浏览器验收在签名有效期内完成，数据库中的业务 artifact 按原样保存模型返回 URL 并受三天 TTL 管理。

## 2. 环境

| 能力 | 烟测配置 |
| --- | --- |
| Adapter | `http://127.0.0.1:8240` |
| Agent Server | `http://127.0.0.1:8242` |
| 开发面板 | `http://127.0.0.1:8245` |
| 检索服务 | `http://127.0.0.1:8220` |
| PostgreSQL | 本地独立 `wechat_article_agent` database |
| 语言模型 | `doubao-seed-2-1-pro-260628` |
| 图片模型 | `doubao-seedream-5-0-pro-260628` |
| Ark Responses | `https://ark.cn-beijing.volces.com/api/v3/responses` |
| Seedream | `https://ark.cn-beijing.volces.com/api/v3/images/generations` |

测试文档为 294 页的《平安银行2023年年度报告》，doc ID 在本报告中缩写为
`5a5908ec...f350`。API key 只从已有环境配置提取到本地 `.env`，未写入代码、示例配置、日志或本报告。

## 3. 完整生成与 HITL

首次 revision（标识均缩写）：

1. adapter 接收完整对话和文档范围，创建确定性 thread 与 revision 1。
2. 文档研读调用真实 meta、route、raw/retrieve；294 页文档走分组检索，保留 coverage warning。
3. 到达任务书审批。断开后发送“继续”，未 resume 或重跑节点，重放相同
   `response_id/interrupt_id` 审批卡片。
4. 任务书 approve 后生成大纲；大纲使用 revise，反馈要求增加“普通投资者如何持续跟踪这些指标”。
5. revise 在同一 revision 覆盖未批准候选，重新到达大纲审批；approve 后生成正文。
6. 正文 approve 后真实生成 4 张 Seedream 图片，并用确定性 renderer 生成 HTML。
7. revision 1 最终为 `completed`，4 张图片，Markdown 11,054 字符，HTML 17,515 字符。

随后发送只修改正文的真实业务请求，创建 revision 2。orchestrator 选择 `article` 入口，继承已批准
素材库、任务书和大纲，清空正文及下游字段。正文要求改得更口语化并把结尾压缩为三个短段落；审批
通过后再次真实生成 4 张图片和最终 HTML。revision 2 从创建到完成约 5 分 12 秒，其中包含人工模拟
审批和四张图片生成等待。

## 4. 恢复、取消与并发边界

以下路径均通过真实 HTTP、Agent Server 和 PostgreSQL 验证：

| 场景 | 结果 |
| --- | --- |
| pending 时输入“继续” | 复用原 ID 重放审批卡片，不执行 `Command(resume=...)` |
| stale `interrupt_id` 审批 | HTTP 409，`STALE_INTERRUPT` |
| pending cancel | HTTP 200，checkpoint 通过 cancel command 退出，artifact 为 `cancelled` |
| 重复 cancel | HTTP 200，幂等返回 `cancelled` |
| cancel 后输入“继续” | 返回已取消说明，不恢复已取消 run |
| 断开 SSE 但后台仍运行 | 立即“继续”返回 HTTP 409，`RUN_IN_PROGRESS` |
| 运行中 cancel | 等待 Agent Server 确认后 HTTP 200；5 秒后 artifact 无继续写入 |
| pending 时自由输入改变主题 | 旧 revision 为 `superseded`，新 revision 从上游阶段启动 |
| 同 thread 并发创建 run | Agent Server `multitask_strategy=reject` 拒绝第二个 run |

烟测还覆盖了一个重要 revision 链边界：revision 4 在 orchestrator 写入前被取消，因此直接父节点没有
任何业务 artifact。修复后 revision 5 会沿 `parent_artifact_id` 找到最近已批准祖先，正确继承素材库、
任务书和大纲并只生成正文。随后主题变更使 revision 5 成为 `superseded`，revision 6 回到文档研读，
在任务书审批点暂停；测试结束后已正式 cancel，没有残留后台生成。

## 5. 协议与可观测性

实际 SSE 包含标准 Responses-like 文本事件、标准 reasoning summary 事件，以及仅三种扩展事件：

- `agent.activity`
- `agent.artifact`
- `agent.interrupt`

正文生成流中可观察到 node/tool 的 running、completed、degraded/failed 状态；HITL 卡片包含 stage、
artifact revision、表单和三个决策。`debug=true` 的 terminal event 一次性返回节点原始输入输出、完整
系统提示词、模型原始结构化输出、工具参数/结果、fallback、错误和耗时；`debug=false` 不创建 collector，
也不返回 debug 字段。

真实 Ark reasoning capability probe 返回了 20 个 reasoning delta、35 个 reasoning 字符和一个 usage
终态事件。任务书、大纲、正文的 thinking 由环境变量独立控制，其余 LLM phase 均禁用 thinking。

早期两次完整生成发生在 adapter 增加终态 usage 汇总之前，产品事件又按设计不持久化，因此不能事后
准确补算 token，报告不伪造该数据。修复后真实 adapter 探针返回标准：

```json
{
  "input_tokens": 379,
  "output_tokens": 5,
  "total_tokens": 384,
  "input_tokens_details": {"cached_tokens": 0},
  "output_tokens_details": {"reasoning_tokens": 0}
}
```

多次 LLM 调用会在同一 `response.completed.response.usage` 中逐字段相加；usage 没有增加自定义事件。

## 6. 开发面板

浏览器在桌面和移动 viewport 验证通过，无控制台错误和横向溢出。面板已实际消费完成结果 SSE：

- 中栏以打字机方式渲染文本、reasoning、activity、artifact 和可点击 HITL 卡片。
- 最终 HTML 在 sandboxed iframe 中显示，标题与 4 张图片位置正确。
- 右栏 terminal 后显示每个 LLM 调用的 prompt/原始输出、节点 I/O 和工具调用。
- “停止接收”只中断浏览器 SSE；“取消 run”调用产品 cancel endpoint，两者语义已分别验证。

## 7. 发现并修复的失败案例

| 失败 | 根因 | 修复与复测 |
| --- | --- | --- |
| Seedream 走 `/responses` 返回 400/403 | 图片模型实际使用 Images Generations API | 改为 `/api/v3/images/generations`，真实生成 4 张图片 |
| 正文节点排队超时 | LLM 公平调度器错误地重复扣减同一 run 的配额 | 修正 permit 生命周期并增加调度测试，失败 checkpoint 重试成功 |
| 失败后重试仍显示旧 response | retry 只 resume 未更新 Graph State response ID | 使用 `Command(update=...)` 重跑失败 task，真实复测通过 |
| running cancel 偶发返回 202 | runtime 已取消但状态确认与 artifact reconcile 存在竞态 | 终态刷新后返回 200，真实复测并确认无迟到写入 |
| 空的 cancelled 直接父 revision 破坏继承 | orchestrator 只查看直接父节点 | 沿 ancestry 找最近已批准祖先，集成与真实复测通过 |
| terminal 缺少标准 usage | normalizer 忽略内部 usage custom event | 聚合到标准 `response.usage`，单元测试与真实探针通过 |
| 非文章短路的终态偶发显示 running | SSE 终态读取早于异步 monitor 回写 artifact | 短路节点结束前同步写 completed，真实探针返回 `workflow_status=completed` |

## 8. 自动化验证

最终交付前执行：

```text
ruff check
ruff format --check
mypy --strict
pytest
WECHAT_RUN_LIVE_CONTRACTS=1 pytest tests/e2e/test_agent_server_runtime.py
langgraph dev contract validation
bash -n scripts/deploy.sh
```

覆盖点包括配置模板精确字段、无 Redis/事件表、prompt 分区与 thinking 白名单、Responses/Ark 严格输出、
检索 raw -> retrieve 契约、Seedream 有限重试、revision/TTL/并发锁、state 体积、cancel command、
开发面板代理和部署文件契约。

## 9. 环境限制

当前开发容器没有 Docker CLI 和 hadolint，因此 Dockerfile/Compose 只做静态契约检查、官方
`langgraph.json` 校验和 shell 语法检查，没有在本机 build 镜像。部署文件按项目现有方式接入根 compose
和 `scripts/deploy.sh`，生产运行仍需在带 Docker 的目标环境完成镜像构建验证。

工作区位于 Windows/9p 挂载，`chmod 600 .env` 无法改变显示的 mode；生产部署脚本会在真实 Linux
文件系统强制检查并设置 0600。该限制不影响 `.env` 未进入 git 和未写入示例配置。
