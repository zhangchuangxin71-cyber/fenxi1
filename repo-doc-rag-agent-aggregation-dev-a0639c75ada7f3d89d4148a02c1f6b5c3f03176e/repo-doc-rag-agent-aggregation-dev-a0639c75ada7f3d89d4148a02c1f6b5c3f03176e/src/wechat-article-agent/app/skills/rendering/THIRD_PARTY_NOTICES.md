# 排版 Skill 固定资产来源

本目录保存经过项目适配的固定资产，不在运行时下载或执行上游仓库代码。

- `gzh_design`：`isjiamu/gzh-design-skill@ba1f4175519b481cb3566616c9e5178705067904`。
- `xiaowan_layout`：`cyberxiaowan/xiaowan-wechat-layout-skill@5a96543a5f47c0f82f74e4ce5041fbe6f6b57c45`。

全部候选 Skill 的项目使用许可已由项目负责人确认。`gzh_design/references/` 已固定迁入主题索引、
六套完整主题组件库、通用组件库和上游 validator/lint 规则快照；`xiaowan_layout/references/` 已固定迁入
工作流、布局标准、发布清单和反馈分类。上游脚本仅作为审计文本保存，生产运行时不会执行。

项目适配删除了文件工作区、发布、浏览器、外部下载、任意脚本执行、正文改写和运行时主题生成能力；
模型只能通过 `app/rendering/skill_driven` 中的 ToolSpec 与内存组件装配器使用这些资产。
