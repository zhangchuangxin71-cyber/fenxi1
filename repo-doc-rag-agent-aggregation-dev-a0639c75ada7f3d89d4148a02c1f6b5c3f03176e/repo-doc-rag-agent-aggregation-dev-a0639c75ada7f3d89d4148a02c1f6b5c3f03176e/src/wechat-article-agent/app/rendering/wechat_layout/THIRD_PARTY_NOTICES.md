# 第三方排版资产说明

本目录实现参考并改写了以下固定版本的开源或获授权资产。运行时不会从这些仓库下载代码。

| 来源 | 固定提交 | 本地采用方式 |
|---|---|---|
| WeWrite | `b998de801bf13cb251caa2a4c529f99cdfdd6537` | 参考 Python DOM 转换、主题 token 和校验思路，重写为本项目 strict schema 与 renderer。 |
| aiworkskills/wechat-article-skills | `c83831a14a1f708a1a3136ebe75378d2b782f339` | 参考小型 YAML 主题契约和中文排版规则。 |
| gzh-design-skill | `ba1f4175519b481cb3566616c9e5178705067904` | 经作者明确授权，改写摸鱼绿、石墨极简、橄榄手记、红白主题的标题组件和视觉 token。 |
| xiaowan-wechat-layout-lite | `5a96543a5f47c0f82f74e4ce5041fbe6f6b57c45` | 参考正文冻结、手机端密度和验收规则；未复制 Agent 工作流。 |
| Baoyu skills | `6b7a2e417500561a5ecdd0b168332f4142584617` | 仅交叉验证 Markdown 到公众号 HTML 行为，未引入其 Bun/CDP 运行时。 |

详细的文件级调研与许可边界见 `docs/open-source-skill-migration.md`；当前 Agent+Skill 排版实现见
`docs/optimization_rendering.md` 和 `docs/agent_engine.md`。
