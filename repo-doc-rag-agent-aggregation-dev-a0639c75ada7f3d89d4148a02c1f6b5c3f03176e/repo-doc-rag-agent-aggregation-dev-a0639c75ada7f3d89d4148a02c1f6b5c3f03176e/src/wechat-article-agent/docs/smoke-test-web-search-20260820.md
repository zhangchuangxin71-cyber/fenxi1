# 联网素材调研真实烟测报告

## 测试范围

- 测试日期：2026-08-20
- 请求：`生成一篇关于牛来的文章`
- 网页搜索：已开启（仅记录开关状态，不记录 API key）
- 服务：本地 adapter `8240`、LangGraph runtime `8242`、开发面板 `8245`
- 会话：`web-search-smoke-20260820-01`
- run：`run_99534acbb7f04b02984001b698ff671d`
- artifact：`art_c04b01c5ceb24e1fa82395137583c01a`

## 执行结果

本次请求按以下顺序完成了真实 HITL 审批：

1. 素材搜集方向：选择“《牛来》现象的社会文化综合评论”。
2. 素材冲突：选择“按来源权威性自动判断”。
3. 任务书：接受。
4. 大纲：接受。
5. 未排版文章：接受。
6. 配图和 HTML 排版：完成。

工作流最终状态为 `completed`，没有残留 pending interrupt。完整 SSE 保存在本机临时文件 `/tmp/wechat-web-smoke-06.sse`，其中包含本次响应的原始事件和 debug trace。

## 已验证的联网素材行为

- `web_info_overview` 成功调用 Ark 内置 `web_search`，并在搜索结果存在多个相关主题时给出背景概览。
- 网络素材 worker 返回了事实数据、创作参考和流行文化等素材；来源 URL 经过 annotations 校验后才进入素材库。
- 冲突检查发现了《牛来》早期票房与总票房预测的多源口径差异，并先发送 `material_conflicts` artifact，再发送冲突 HITL 卡片。
- 素材调研完成后发送了面向用户的 `material_sources` artifact，内容为 4 个来源 URL；debug 模式同时保留了完整 `material_library`。
- 冲突处理后的素材被任务书、大纲和正文节点消费，正文没有将互相矛盾的数字无提示地混写。

## 最终产物

- 生成配图：5 张 Seedream 图片 URL，均包含明确的文章段落插入位置和图片标注。
- 最终 HTML：包含文章正文和图片 URL；数据库中 `final_html` 字符数为 6949，`images` 数量为 5。历史 HTML 副本已随旧排版烟测产物统一清理，本报告保留当时的验收指标。
- 产物没有把图片文件下载到 agent 数据库，符合前端调用者负责下载 URL 到 OSS 的约定。
- Chromium 实际加载最终 HTML 后检测到 12 个正文段落、5 个图片节点，5 张图片均成功加载，页面脚本错误为 0。

## 自动化回归

- Pytest：`121 passed, 1 skipped`。
- Ruff lint：通过。
- Ruff format check：通过。
- mypy：`89` 个源文件无类型错误。
- 开发面板 Chromium 检查：素材来源卡、冲突审批三选项和 URL 外链均正常；SSE 增量重绘后，用户已折叠的素材来源卡保持折叠；浏览器控制台错误为 0。

## 失败与降级观察

本次完整链路没有发生服务级失败、结构化输出修复耗尽或 stale interrupt。真实模型调用产生了 reasoning 增量，adapter 正常转发为 `response.reasoning_summary_text.delta`，开发面板可以按打字机方式展示。

## 验收结论

联网搜索、素材来源 artifact、素材冲突 HITL、任务书/大纲/文章审批、配图和最终 HTML 已完成一次端到端真实烟测。数据库查询确认该 revision 为 `completed`，素材库、图片和 HTML 均在同一 `article_artifacts` 快照中，三天 TTL 仍有效。
