# 文档索引

Flow B FastAPI 相关说明的**入口页**。按角色选读即可。

---

## 我该读哪一篇？

| 你想做什么 | 读这篇 |
|------------|--------|
| **Docker 一键部署 / 运维** | [DOCKER_OPS.md](../DOCKER_OPS.md) → `bash scripts/deploy.sh` |
| **Swagger 逐步联调（主文档）** | [api-flow-b-fastapi-walkthrough.md](./api-flow-b-fastapi-walkthrough.md) |
| **JPG 快速联调（~2 min · 隔离 8797）** | [api-flow-b-fastapi-jpg-test-walkthrough.md](./api-flow-b-fastapi-jpg-test-walkthrough.md) |
| **16 句可复制 JSON** | [api-flow-b-web-test-examples.json](./api-flow-b-web-test-examples.json) |
| **查接口字段、错误码、Schema** | [api-flow-b-fastapi-service-api.md](./api-flow-b-fastapi-service-api.md) |
| **完整接口文档（Markdown · 含实测）** | [api-flow-b-fastapi-api-doc.md](./api-flow-b-fastapi-api-doc.md) |
| **硬字幕字体怎么放** | [fonts/README.md](../fonts/README.md) |
| **产品 / 架构设计契约** | [api-flow-b-production.md](./api-flow-b-production.md) |
| **本地 Python 启动（非 Docker）** | [README.md](../README.md) |

**交互式 API 文档（运行时）：** `http://<host>:8787/docs`

---

## 文档地图

```
README.md                          项目入口
DOCKER_OPS.md                      部署 / 升级 / 排查
fonts/README.md                    字体
docs/
├── README.md                      ← 本页
├── api-flow-b-fastapi-walkthrough.md   ★ 联调主文档（步骤 0～9 + FAQ）
├── api-flow-b-fastapi-jpg-test-walkthrough.md  ★ JPG 快速联调（v3.1 · 8797 隔离 · ~2 min）
├── api-flow-b-web-test-examples.json   16 句 JSON 模板（复制到 Swagger）
├── api-flow-b-fastapi-api-doc.md         ★ 完整接口文档（Markdown · v2.30 · 2026-06-25 实测）
├── api-flow-b-fastapi-service-api.md   接口规范（v2.30）
├── api-flow-b-production.md            设计契约（v2.30）
└── api-flow-b-types.ts                 前端 TypeScript 类型定义
```

---

## 推荐阅读顺序

### 运维 / 后端部署

1. [README.md](../README.md) — Docker 一键启动  
2. [DOCKER_OPS.md](../DOCKER_OPS.md)  
3. [fonts/README.md](../fonts/README.md)  
4. `curl .../api/v1/health`

### 前端 / 业务联调

1. [api-flow-b-fastapi-walkthrough.md](./api-flow-b-fastapi-walkthrough.md) — 按步骤走通  
2. [api-flow-b-fastapi-service-api.md](./api-flow-b-fastapi-service-api.md) — 查字段与错误码  
3. Swagger `/docs`

### 产品 / 架构评审

1. [api-flow-b-production.md](./api-flow-b-production.md)  
2. [api-flow-b-fastapi-service-api.md](./api-flow-b-fastapi-service-api.md)

---

## 维护说明

| 变更类型 | 应更新 |
|----------|--------|
| 新增/改接口 | `service-api.md` + `api-doc.md` + Swagger（代码内 openapi） |
| 联调流程或 OSS 踩坑 | `walkthrough.md` 或 `jpg-test-walkthrough.md` |
| Docker / 部署 / 并发调参 | `DOCKER_OPS.md`、`.env.example` |
| 16 句 JSON 样例 | `api-flow-b-web-test-examples.json` |
| 设计决策 | `production.md` |

若 walkthrough 与 service-api 冲突，**以代码与 service-api 为准**。
