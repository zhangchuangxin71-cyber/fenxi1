# 微信公众号确定性排版器烟测报告

> 烟测日期：2026-08-21  
> 代码范围：`app/rendering/wechat_layout/` 与 `render_html` 节点接入  
> 数据来源：开发环境 `article_artifacts` 数据库中的一条已完成测试 revision，只读抽取，未保存业务 ID、checkpoint 或完整会话。

## 输入

固定输入已保存到 `tests/rendering_data/input/`：

- 真实 LLM 生成的未排版 Markdown，8049 bytes，包含 H1、H2、H3、普通段落和中文混排。
- 真实 Seedream 图片 artifact 4 张，保留原始 URL、图注和 `heading_path + paragraph_ordinal` 插入位置。
- 对应任务书 JSON，用于验证确定性主题选择输入。
- `manifest.json` 保存三个输入文件的 SHA-256，后续执行器会先校验哈希。

图片 URL 是业务层的短期签名 URL，排版器没有下载、上传或改写图片。URL 过期后，刷新 fixture 时必须整体重新
采集，不能在测试中偷偷替换成占位图片。

## 结果

同一份输入显式渲染 registry 中全部 8 套主题，结果写入 `tests/rendering_data/output/`：

| 指标 | 结果 |
|---|---:|
| 主题数量 | 8 / 8 |
| 安全、结构、正文和图片硬校验 | 8 / 8 通过 |
| 意外 fallback | 0 |
| 可见正文字符 | 2743 / 2743 |
| 图片数量及顺序 | 4 / 4 |
| 重复渲染字节一致 | 8 / 8 |
| 兼容性 warning | 0 |
| 单主题排版耗时 | 约 8--39 ms |

每个主题目录包含 `final.html`、`validation.json` 和 `metadata.json`；根目录包含 `summary.json` 和
`index.html` 横向预览页。执行命令：

```bash
uv run python tests/rendering_data/render_all.py
```

## 浏览器检查

使用 Playwright 在 390px 宽度加载全部 8 个 `final.html`：

- 每个主题 `scrollWidth == clientWidth == 390`，没有横向溢出。
- 每个主题均存在 H1、5 个 H2 和 3 个 H3 章节标题，4 张图片均完成加载。
- 逐个检查首屏、主题组件、图注和长段落，没有发现文字重叠或元素遮挡。
- 视觉结果截图保存在临时目录 `/tmp/wechat-layout-screens-fixed/`，不作为产品产物提交。

## 期间修复

首次视觉检查发现 sanitizer 的标签 allowlist 漏掉了 `h1`--`h4`，导致 token 型主题标题被清洗成普通文本。
已补齐标题标签并增加主题级回归断言，重新生成全部输出后通过上述结果。

## 后续边界

本烟测只证明确定性 renderer 的结构、内容、图片和移动端基础表现，不等同于所有微信公众号客户端和粘贴路径
的完全兼容保证。真实公众号后台粘贴、暗色模式和不同客户端的观察仍按 `docs/optimization.md` 的 P5 阶段执行。
