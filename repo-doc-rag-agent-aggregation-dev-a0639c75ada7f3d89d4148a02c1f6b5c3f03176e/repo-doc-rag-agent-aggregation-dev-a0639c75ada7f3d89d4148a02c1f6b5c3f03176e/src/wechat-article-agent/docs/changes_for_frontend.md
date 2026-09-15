本文说明本轮微信公众号文章 Agent 的前端适配变化。它不是产品变更日志，而是给前端调用者的实现指南。
请与 [api.md](api.md) 和 [frontend-integration.md](frontend-integration.md) 一起阅读。

# 2026年8月20日更新说明

## 前端适配说明：素材调研与联网搜索

本轮把原产品概念“文档研读”升级为“素材调研”。“文档研读”只描述了读取用户文档这一条来源，而新版
节点还会理解主题背景、筛选历史素材，并在服务端已启用联网且现有素材不足时搜索网络来源。继续在界面上
显示“文档研读”，会让没有上传文档的用户误以为系统无法工作，也会让用户无法理解联网搜索为何出现在
这个阶段。因此，**产品界面中的阶段名称应统一改为“素材调研”**；只有确实正在读取某一份用户文档的
工具活动，才使用“研读文档”这样的动作名称。

这只是产品术语和展示文案升级，不是协议字段改名。为保持兼容，`docs_research`、
`docs_research_clarification` 等内部 `node/stage` 值暂时保持不变，前端不能自行把事件 JSON 中的值改成
`material_research`。前端不需要新增接口，也不能直接调用 Ark；新增状态仍通过既有的
`POST /v1/responses` SSE 和三种 `agent.*` 事件传输。

前端本轮必须适配四项变化：

1. 把用户可见的“文档研读”阶段和相关文案统一更新为“素材调研”；
2. 素材方向卡中的 `target/about` 使用新的展示语义；
3. 把联网搜索的 `agent.activity(kind=tool)` 显示为工具活动；
4. 展示 `material_sources` 来源清单，并支持可选的素材冲突审批卡。

### 产品文案迁移表

协议值不变，前端只替换用户可见文案：

| 内部标识或旧文案 | 推荐用户文案 | 说明 |
| --- | --- | --- |
| `docs_research` / 文档研读 | 素材调研 | 整个阶段的统一名称 |
| `docs_research_clarification` | 确认素材调研方向 | HITL 卡片和等待状态 |
| 等待确认研读方向 | 等待确认素材调研方向 | 节点活动或进度文案 |
| 文档研读完成 | 素材调研完成 | 阶段完成文案 |
| `document_material_worker` | 研读用户文档 | 只用于确实读取用户文档的子活动 |
| `web_material_worker` | 搜索网络素材 | 联网工具活动 |
| `finalize_material_research` | 整理并完成素材调研 | 合并、压缩、冲突处理后的收尾阶段 |

不要向用户显示 `docs_research`、`document_material_worker` 等内部英文标识，也不要为了改文案而改变 reducer
用于匹配的 `stage/node` 常量。

## 一、素材调研方向卡

`docs_research_clarification` 的 JSON 外形和提交方式不变，但字段语义已经从“选择要读哪些文档”调整为
“定义本轮要搜集什么素材”：

- `target`：素材主题，即用户想围绕什么对象、事件或问题搜集素材；
- `about`：关注重点，即针对该主题重点查找哪些事实、观点、案例或表达方式。

二者共同形成素材筛选边界。`target` 不是底层搜索 query，也不等同于最终文章标题；`about` 不是文章受众、
语气或写作目标。受众、目标、语气等更细的创作要求会在后续任务书审批中确认。这样拆分的目的是让素材
调研阶段只回答一个问题：“后续写作应该收集哪些内容”，避免与任务书节点职责重叠。

### 可直接采用的卡片文案

建议不要直接把 JSON 渲染成 `target: ... / about: ...`。推荐使用下面的产品文案：

| 界面位置 | 推荐文案 |
| --- | --- |
| 卡片标题 | 确认素材调研方向 |
| 卡片说明 | 请选择本次要搜集的素材主题和重点关注方向。确认后，系统将据此筛选用户文档，并在需要且可用时补充网络素材。 |
| A/B/C 选项主文案 | 围绕「{target}」搜集素材 |
| A/B/C 选项辅助文案 | 重点关注：{about} |
| 选项范围提示 | 与该方向无关的内容不会进入后续写作素材。 |
| 自定义选项标题 | 其他：自定义素材调研方向 |
| `target` 输入框标题 | 素材主题 |
| `target` placeholder | 例如：电影《牛来》的传播现象 |
| `about` 输入框标题 | 重点关注 |
| `about` placeholder | 例如：剧情背景、观众评价和近期网络讨论 |
| 提交按钮 | 确认并开始调研 |

如果界面希望把一个选项合并成一句话，建议使用：

```text
本次将围绕「{target}」搜集素材，重点关注「{about}」。
```

这比“搜集与某主题有关的素材”更简洁，也避免用户把 `about` 误解为第二个主题。选项内容较长时应允许
自然换行或展开查看，不能只显示省略号而让三个选项无法比较。

“其他”项展开 `target` 与 `about` 两个输入框。选择服务端推荐项只回传 `option_id`；选择自定义项时回传
用户填写的两个字段。推荐项提交后直接进入调研；自定义输入如果仍然含糊，服务端可能再次发送一张新的
方向卡。前端按新的 `interrupt.id` 创建卡片，不覆盖上一张已完成卡片。

审批成功后，建议在对话历史中显示下面的可读记录，而不是“接受 docs_research”或“选择 B”：

```text
已确认素材调研方向：围绕「{target}」搜集素材，重点关注「{about}」。
```

联网是否可用由服务端部署配置控制，本轮卡片没有用户联网开关字段。前端不能因为没有看到联网活动就
判定请求失败：素材可能已经充足、服务端可能关闭联网，或者联网失败后已降级继续。

## 二、联网工具活动

联网搜索通过既有 `agent.activity` 表示：

```json
{"type":"agent.activity","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-20T08:00:20Z","schema_version":"1","activity":{"id":"activity_web_material_01","kind":"tool","name":"ark.web_search","label":"搜索网络写作素材","status":"running","node":"web_material_worker","input_summary":{"categories":4},"output_summary":null,"error":null}}
```

结束时同一 `activity.id` 更新为 `completed` 或 `degraded`：

```json
{"type":"agent.activity","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-20T08:00:42Z","schema_version":"1","activity":{"id":"activity_web_material_01","kind":"tool","name":"ark.web_search","label":"网络写作素材搜索完成","status":"completed","node":"web_material_worker","input_summary":null,"output_summary":{"material_count":3,"query_count":4},"error":null}}
```

前端按 `run_id + activity.id` 原位更新，不能生成两张卡。工具活动应显示在节点时间线之外的工具区域或
对应节点下方。`degraded` 表示系统会使用文档素材或通识继续，不应把整条工作流标记为失败。

## 三、素材来源 artifact

素材调研完成后总会收到一条用户可见的 `material_sources`：

```json
{"type":"agent.artifact","run_id":"run_01","response_id":"resp_02","timestamp":"2026-08-20T08:00:50Z","schema_version":"1","artifact":{"id":"art_01","revision":1,"stage":"material_sources","status":"completed","content":["公司年度报告.pdf","https://example.com/official-report","https://example.com/reference-article"],"summary":{"source_count":3}}}
```

`content` 严格为去重后的 `string[]`：普通字符串是用户文档名，HTTP(S) 字符串是网络来源。建议用一个
可折叠的“参考素材来源”卡片展示，URL 使用安全外链并加 `rel="noopener noreferrer"`。数组可以为空；
不要期待页码、网页摘要、搜索 query 或素材正文。

debug 模式下开发面板还可能收到 `stage=material_library` 的完整原始素材模型。这是开发数据，不是
`material_sources` 的替代品，产品前端不得展示或依赖它。

## 四、素材冲突审批

当用户文档与事实型网络来源存在会影响文章结论的冲突时，服务端先发送
`stage=material_conflicts` artifact，再发送 `stage=material_conflict_review` interrupt。前端将
`material_conflicts.content.conflicts[].sources` 展示成出处对照卡；interrupt 的 `form.fields[].sources`
也会携带同一批面向用户的来源信息，供卡片独立渲染。两者按当前 `artifact_id + revision` 关联，不要把
其他 revision 的冲突来源混入当前卡片。卡片提供以下三个单选项：

| `option_id` | 推荐显示文案 | feedback |
| --- | --- | --- |
| `document_priority` | 以我提供的文档为准 | 可为空 |
| `automatic_authority` | 由系统按来源权威性判断 | 可为空 |
| `custom_feedback` | 按我的意见处理 | 必填 |

三种选择都通过主接口提交 `decision=revise`，没有新的 resume 接口：

```json
{
  "previous_response_id": "resp_02",
  "input": [{"role":"user","content":"以我提供的文档为准。"}],
  "context": {
    "session_id": "frontend-session-1",
    "hitl": {
      "interrupt_id": "int_conflict_01",
      "decision": "revise",
      "feedback": "",
      "selection": {"option_id":"document_priority"}
    }
  }
}
```

`previous_response_id` 必须取产生该卡片的 Response ID，`interrupt_id` 必须取卡片自身 ID。收到 409
`STALE_INTERRUPT` 后把旧卡标为过期，不自动向新卡重提旧选择。

## 五、断线、重放与取消

本轮没有改变既有恢复语义：

- 仅断开 SSE 不取消后台 run；断线期间的 token、reasoning 和临时 activity 不补发；
- 用户输入“继续”时，运行中返回 409，等待审批时重放同一张卡，已完成时重发最终 HTML；
- 重放沿用原 `response_id/interrupt_id`，前端必须 upsert，不能创建重复卡；
- 用户正式调用 cancel 后旧 interrupt 失效；之后的普通输入或“继续”会创建新 revision 并重新路由，
  不能恢复已取消的素材冲突或其他审批卡。

## 六、本轮验收清单

- [ ] 用户视图中的阶段名称已从“文档研读”统一更新为“素材调研”；
- [ ] 内部 `docs_research*` 协议值保持不变，前端只做显示映射；
- [ ] 素材方向 A/B/C 卡按“素材主题 + 重点关注”显示完整的 `target/about`；
- [ ] 自定义项使用两个清晰输入框，提交按钮显示“确认并开始调研”；
- [ ] 审批历史显示可读方向，不出现 `docs_research` 或裸 `option_id`；
- [ ] `ark.web_search` 被渲染为工具活动，不进入节点主时间线；
- [ ] running/completed 使用同一 `activity.id` 原位更新；
- [ ] `material_sources` 文档名和 URL 均可正确展示，空数组不报错；
- [ ] 产品界面不展示 debug-only `material_library`；
- [ ] `material_conflicts` 与后续 interrupt 正确关联；
- [ ] 三种冲突选项都通过主接口提交，且 ID 来自当前卡片；
- [ ] 断线重放不复制来源卡、活动或 HITL 卡；
- [ ] 正式 cancel 后旧卡不可再次提交。

# 2026年8月17日更新说明

## 前端适配说明：意图确认 HITL 与活动事件去重

本轮有两项必须适配的变化：

1. 入口意图识别不再直接用固定文案结束请求，而是可以发送一次“是否要生成微信公众号文章”的 HITL
   选项卡。
2. 后端已修复节点活动重复发送问题。此前“识别请求类型”“整理历史审批偏好”等节点可能重复发送两条
   相同的 `agent.activity`，导致前端节点列表出现两个相同节点；前端需要检查节点总数和进度计算是否仍然正确。

## 一、意图确认 HITL

### 1.1 什么时候出现

当用户首次输入没有明确说要生成微信公众号文章，但输入内容可以转化为公众号主题时，Agent 会暂停并
发送一个 `agent.interrupt`。例如用户输入：

```text
介绍一下这几份养老政策文件。
```

Agent 不会立即回答政策内容，也不会直接提示用户切换到其他 Agent，而是询问用户是否要围绕相关主题
生成公众号文章。

这类确认只用于确认“是否进入微信公众号文章工作流”。它不负责确认受众、语气、篇幅，也不负责选择从
素材调研、任务书还是大纲阶段开始。

### 1.2 事件结构

前端按 `type=agent.interrupt` 和 `interrupt.form.form_type=agent_clarification` 判断这是澄清表单，
再按 `clarification_type=intent_confirmation` 判断它是入口意图确认：

```text
event: agent.interrupt
data: {"type":"agent.interrupt","run_id":"run_01","response_id":"resp_01","timestamp":"2026-08-17T08:00:00Z","schema_version":"1","interrupt":{"id":"int_intent_01","status":"pending","stage":"intent_clarification","artifact_id":"art_01","revision":1,"form":{"form_type":"agent_clarification","clarification_type":"intent_confirmation","title":"确认公众号文章生成方向","description":"请选择一个建议主题，或填写其他主题。","selection_mode":"single","options":[{"id":"A","topic":"养老政策变化与普通家庭的影响"},{"id":"B","topic":"养老政策办理流程与常见问题"},{"id":"C","topic":"基层养老服务的现实难点与改进方向"}],"custom_option":{"enabled":true,"topic_label":"其他文章主题"},"fields":[]}}}
```

前端应保存以下值，不要自行生成：

```ts
type IntentClarificationCard = {
  interruptId: string;       // interrupt.id
  sourceResponseId: string;  // 当前 agent.interrupt 的 response_id
  runId: string;
  artifactId: string;
  revision: number;
  options: Array<{ id: "A" | "B" | "C"; topic: string }>;
  customEnabled: boolean;
};
```

### 1.3 推荐的界面展示

建议将卡片标题显示为 `确认公众号文章生成方向`，说明文字显示为 `请选择一个最符合需求的主题`。

- A、B、C 使用三个可点击的单选卡片，每张卡片突出显示主题名称；
- 每张卡片下面可以显示统一的辅助文案：`围绕“该主题”生成微信公众号文章`；
- 最后一项“其他文章主题”使用输入框或展开式卡片，不要把“其他”伪装成一个没有内容的主题；
- 用户选择 A/B/C 后，提交按钮应显示“围绕该主题生成”；
- 用户选择“其他”后，要求输入非空主题，再启用提交按钮；
- 该卡片是一次性入口确认，成功选择后不要在前端重复创建第二张意图卡。

### 1.4 提交 A/B/C

仍然调用主接口 `POST /v1/responses`，没有单独的 resume 接口。`previous_response_id` 必须使用产生
这张卡片的 `sourceResponseId`，不能使用 session 中任意最新的 Response ID：

```json
{
  "model": "wechat-article-agent",
  "stream": true,
  "previous_response_id": "resp_01",
  "input": [
    {"role": "user", "content": "选择主题 A，围绕养老政策变化与普通家庭的影响生成公众号文章。"}
  ],
  "context": {
    "session_id": "frontend-session-1",
    "user_id": "user-1",
    "kb_id": "kb-1",
    "doc_ids": ["doc-1", "doc-2"],
    "hitl": {
      "interrupt_id": "int_intent_01",
      "decision": "revise",
      "feedback": "",
      "selection": {"option_id": "A"}
    }
  }
}
```

这里 `decision=revise` 是协议层对“提交结构化澄清选择”的统一表示，不代表用户正在修改一篇已经生成
的文章。前端界面可以把它显示为“确认主题”，不要显示内部的 `revise` 字样。

### 1.5 提交“其他主题”

```json
{
  "previous_response_id": "resp_01",
  "input": [
    {"role": "user", "content": "我想写乡村教育数字化的实践难点。"}
  ],
  "context": {
    "session_id": "frontend-session-1",
    "hitl": {
      "interrupt_id": "int_intent_01",
      "decision": "revise",
      "feedback": "",
      "selection": {
        "option_id": "custom",
        "topic": "乡村教育数字化的实践难点"
      }
    }
  }
}
```

自定义主题可能会由服务端再做一次简单意图判断，但不会再次发起意图确认卡。若仍无法确认是公众号
文章意图，服务端会返回普通的可读提示，前端按错误/结束状态展示即可，不要自行伪造新的 HITL 卡片。

### 1.6 卡片生命周期与重复提交

- 收到卡片：以 `interrupt.id` 为 key 写入或更新卡片，状态为 `pending`；
- 用户点击提交：立即将卡片置为 `submitting`，禁用按钮，避免重复请求；
- 收到新的 `response.created`：提交成功后将旧卡片置为 `resolved`；
- 返回 `409 STALE_INTERRUPT`：将卡片置为 `stale`，提示用户该卡已过期，不要自动再次提交；
- 网络中断后用户输入“继续”：服务端可能重发同一个 `interrupt.id`，前端必须原位 upsert，不能复制一张
  新卡；
- 用户明确 cancel 后再输入“继续”：这是新一轮业务请求，旧卡应标记为 `cancelled` 或 `stale`，不能
  重放旧卡片。

## 二、节点活动不再重复展示

### 2.1 本轮后端修复的重点

此前后端在部分节点执行过程中会错误地重复发送相同的节点活动事件。例如“识别请求类型”“整理历史审批
偏好”以及其他普通工作流节点，前端可能收到两条看起来相同的 `agent.activity`，于是节点活动列表中出现
两个相同节点，进而影响“正在进行 2/8 步骤”这类进度展示。

该问题已经在后端修复：同一节点在一次 `run_id` 内只对应一项节点活动，不再因为后端重复发送而产生两个
相同节点。节点的开始与结束仍然可能是同一个活动 ID 的多次状态更新，详见下节；这不表示节点数量增加。

请前端重点检查以下代码：

- 是否按照收到的 `agent.activity` 事件条数计算总步骤数；如果是，需要改为按当前 `run_id` 下去重后的
  节点活动计算，不能把 running/completed 或历史重复事件算成两个步骤；
- 是否用固定的 8 步、节点名称数组或重复节点名称判断进度；应以实际工作流节点集合为准，并确认新版节点
  数量与顺序；
- 是否在收到一个节点的 completed 事件后又追加一行，而不是更新原有节点；
- 是否把不同 `run_id` 的节点合并到了同一个活动列表；完成后的新 revision 应创建新的步骤列表；
- 是否有“当前进行第 N 步”的缓存，在新 run 开始时没有清零。

建议进度计算使用去重后的节点活动：

```ts
const nodeActivities = [...run.activities.values()]
  .filter(activity => activity.kind === "node");
const completedCount = nodeActivities.filter(
  activity => ["completed", "degraded"].includes(activity.status),
).length;
const totalCount = expectedNodeCountForRun(run); // 不要直接使用 SSE 事件条数
const progressText = `正在进行 ${Math.min(completedCount + 1, totalCount)}/${totalCount} 步骤`;
```

`expectedNodeCountForRun` 可以使用前端与后端约定的当前工作流节点集合，但不能把同一个节点的状态事件重复
计数。若前端无法可靠预先知道总节点数，建议展示“正在进行：识别请求类型”或“已完成 N 个阶段”，不要根据
事件数量猜测总步骤数。

### 2.2 正确理解一项活动

一次节点活动通常会至少发送两次 `agent.activity`：

1. `status=running`：节点开始执行；
2. `status=completed`、`degraded` 或 `failed`：同一节点结束。

两次事件的 `activity.id` 相同。它们不是两项活动，也不是 SSE 重复发送。前端必须把第二次事件视为对
第一条记录的更新。

示例：

```text
event: agent.activity
data: {"type":"agent.activity","run_id":"run_01","response_id":"resp_01","activity":{"id":"activity_intent_01","kind":"node","name":"intent_router","label":"识别请求类型","status":"running","node":"intent_router","input_summary":null,"output_summary":null,"error":null}}

event: agent.activity
data: {"type":"agent.activity","run_id":"run_01","response_id":"resp_01","activity":{"id":"activity_intent_01","kind":"node","name":"intent_router","label":"识别请求类型","status":"completed","node":"intent_router","input_summary":null,"output_summary":{"is_wechat_article_intent":true},"error":null}}
```

### 2.3 推荐 reducer

不要使用数组 `push` 直接追加。推荐按 `run_id` 分组后使用 Map：

```ts
type RunView = {
  activities: Map<string, AgentActivity>;
};

function reduceActivity(run: RunView, event: any) {
  const activity = event.activity;
  const previous = run.activities.get(activity.id);
  run.activities.set(activity.id, {
    ...previous,
    ...activity,
  });
}
```

如果使用数组，必须按 `activity.id` 查找并替换：

```ts
const index = activities.findIndex(item => item.id === incoming.id);
if (index === -1) activities.push(incoming);
else activities[index] = {...activities[index], ...incoming};
```

注意：`activity.id` 只在同一个 `run_id` 内作为活动 key 使用。不同 `run_id` 即使名称相同，也必须显示
为不同 revision 的活动，不能跨 run 合并。

### 2.4 活动列表的推荐展示

节点活动建议固定显示在当前 run 的顶部，并按首次出现顺序排列；状态更新只改变同一行的图标、状态和耗时：

- `running`：灰色/蓝色进行中图标，可显示“正在识别请求类型”；
- `completed`：绿色完成图标，并显示耗时；
- `degraded`：黄色警告图标，显示“已完成（降级）”及摘要；
- `failed`：红色错误图标，显示用户可理解的错误；
- `cancelled`：灰色停止图标。

节点活动是工作流状态，不要把它写入聊天消息，也不要把每次状态更新显示成一条新消息。

## 三、工具与 skill 活动的兼容方式

未来 Agent 可能接入检索工具、内容工具或 skill。工具和 skill 不会新增 `agent.tool`、`agent.skill` 等
事件名，仍统一使用 `agent.activity`。这样前端只需要维护一套 SSE parser。

### 3.1 用 `kind` 区分节点和工具/skill

至少按 `activity.kind` 判断：

```ts
function activityCategory(activity: AgentActivity): "node" | "tool" | "skill" | "unknown" {
  if (activity.kind === "node") return "node";
  if (activity.kind === "skill") return "skill";
  if (activity.kind === "tool") return "tool";
  return "unknown";
}
```

当前公共协议要求 `kind` 支持 `node`、`tool` 和 `skill`。本项目的 `skill_driven` 排版已经实际发送
`kind=skill`；前端应优先按 `kind` 分支渲染，不要仅凭 `name` 猜测业务语义，`name` 只作为兼容兜底。

工具示例：

```json
{
  "type": "agent.activity",
  "run_id": "run_01",
  "response_id": "resp_01",
  "activity": {
    "id": "activity_retrieve_01",
    "kind": "tool",
    "name": "retrieval.search",
    "label": "检索与当前主题相关的文档片段",
    "status": "running",
    "node": "article",
    "input_summary": null,
    "output_summary": null,
    "error": null
  }
}
```

当前排版 Skill 示例：

```json
{
  "type": "agent.activity",
  "run_id": "run_01",
  "response_id": "resp_01",
  "activity": {
    "id": "activity_style_skill_01",
    "kind": "skill",
    "name": "wechat_layout.skill_driven",
    "label": "使用排版 Agent 与 Skill",
    "status": "completed",
    "node": "render_html",
    "input_summary": null,
    "output_summary": {"theme_id": "gzh-editorial", "steps": 8, "tool_calls": 7, "compressed": false},
    "error": null
  }
}
```

### 3.2 推荐视觉差异

节点、工具和 skill 都属于运行链路，但用户理解它们的方式不同，建议不要使用完全相同的卡片：

| 类型 | 推荐视觉 | 面向用户的重点 |
|---|---|---|
| `node` | 左侧时间线、圆点或阶段标签 | 工作流正在“进入/完成哪个阶段” |
| `tool` | 插件/扳手图标、较窄的操作卡 | Agent 正在调用什么外部能力 |
| `skill` | 魔法棒/积木图标、带能力名称的卡片 | Agent 正在应用哪项可复用能力 |
| `unknown` | 中性图标和原始 label | 兼容未来扩展，不阻塞整条流 |

建议状态文案：

- 节点：`正在生成文章大纲`、`文章大纲已完成`；
- 工具：`正在检索相关文档`、`文档检索完成`；
- skill：`正在应用排版风格 skill`、`排版风格已应用`。

工具/skill 卡片应放在对应节点下方或“工具调用”区域，不要插入节点主时间线作为新的节点。若工具活动
非常频繁，可以默认折叠参数摘要，只显示 label 和状态；展开详情属于开发面板能力。

### 3.3 debug 与产品展示的边界

当服务端 debug 关闭时，`agent.activity` 仍可发送，用于让用户观察“正在做什么”，但不应包含内部提示词、
完整工具参数、完整工具结果或敏感内容。前端产品只消费 `label`、`status` 和安全摘要。

当服务端 debug 开启且请求被允许返回 debug 信息时，工具/skill 的原始参数和执行结果会出现在 debug
响应中，通常不通过 `agent.activity` 发送。开发面板可以在独立的“工具/skill 调用”栏目中折叠展示：

```ts
type DebugToolCall = {
  id: string;
  kind: "tool" | "skill";
  name: string;
  node?: string;
  arguments: unknown;
  result?: unknown;
  status: "running" | "completed" | "failed";
};
```

产品前端不要依赖 debug 字段判断工作流是否完成；完成状态始终以 `response.completed.response.metadata.workflow_status`
和标准 artifact/interrupt 事件为准。

## 四、一个完整消费流程

```text
POST /v1/responses
  -> response.created，创建 run 视图并保存 response_id
  -> response.output_text.delta，追加专业解释文本，呈现打字机效果
  -> agent.activity(kind=node, status=running)，创建节点活动行
  -> agent.activity(kind=node, status=completed)，原位更新同一行
  -> agent.interrupt(clarification_type=intent_confirmation)，创建意图确认卡
  -> response.completed(workflow_status=waiting_for_input)

用户选择 B
  -> POST /v1/responses(previous_response_id=原卡片 response_id, context.hitl.selection=B)
  -> 新 response.created，但 run_id 不变
  -> 后续 activity/artifact 按同一 run 继续追加或更新
```

前端实现时请牢记：

1. `agent.interrupt` 按 `interrupt.id` upsert；
2. `agent.activity` 按 `run_id + activity.id` upsert；
3. 文本 delta 追加，文本 done 用于校准，不能重复拼接；
4. 新 revision 到达时创建新的 run 视图，不要把新旧活动、卡片和 artifact 混在一起；
5. 未知事件和未知 `kind` 应忽略或中性展示，不能让整条 SSE 流失败；
6. 用户取消后，旧 run 的 pending 卡片不可再次提交；
7. 详细字段和主接口请求方式以 [api.md](api.md) 为准。

## 五、适配检查清单

- [ ] 能识别 `agent.interrupt.form.clarification_type=intent_confirmation`；
- [ ] 能渲染 A/B/C 主题单选和“其他主题”输入；
- [ ] 提交时复制正确的 `previous_response_id` 与 `interrupt_id`；
- [ ] `decision=revise` 在界面上显示为“确认选择”，不显示内部术语；
- [ ] 活动使用 Map 或按 ID 替换，running/completed 不重复；
- [ ] 活动按 `run_id` 隔离，新的 revision 不复用旧活动列表；
- [ ] 节点和工具/skill 使用不同视觉样式；
- [ ] debug 关闭时不要求或依赖工具原始参数；
- [ ] 对未知 `agent.activity.kind` 保持向前兼容；
- [ ] 断线重连、卡片重放和 cancel 后继续均按卡片生命周期处理。
