# 角色与任务

本 Skill 是迁入项目的 GZH 公众号富组件排版资产。它向 `render_html` Agent 提供六套主题、组件语义、
文章配方和公众号 HTML 兼容规则。最终 HTML 由项目内存装配器生成，不由模型自由书写。

# 工作流

1. 读取 `references/theme-index.md`，结合任务书选择一套主题。
2. 读取所选 `references/theme-{theme_id}.md` 和 `references/common-components.md`。
3. 使用 `get_skill_component_catalog` 获取经过项目安全适配的组件 ID。
4. 对全部章节建立组件计划，只引用 `analyze_markdown_layout` 返回的节点 ID。
5. 计划校验后调用装配器，再执行 GZH 与 Xiaowan 检查。

# 固定主题

- `moyu-green`：教程、清单、工具盘点。
- `red-white`：政策解读、深度分析、观点。
- `graphite-minimal`：科技评论、专业观点、高端品牌。
- `zen-whitespace`：随笔、人物、生活方式和艺术。
- `moyu-ticket`：测评、热点、创意盘点。
- `olive-journal`：案例复盘、系统说明和内刊。

# 强制边界

- 已批准 Markdown 和图片 artifact 只读；不得补写、删减、重排或改写事实。
- 不创建任意 HTML/CSS，不执行上游脚本，不读写临时文件，不联网或发布。
- 不添加原文不存在的作者、CTA、关注提示或业务文案。
- 组件参数只能是项目 schema 中的主题、组件、章节和节点 ID。
- 禁止调用旧八主题 registry、`professional-clean` 或旧 `LayoutEngine`。
