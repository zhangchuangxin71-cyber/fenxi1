# 参考 UI → API v1 映射

参考交互阶段来自前端仓 `reference-ui/`：`landing → editing → execute`。

## 推荐主路径

1. `POST /api/v1/videos/import` — 立刻返回 `{video_id, status=importing}`
2. `GET /api/v1/videos/{video_id}` — 轮询至 `status=ready`（含 waveform）
   - `preview_url`：URL 输入会先流式转存到 `OSS_INGEST_PREFIX/{video_id}/source.ext`；媒体入口优先 CDN，CDN 不可用时 302 到 OSS 签名 URL
   - `filmstrip_url` 仍为本服务胶片条地址
3. 手动：`POST /api/v1/jobs/manual` — **建档并切割**（必填 `video_id` + `segments`）；重切再调一次建新 Job
4. 自动：`POST /api/v1/jobs/auto` — **建档并检测** → 轮询至 `preview` → `POST /jobs/{job_id}/cut`
5. `GET /api/v1/jobs/{job_id}` — 轮询至 `done`（完整 Job + segments）
6. `POST /api/v1/jobs/{job_id}/publish` — 返回全部及选中切片的 OSS 对象列表，不执行复制

回溯：`GET /api/v1/videos/{video_id}/jobs`（**仅当前 API 进程存活期内有效**；重启后旧 ID 全部失效）。

- 无只建档、无 `auto_cut`、无 `confirm`、无 `/detect`（失败再调 `/jobs/auto`）
- 建 Job **不支持**内嵌 `oss_key`/`url`（须先 import 至 ready）
- JSON 统一 `{code:0, message:"ok", data}`；二进制不包 envelope
- 主键均为标准 UUID
- 后端为单进程内存账本：切完请及时保存返回的 OSS 对象列表

### 命名约定

| 资源 | 路径参数 | 响应主键 |
|---|---|---|
| Video | `{video_id}` | `video_id` |
| Job | `{job_id}` | `job_id` |
| Segment | `{segment_id}` | `segment_id` |

Job **不嵌套** `video`；用 Job.`video_id` 调 `GET /videos/{video_id}`。

## UI 动作 → API

| UI 动作 | v1 |
|---|---|
| 导入 | `POST /api/v1/videos/import` → 轮询 `GET /videos/{video_id}` |
| 视频信息 | `GET /api/v1/videos/{video_id}`（`status=ready`） |
| 该视频任务列表 | `GET /api/v1/videos/{video_id}/jobs` |
| 手动切 / 重切 | `POST /api/v1/jobs/manual`（每次新建 Job） |
| 自动检 / 重检 | `POST /api/v1/jobs/auto`（恒停 preview；失败再调一次） |
| 轮询任务 | `GET /api/v1/jobs/{job_id}` |
| 确认切割（仅自动） | `POST /api/v1/jobs/{job_id}/cut` |
| 对象列表 | `POST .../publish`（可传 `segment_ids`；结果含 `all` / `selected` 的 `oss_key`、`oss_url`） |
