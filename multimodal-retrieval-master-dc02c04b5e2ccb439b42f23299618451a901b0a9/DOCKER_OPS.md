# Docker 部署运维手册

面向 Docker 启动/运维人员，用于部署和维护 **multimodal-embedding-service** 向量化 API。

> 完整参数见 [`embedding_service/PARAMS.md`](embedding_service/PARAMS.md)；通用部署说明见 [`DEPLOY.md`](DEPLOY.md)。

---

## 一、部署前检查

| 项 | 要求 |
|---|---|
| Docker + Compose 插件 | 已安装且可用 |
| 内存 | 建议宿主机 ≥ 8GB（compose 默认限制容器 12GB） |
| 端口 | `.env` 中 `EMBEDDING_SERVICE_PORT` 未被占用（如 `8030`） |
| 代码版本 | 需包含 runtime 自动清扫相关更新（commit `3ccc77d` 及之后） |
| 密钥 | `.env` **不要提交 Git**，在服务器单独创建 |

---

## 二、首次部署

```bash
# 1. 进入项目根目录
cd /path/to/multimodal-retrieval

# 2. 创建环境变量（必填项不能留空）
cp .env.example .env
vim .env   # 至少填写下面「必填项」

# 3. 模型目录权限（容器内 UID=1000，否则自动下载可能失败）
mkdir -p models
sudo chown -R 1000:1000 models

# 4. 构建并启动（代码更新也必须带 --build）
docker compose up -d --build

# 5. 查看状态
docker compose ps
```

### `.env` 必填项

```env
EMBEDDING_SERVICE_PORT=8030

ALIYUN_OSS_ENDPOINT=https://oss-cn-xxx.aliyuncs.com
ALIYUN_OSS_ACCESS_KEY_ID=xxx
ALIYUN_OSS_ACCESS_KEY_SECRET=xxx
ALIYUN_OSS_BUCKET=xxx
```

视频/图片异步任务依赖 OSS；仅测文本向量可不配 OSS，但视频任务会失败。

### 可选调参（大批量视频时建议关注）

```env
EMBEDDING_WORKERS=4              # 异步任务并发线程数，按 CPU/内存调整
MEDIA_DOWNLOAD_TIMEOUT=300       # OSS 下载超时（秒）；大视频/批量建议 300–600
TMP_MAX_AGE_HOURS=6              # tmp 残留最长保留时间（小时）
RUNTIME_CLEANUP_INTERVAL_HOURS=1 # 定时清扫间隔（小时）
LOG_MAX_AGE_DAYS=7               # 旧辅助日志保留天数
```

`docker-compose.yml` 已暴露上述变量；也可在 `.env` 中覆盖。

---

## 三、升级 / 重新部署（重要）

```bash
cd /path/to/multimodal-retrieval

git pull   # 或解压新包覆盖

# 必须 rebuild，仅 restart 不会更新代码
docker compose up -d --build
```

**禁止**只执行 `docker compose restart` 当作发版——镜像内代码不会更新。

### 从旧版本升级后，建议一次性清理历史垃圾（可选）

```bash
docker compose stop app

docker compose run --rm app sh -c '
  rm -f /app/runtime/logs/embedding_service.log
  rm -f /app/runtime/logs/ffmpeg_*.log
  find /app/runtime/tmp -mindepth 1 ! -name .gitkeep -delete 2>/dev/null || true
'

docker compose up -d
```

---

## 四、部署后验证（必做）

```bash
# 1. 健康检查（首次启动可能需数分钟加载模型）
curl http://127.0.0.1:8030/health

# 2. 确认 runtime 清扫已生效（日志里应有类似输出）
docker compose logs app | grep -E "Startup runtime cleanup|Starting Media Embedding Service"

# 3. 查看 runtime 占用（空闲时应很小）
docker compose exec app du -sh /app/runtime/tmp /app/runtime/logs

# 4. 确认不再写 embedding_service.log（logs 目录应几乎为空）
docker compose exec app ls -la /app/runtime/logs/

# 5. API 文档
# 浏览器打开 http://<服务器IP>:8030/docs
```

**正常表现：**

- 日志在 **stdout**，用 `docker compose logs` 查看，**不要**去 volume 里找 `embedding_service.log`
- 启动时出现 `Startup runtime cleanup: tmp_removed=...` 表示清扫逻辑在跑
- `runtime/tmp` 空闲时约 4KB（仅 `.gitkeep`）

---

## 五、日常运维命令

```bash
# 查看运行状态
docker compose ps

# 实时日志（唯一推荐日志查看方式）
docker compose logs -f app

# 最近 200 行日志
docker compose logs --tail 200 app

# 停止 / 启动
docker compose stop app
docker compose start app

# 重启（不更新代码，仅进程重启；启动时会扫 tmp 孤儿）
docker compose restart app

# 查看任务统计
curl http://127.0.0.1:8030/stats
```

### 一键部署脚本（可选）

```bash
bash scripts/deploy.sh
```

等价于检查 `.env` 后执行 `docker compose up -d --build`。

---

## 六、注意事项（必读）

### 1. 持久化 volume

| 挂载 | 容器路径 | 说明 |
|---|---|---|
| `./models` | `/app/models` | 模型权重（bind mount） |
| `embedding_runtime` | `/app/runtime` | `tasks.db`、视频临时文件 |

- 容器重启 **不会**清空 volume（**有意保留任务记录**）
- tmp 孤儿会在 **每次容器启动时自动清扫**
- 运行中靠定时清扫（默认每 1 小时，受 `RUNTIME_CLEANUP_INTERVAL_HOURS` 控制）

### 2. 日志

- **当前版本只输出到 stdout**，用 `docker compose logs -f app`
- volume 里若还有旧的 `embedding_service.log`，是升级前残留，可按第三节命令手工删一次

### 3. 不要用宿主机 `clean_runtime.sh` 清 Docker 数据

```bash
# ❌ 这个脚本清的是宿主机 ./runtime，对 Docker volume 无效
bash scripts/clean_runtime.sh
```

清 Docker 内 runtime 请用第三节的一次性清理命令，或依赖服务自动清扫。

### 4. 视频批量任务

- 默认 **4 个 worker**（`EMBEDDING_WORKERS`），100 个视频会排队，队尾可能要 1~2 小时
- 客户端轮询超时 ≠ 服务端失败，需调大 `poll_timeout` 或控制提交速率
- 并发时 `runtime/tmp` 暂时涨到几百 MB 属正常，任务结束后应回落

### 5. 失败与 tmp 清理

- 正常业务失败（ffmpeg 失败、下载失败等）**会自动清理** tmp
- **OOM / kill -9 / 断电** 可能留残留，靠 **下次启动清扫** 处理

### 6. 彻底重置 runtime（会丢任务历史）

```bash
docker compose down
docker volume ls | grep embedding_runtime   # 确认卷名
docker volume rm <卷名>
docker compose up -d --build
```

---

## 七、Docker 部署下的「重启」与稳定性

### 7.1 什么会触发容器重启？

`docker-compose.yml` 使用 `restart: unless-stopped`：**主进程一旦退出**，Docker 会自动再拉起（不是代码里定时重启）。

| 原因 | 典型迹象 | 处理 |
|------|----------|------|
| **OOM 内存超限** | `RestartCount` 增加，`OOMKilled=true`，退出码 **137** | 提高 `EMBEDDING_MEM_LIMIT`；`EMBEDDING_WORKERS=1`；或恢复 `MEDIA_MAX_VIDEO_MB` 上限 |
| **启动失败循环** | 日志反复出现 `Starting Media Embedding Service` 后立刻结束 | 查模型是否就绪、`models/` 权限、`EMBEDDING_SERVICE_PORT` |
| **端口被占用** | `address already in use`、Exit **1** | 宿主机勿重复起第二个实例占用同端口 |
| **人工/发版** | `docker compose up --build`、`restart`、`stop` | 属预期；进行中任务会变 `INTERRUPTED` |

标准 Docker Compose **不会因为 `unhealthy` 就重启**（仅标记不健康）。若接 K8s 且配置了 liveness 探针，health 超时会杀 Pod，表现为频繁重启。

### 7.2 与「任务失败」区分

服务重启后，SQLite 里进行中的任务会被标为：

- `error_type=INTERRUPTED`
- `error=服务重启导致任务中断，请重新提交`

这是**任务记录**，不等于容器仍在反复重启。

### 7.3 排查命令

```bash
# 重启次数与是否 OOM
docker inspect multimodal-embedding-service \
  --format='RestartCount={{.RestartCount}} Status={{.State.Status}} OOM={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}'

# 退出前日志
docker compose logs --tail 150 app | grep -E 'ERROR|Killed|shutdown|address already in use|Out of memory'

# 宿主机 OOM（若有权限）
dmesg -T 2>/dev/null | grep -iE 'oom|killed process' | tail -10
```

### 7.4 compose 已做的稳定性配置

| 配置 | 作用 |
|------|------|
| `mem_limit` 默认 16g | 容器内存上限 |
| `EMBEDDING_WORKERS` 默认 **1** | 降低大视频并发 OOM |
| `stop_grace_period: 120s` | `up --build` / `stop` 时留时间收尾任务 |
| `healthcheck` 超时 30s、`start_period` 15min | 首启加载模型、重负载时少误报 unhealthy |
| `init: true` | 减少 ffmpeg 僵尸进程 |

大文件无体积限制时，**内存仍是硬约束**；监控 `docker stats multimodal-embedding-service` 接近 limit 时应降并发或加内存。

---

## 八、常见问题排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 容器 **RestartCount 很高** | OOM、启动失败、端口冲突 | 见上文 **7.3** |
| 仍写 `embedding_service.log` | 跑的是旧镜像 | `docker compose up -d --build` |
| tmp 一直很大 | 多任务并发中，或 OOM 后未重启 | 看 `/stats`；重启后查清扫日志 |
| 视频任务大量失败 | OSS 未配、模型未就绪、内存不足 | `docker compose logs app` 查错误 |
| `unhealthy` 持续很久 | 首次下载/加载模型慢（可达 ~15 分钟） | 等 `/health` 返回 200 |
| 权限错误写 models | UID 不匹配 | `sudo chown -R 1000:1000 models` |

### 查失败任务类型

```bash
docker compose exec app python -c "
import sqlite3
c = sqlite3.connect('/app/runtime/tasks.db')
for r in c.execute('''
    SELECT status, error_type, COUNT(*)
    FROM tasks
    GROUP BY status, error_type
    ORDER BY 3 DESC
    LIMIT 10
'''):
    print(r)
"
```

---

## 九、发版检查清单

- [ ] 已 `git pull` / 更新代码包
- [ ] 已执行 `docker compose up -d --build`（不是仅 restart）
- [ ] `curl .../health` 返回正常
- [ ] 日志中有 `Startup runtime cleanup`
- [ ] `docker compose logs -f app` 能看日志
- [ ] 升级后已按需清理旧 `runtime/logs` 残留
- [ ] `.env` 中 OSS、端口配置正确

---

## 十、相关文件

| 文件 | 说明 |
|---|---|
| `docker-compose.yml` | 服务编排、volume、runtime 清扫环境变量 |
| `Dockerfile` | 镜像构建 |
| `.env.example` | 环境变量模板 |
| `scripts/deploy.sh` | 一键部署脚本 |
| `embedding_service/PARAMS.md` | 全部环境变量说明 |
| `DEPLOY.md` | 完整部署指南（含裸机部署） |
