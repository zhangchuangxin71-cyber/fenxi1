# Video Clip API

纯 FastAPI 服务：OSS 远程预览、手动/自动分镜、流式切片与 OSS 对象列表返回。
本仓 `frontend/` 为对接参考界面（**不**进入 Docker 镜像）。

## 运行模型（重要）

- **单 API 进程**：任务状态在**内存**；后台导入/切割在同进程线程池执行
- **不跨重启恢复**：进程重启后旧 `video_id` / `job_id` 全部失效，请及时保存返回的 OSS 对象列表
- **单实例**：不要水平扩展多个 API 副本
- 源片与切片成品不完整落盘：FFmpeg 远程读 OSS，stdout multipart 直接写源目录下的 `clips/`
- 磁盘 `data/` 只存波形/胶片条等小型缓存，不当业务账本

## 前置条件

- 已安装 **Docker**（含 `docker compose`）
- 填写 `.env` 中的 **`OSS_*`**（导入源片 / 预存切片必填）
- `ARK_*` 仅在使用 `detector=semantic` 时需要
- 镜像内已含 FFmpeg；API 无鉴权，勿对公网裸暴露，应由网关/VPN/防火墙限制入口

## 快速启动

```bash
cp .env.example .env          # 填写 OSS_ACCESS_KEY_* / OSS_BUCKET 等
./start.sh                    # 有镜像则直接起；没有才 build
./start.sh health             # GET /api/v1/health（含 git_sha / build_time）
```

### 代码更新（同事部署必看）

镜像里的代码在 **build 时 COPY 进去**；`./data` 只挂数据。因此：

| 操作 | 会不会更新代码 |
|------|----------------|
| `git pull` + `./start.sh` / `up` / `restart` | **不会**（启动很快 = 复用旧镜像） |
| `git pull` + `./start.sh redeploy` | **会**（build + 强制重建容器） |

推荐流程：

```bash
git pull
./start.sh redeploy
./start.sh verify             # 对比本地 git 与 health.git_sha，并检查关键文件
```

`/api/v1/health` 的 `data.git_sha` 必须与部署机 `git rev-parse --short HEAD` 一致，才说明新镜像已生效。

| 服务 | 说明 |
|---|---|
| `api` | FastAPI + 同进程后台任务，默认端口 `API_PORT=8010`，容器名 `video-clip-api` |

说明：

- 本地数据：`./data` → 容器 `/app/data`（仅小型派生缓存；URL 转存源片与 `clips/` 切片按 OSS 保留策略管理）
- 业务配置经 **`env_file: .env`** 注入；`init: true` 回收 ffmpeg 僵尸进程；停容器宽限 `stop_grace_period: 90s`
- `/api/v1/health` 检查 **ffmpeg / ffprobe / OSS / 内存账本 / 调度器**，并回显 **`git_sha` / `build_time`**（镜像内亦有 `HEALTHCHECK`）
- 跨域时设 `CORS_ORIGINS`；反代时设 `API_PUBLIC_BASE`；配置 `OSS_CDN_BASE` 后优先走 CDN，CDN 不可读时自动回退 OSS 签名 URL

常用命令：

```bash
./start.sh redeploy           # 拉代码后更新：build + 强制用新镜像重建
./start.sh verify             # 核对 health.git_sha 与本地 git，防旧镜像
./start.sh build              # 仅构建镜像（注入当前 short SHA）
./start.sh logs               # api 日志
./start.sh down               # 停止
```

## 推荐部署流程（避免现场 pip）

现场 `docker compose up --build` 会跑很久的 `pip install`（含 OpenCV），易失败。**请先在有网络的机器预构建**，再离线交付：

```bash
# --- 构建机 ---
cp .env.example .env          # 构建可不填 OSS；部署机再填
./start.sh build              # 打出 video-clip-api:latest（Dockerfile 已用国内 apt/pip 源）
docker save video-clip-api:latest -o video-clip-api.tar

# --- 部署机 ---
docker load -i video-clip-api.tar
cp .env.example .env          # 填写 OSS_* 等
./start.sh up                 # 默认不 --build，直接起容器
./start.sh health
```

若已有容器镜像仓库，设置 `IMAGE=你的仓库/video-clip-api:tag` 后 `docker pull`，再 `./start.sh up` 即可。必须现场构建时，镜像内已切阿里云 apt/pip 源，但仍不如预构建稳定。
## 前端

仓库当前只保留完整交互前端：`frontend/reference-ui/`。

```bash
cd frontend/reference-ui
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8010`。前后端同机时可直接使用；Windows 前端与 Linux 后端分离时，推荐通过 SSH 隧道把 Windows 的 `8010` 转发到 Linux 后端，或将 `vite.config.ts` 中的 proxy target 改为 Linux API 地址。

## API

| | |
|---|---|
| 契约 | **`/api/v1/*`**，响应 `{code, message, data}`（二进制媒体除外） |
| 主键 | UUID：`video_id` / `job_id` / `segment_id` |
| OpenAPI | [`docs/openapi/v1.yaml`](docs/openapi/v1.yaml) |
| 接口文档 | [`docs/视频切片服务接口文档.md`](docs/视频切片服务接口文档.md) |
| 映射表 | [`docs/api-mapping.md`](docs/api-mapping.md) |
| Swagger | 服务启动后访问 `/docs` |

## 环境变量

完整示例见 [`.env.example`](.env.example)。要点：

| 变量 | 说明 |
|---|---|
| `OSS_ENDPOINT` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_BUCKET` | **必填**（生产） |
| `OSS_CDN_BASE` | 推荐；源片与 `clips/` 预存切片优先走 CDN，异常时回退 OSS 签名 URL |
| `OSS_DOWNLOAD_RETRIES` / `OSS_DOWNLOAD_RETRY_BASE_SLEEP` | URL 转存与 OSS 操作重试（默认 3 次 / 1.5s 起指数退避） |
| `OSS_INGEST_PREFIX` / `OSS_DERIVED_PREFIX` | URL 源片和派生资源的受管 OSS 前缀；当前切片写入源片同级 `clips/` |
| `OSS_PROCESS_SIGN_EXPIRES` / `OSS_MULTIPART_PART_SIZE_MB` | 处理签名有效期与 multipart 分片大小 |
| `IMPORT_ALLOWED_HOSTS` | 可选的公网源片域名白名单；为空时允许通过 SSRF 校验的公网 URL |
| `OSS_MANAGED_SOURCE_RETENTION_HOURS` | 重启后孤儿 URL 源片和派生对象的保留时间 |
| `CORS_ORIGINS` | 前端 Origin 白名单 |
| `API_PUBLIC_BASE` | 可选；本机媒体绝对 URL 前缀（反代后填写；勿写成 `==`） |
| `ARK_API_KEY` 等 | 可选；仅 semantic 分镜 |
| `INLINE_MAX_JOBS` | 同进程后台任务并发上限，默认 4 |
| `CUT_MAX_WORKERS` | 单任务内并行 ffmpeg 路数 |
| `MAX_INMEM_VIDEOS` / `MAX_INMEM_TASKS` | 内存账本上限 |
| `API_PORT` | 宿主机映射端口，默认 **8010**（容器内亦监听 8010） |
| `IMAGE` | Compose 镜像名，默认 `video-clip-api:latest`（可改为仓库地址） |
| `STALE_TASK_HOURS` / `LOCAL_RETENTION_HOURS` / `CLEANUP_INTERVAL_MINUTES` / `DISK_FREE_GB_MIN` | 本地临时文件、任务产物及波形/胶片条派生缓存的清理策略；派生缓存按 `LOCAL_RETENTION_HOURS` 的文件年龄清理，运行中的任务会跳过 |

**.env 勿提交仓库。**
