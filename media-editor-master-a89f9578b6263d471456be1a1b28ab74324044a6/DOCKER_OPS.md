# Docker 部署运维手册

面向 Docker 启动/运维人员，用于部署和维护 **videoaudiotext Flow B FastAPI**（文案成片 API）。

> 接口说明见 [`docs/api-flow-b-fastapi-service-api.md`](docs/api-flow-b-fastapi-service-api.md)；联调 Walkthrough 见 [`docs/api-flow-b-fastapi-walkthrough.md`](docs/api-flow-b-fastapi-walkthrough.md)；**文档索引** [`docs/README.md`](docs/README.md)。

---

## 一、部署前检查

| 项 | 要求 |
|---|---|
| Docker + Compose 插件 | 已安装且可用 |
| 内存 | 建议宿主机 ≥ 8GB（compose 默认限制容器 12GB） |
| 端口 | `.env` 中 `API_PORT` 未被占用（默认 `8787`） |
| ffmpeg | 已打入镜像，无需宿主机安装 |
| 密钥 | `.env` **不要提交 Git**，在服务器单独创建 |
| 字体 | `./fonts` bind mount（**CodeUp 已含约 17 个字体文件**，`git pull` 后确认挂载；见 [`fonts/README.md`](fonts/README.md) §5） |
| 出网 GET | 容器须能 **GET** 业务侧 OSS/CDN URL（`audio/process`、`media/bind` 拉取输入素材） |
| 出网 PUT | 容器须能访问 **阿里云 OSS**（`compose` 成功后上传成片 MP4 / 完整音频 / 任务日志） |

---

## 二、首次部署

```bash
# 1. 进入项目根目录
cd /path/to/videoaudiotext

# 2. 创建环境变量
cp .env.example .env
vim .env   # 填写下方「必填项」

# 3. 工作区与字体（容器内 UID=1000）
mkdir -p workspaces
sudo chown -R 1000:1000 workspaces

# fonts/ 已随 Git 跟踪，pull 后应约有 17 个 .ttf/.otf（~150MB）
git pull origin master
ls fonts/*.{ttf,otf} 2>/dev/null | wc -l
# 若数量明显偏少：git checkout origin/master -- fonts/

# 4. 构建并启动（代码更新也必须带 --build）
docker compose up -d --build

# 5. 查看状态
docker compose ps
```

### `.env` 必填项（按场景）

**compose 成片交付（推荐生产配置）** — 与 LLM 分句叠加使用：

```env
API_PORT=8787
WORKSPACES_ROOT=./workspaces

OSS_UPLOAD_ENABLED=1
ALIYUN_OSS_ENDPOINT=oss-cn-shenzhen.aliyuncs.com
ALIYUN_OSS_ACCESS_KEY_ID=xxx
ALIYUN_OSS_ACCESS_KEY_SECRET=xxx
ALIYUN_OSS_BUCKET=your-bucket
# 可选：ALIYUN_OSS_PREFIX=prod/ImagesVideosText2Video
```

成片 OSS 路径：`{prefix}/{code}-{id}/{YYYY-MM-DD}/{job_id}.mp4`（`code`/`id` 由 `compose/run` 请求体传入）。上传成功后 **workspace 目录会被删除**；Job 结果归档于 `workspaces/_compose_results/{job_id}.json`。

**LLM 语义分句**（与 OSS 叠加）：

```env
ARK_API_KEY=xxx
DOUBAO_MODEL=doubao-seed-2-0-lite-260428
TEXT_SPLIT_MODE=llm
```

**仅 rule 分句（不用 LLM）**：

```env
TEXT_SPLIT_MODE=rule
```

**维护接口（可选）**：

```env
MAINTENANCE_API_TOKEN=your-secret-token
```

- `POST /api/v1/maintenance/purge-workspaces` — TTL 清理过期 workspace（默认 24h grace，当天测试不会自动删）
- `GET /api/v1/maintenance/coord-status` — 查看 `GLOBAL_JOB_WORKERS` 排队与 `_coord/` 状态

`audio/process` 由调用方提供每句 `.wav` 的 HTTP URL，服务端不合成 TTS。

### 可选调参

```env
API_MEM_LIMIT=12g
API_LOG_LEVEL=INFO
MEDIA_UPLOAD_FETCH_TIMEOUT_SEC=0   # 0 = 单 URL 拉取不限制读超时（大素材建议保持 0）
SUBTITLE_X264_PRESET=veryfast
CLIP_X264_PRESET=veryfast
GLOBAL_JOB_WORKERS=4
UVICORN_WORKERS=1
CLIP_BUILD_WORKERS=4
WORKSPACE_TTL_EMPTY_DAYS=3
WORKSPACE_TTL_ABANDONED_DAYS=7
WORKSPACE_TTL_COMPOSED_DAYS=30
```

### 反向代理 / 客户端 HTTP 超时（Job 轮询）

分句、配音、素材绑定、预览、合成均为 **异步 Job**（`POST .../run` 立即返回 `job_id`，再 `GET .../jobs/{job_id}` 轮询）。网关/Swagger 对 **`/run` 提交** 的超时可较短（秒级）；**轮询** 间隔见 walkthrough §3.2。

`media/bind` 在 Job 执行期间会从 OSS 拉取全部视频，典型 **~5 min / 13 句**（取决于素材体积与带宽）。若客户端超时断开，任务仍可能在容器内继续——用 `GET .../status` 的 `media_bound` 与 `active_media_bind_job_id` 确认，**勿重复提交** `/run`。

| 阶段 | 建议轮询间隔 | 典型耗时（13 句参考） |
|------|-------------|----------------------|
| split | 2～3 s | ~10 s |
| audio/process | 5 s | ~15 s |
| media/bind | 5～10 s | **~5 min** |
| compose + OSS | 5～10 s | **~60 s** |

Nginx 若反代 API，建议对长连接适当放宽读超时（Job 轮询为短请求，通常无需小时级；旧版同步接口才需要 7200s）：

```nginx
proxy_connect_timeout 60s;
proxy_send_timeout 300s;
proxy_read_timeout 300s;
```

---

## 三、升级 / 重新部署（重要）

```bash
cd /path/to/videoaudiotext

git pull   # 或解压新包覆盖；须包含 fonts/ 下全部字体文件

# 必须 rebuild，仅 restart 不会更新代码
docker compose up -d --build
```

**禁止**只执行 `docker compose restart` 当作发版——镜像内代码不会更新。

---

## 四、部署后验证（必做）

```bash
# 1. 健康检查（ffmpeg / ffprobe 可用时返回 200）
curl -s http://127.0.0.1:8787/api/v1/health | head

# 2. 查看启动日志
docker compose logs app | tail -50

# 3. API 文档
# 浏览器打开 http://<服务器IP>:8787/docs
```

**正常表现：**

- 日志在 **stdout**，用 `docker compose logs -f app` 查看
- `/api/v1/health` 中 `checks.ffmpeg.ok` 与 `checks.ffprobe.ok` 均为 `true`
- `workspaces/` 在宿主机持久化；**进行中的** workspace 与 `_compose_results/` 归档在重启后不丢
- **compose 成功** 的 workspace 目录会被删除，成片在 OSS（`result.output_video_url`）

**字体挂载（必做）：**

```bash
# 宿主机：期望 ≥ 17 个字体文件
ls fonts/*.{ttf,otf} 2>/dev/null | wc -l
du -sh fonts

# 容器内应与宿主机一致（在项目根目录执行 compose）
docker compose exec app sh -c 'ls /app/fonts/*.{ttf,otf} 2>/dev/null | wc -l'
docker compose exec app ls /app/fonts/
```

| 检查项 | 正常 | 异常处理 |
|--------|------|----------|
| 宿主机 `fonts/` 文件数 | ≥ 17 | `git checkout origin/master -- fonts/` |
| 容器 `/app/fonts/` | 与宿主机相同 | 确认在项目根执行 `docker compose`；检查 `volumes: ./fonts:/app/fonts:ro` |
| `GET /subtitle/fonts` | 本地可能 17 项；Docker 可能 **0 项** | 见 [`fonts/README.md`](fonts/README.md)；**以文件数 + preview JPG 为准** |
| `visual/preview` JPG（如 `竹石体`） | 字幕可读、非方块 | 宿主机 fonts 缺对应 `.ttf` |

> 字体不打进镜像（`.dockerignore` 排除 `fonts`），仅 bind mount。只手动放思源黑体而忽略 Git 中其余文件，会导致「迷茫体正常、竹石体/星汉等宽方块」。

---

## 五、日常运维命令

```bash
# 查看运行状态
docker compose ps

# 实时日志
docker compose logs -f app

# 停止 / 启动
docker compose stop app
docker compose start app

# 重启（不更新代码，仅进程重启）
docker compose restart app
```

### 一键部署脚本（可选）

```bash
bash scripts/deploy.sh
```

等价于检查 `.env`（含 OSS 必填项）后执行 `docker compose up -d --build`，并轮询 `/api/v1/health` 直至通过（最长 120s）。

---

## 六、持久化与挂载

| 挂载 | 容器路径 | 说明 |
|---|---|---|
| `./workspaces` | `/app/workspaces` | 进行中的 workspace、Job 状态、`_compose_results/` Job 归档 |
| `./fonts` | `/app/fonts`（只读） | 硬字幕字体 |

- 容器重启 **不会**清空 `workspaces/` 中未删除的目录
- compose **成功且 OSS 上传完成** 后，对应 `ws_xxx/` **会被删除**；成片不在本地
- 启动时会执行 `reconcile` 与过期工作区 `purge`（主要清理未完成/失败的 workspace）
- 字体目录较大，**不打进镜像**（`.dockerignore`），通过宿主机 `./fonts` bind mount 提供；**文件来自 Git，非镜像内建**

---

## 七、注意事项

### 1. 合成任务与优雅停止

`compose/run` 为异步 Job。`docker compose stop` 或 `up --build` 时，`stop_grace_period: 120s` 会等待任务收尾；超时强杀后 Job 可能标记为中断，需客户端重新提交。

### 2. 内存与并发

视频合成（FFmpeg 裁切/拼接）可能占用较多内存。监控 `docker stats videoaudiotext-api`，接近 `mem_limit`（默认 **12g**）时提高 `API_MEM_LIMIT` 或降低并发。

单进程内并发由三个环境变量控制：

| 变量 | 默认 | 含义 |
|------|------|------|
| `UVICORN_WORKERS` | `1` | uvicorn 进程数；**>1 时**通过 `workspaces/_coord/` 文件锁做跨进程协调 |
| `GLOBAL_JOB_WORKERS` | `min(4, CPU核数)` | **全机**同时执行的 API Job 数（所有 worker 共享） |
| `CLIP_BUILD_WORKERS` | `min(4, CPU核数)`，上限 8 | **全机** CLIP/ffmpeg 并行线程上限 |

**多 worker 示例（8 核单机）：**

```env
UVICORN_WORKERS=4
GLOBAL_JOB_WORKERS=4
CLIP_BUILD_WORKERS=4
```

每个 worker 进程内 Job 池约为 `ceil(GLOBAL / UVICORN_WORKERS)`（上例为 1），全局槽位文件保证全机仍最多 4 个 Job 同时跑。

打满 `GLOBAL_JOB_WORKERS` 后，新 Job 返回 `status: queued` 并在 `_coord/global_jobs.json` 排队；轮询 Job 时可见 `queue_position` / `queue_ahead` / `queue_message`。

多机多实例（多台服务器）仍须 Redis 等分布式协调；**单机多 worker** 已支持。

### 3. URL 拉取安全

默认拒绝内网/回环地址（SSRF 防护）。音频与素材须使用 **OSS/CDN 公网 HTTPS URL** 或预签名 URL。

### 4. OSS 上传失败

若 `.env` 未配齐 `ALIYUN_OSS_*` 或上传失败，compose Job 会 **failed**，**workspace 保留**在 `./workspaces`，可修配置后重试 `compose/run`。

### 5. 不要用宿主机 Python 虚拟环境清 Docker 数据

Docker 内工作区在 `./workspaces` bind mount。compose 成功后 workspace 自动删除，Job 归档于 `_compose_results/`。清理未完成 workspace：`POST /api/v1/maintenance/purge-workspaces`（注意 24h grace）或 `DELETE /api/v1/workspaces/{id}`；勿误删 `_compose_results/`。

---

## 八、常见问题排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `unhealthy` | ffmpeg 未装好或进程未就绪 | `docker compose logs app`；`docker compose exec app ffmpeg -version` |
| `/health` 返回 503 | ffmpeg/ffprobe 不可用 | 重建镜像 `docker compose up -d --build` |
| 写 workspaces 权限错误 | UID 不匹配 | `sudo chown -R 1000:1000 workspaces` |
| 分句失败 | `ARK_API_KEY` 未配或 `TEXT_SPLIT_MODE=llm` | 改 `rule` 或填写密钥 |
| 拉取素材 URL 失败 | OSS 签名过期、403 或网络不通 | 检查预签名有效期；确认容器出网可 GET |
| compose 成功但无成片 URL | OSS 未配置或上传失败 | 检查 `.env` 中 `ALIYUN_OSS_*`；查 compose Job 的 `error` |
| compose 后 `/files/.../output.mp4` 404 | workspace 已删除 | 正常；使用 `result.output_video_url`（OSS） |
| 硬字幕变方块 / 部分字体失效 | 宿主机 `fonts/` 未拉全或只放了部分字体 | `git checkout origin/master -- fonts/`；核对竹石体/星汉等宽/文楷等 `.ttf` 存在 |
| 硬字幕字体不对（非方块） | `fonts/` 为空或未挂载 | 检查 compose 挂载；在项目根启动 |
| `subtitle/fonts` 返回空列表 | Docker 无 fontconfig（已知） | 不影响烧录；见 [`fonts/README.md`](fonts/README.md) |
| Job 轮询很久 | `media/bind` 拉 OSS 大视频 | 查 `/status`；勿重复提交 `/run` |
| Swagger 一直 loading | 在 Execute 里等大文件/长轮询 | 用 curl 轮询 Job；成片用 OSS URL 下载 |

### 排查命令

```bash
docker inspect videoaudiotext-api \
  --format='RestartCount={{.RestartCount}} Status={{.State.Status}} OOM={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}'

docker compose logs --tail 150 app | grep -E 'ERROR|Killed|shutdown|Out of memory|OSS'
```

---

## 九、发版检查清单

- [ ] 已 `git pull` / 更新代码包（**含 `fonts/` 全部字体**）
- [ ] 宿主机 `ls fonts/*.{ttf,otf} | wc -l` ≥ 17；容器内文件数一致
- [ ] 已执行 `docker compose up -d --build`（不是仅 restart）
- [ ] `curl .../api/v1/health` 返回 200
- [ ] `docker compose logs -f app` 能看日志
- [ ] `.env` 中端口、分句、**OSS**、维护 Token 配置正确
- [ ] `workspaces/` 权限为 UID 1000 可写；`fonts/` 可读
- [ ] （可选）`visual/preview` 用 `竹石体` 抽检 JPG 无方块

---

## 十、相关文件

| 文件 | 说明 |
|---|---|
| `docker-compose.yml` | 服务编排、volume、环境变量 |
| `Dockerfile` | 镜像构建（Python + ffmpeg + FastAPI + oss2） |
| `.dockerignore` | 构建上下文排除项 |
| `.env.example` | 环境变量模板 |
| `scripts/deploy.sh` | 一键部署脚本 |
