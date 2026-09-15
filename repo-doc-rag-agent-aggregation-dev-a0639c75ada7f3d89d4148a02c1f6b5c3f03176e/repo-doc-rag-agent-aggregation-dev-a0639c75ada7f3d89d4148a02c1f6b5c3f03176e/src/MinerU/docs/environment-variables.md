# MinerU 部署配置说明

当前 MinerU 仓库有 CPU 专用的 `mineru-api.cpu.env`，但没有覆盖 GPU/CPU 的统一 `.env.example`。GPU `docker/compose.yaml` 将多数运行值直接写在 Compose 中；CPU `docker/compose.cpu.yaml` 只有 `JINA_API_KEY` 和 `JINA_READER_BASE_URL` 从宿主环境替换。本文记录五服务统一编排时需要保留的运行配置和生产初值，后续应在统一部署仓库中将这些值映射为 Compose 环境变量，而不是修改 MinerU 业务代码。

## 推荐部署形态

第一版生产优先使用 GPU 的 `mineru-api` 服务，容器端口为 `8000`，入库服务通过 `http://mineru-api:8000` 访问。MinerU 不应直接暴露公网或办公网；它本身不是业务鉴权边界，只允许入库服务所在的内部网络访问。

镜像当前写为 `mineru:latest`。正式部署前应替换为公司镜像仓库中的固定版本标签或 digest，避免 `latest` 随时间漂移。单 GPU 初始并发建议从 4 开始压测，再根据显存、文档页数和 OOM/延迟调整。

## 通用运行变量

| 配置项 | GPU 生产推荐值 | 含义 |
|---|---|---|
| `MINERU_MODEL_SOURCE` | `local` | 从镜像/挂载的本地模型加载，避免运行时下载导致启动不确定。 |
| `MINERU_API_MAX_CONCURRENT_REQUESTS` | `4`（当前上游 Compose 示例为 8） | API 同时处理任务上限。应与入库侧 `MINERU_CLIENT_CONCURRENCY` 对齐，并按 GPU 压测逐步上调。 |
| `MINERU_PROCESSING_WINDOW_SIZE` | `16` | 内部处理窗口。显存不足时优先压测下调，而不是继续提高并发。 |
| `MINERU_API_TASK_RETENTION_SECONDS` | `3600` | 异步任务产物保留时间。 |
| `MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS` | `300` | 任务产物清理周期。 |
| `MINERU_API_OUTPUT_ROOT` | `/data/output/_api_tasks` | API 临时产物目录。生产应挂载有容量监控和清理策略的数据卷。 |
| `MINERU_PDF_RENDER_THREADS` | `4` | PDF 渲染线程数，受 CPU 核数限制。 |

任务 retention、cleanup 和 output root 在当前 GPU Compose 中未显式设置；统一编排时建议补上，避免长时间运行后临时产物无界增长。

## CPU 专用变量

CPU 模式只适合开发、兼容回退或低吞吐环境，不建议作为大批量生产主链路。

| 配置项 | CPU 推荐初值 | 含义 |
|---|---|---|
| `MINERU_DEVICE_MODE` | `cpu` | 强制 CPU 模式。 |
| `MINERU_INTRA_OP_NUM_THREADS` | `4` | 单算子线程数，按容器 CPU limit 调整。 |
| `MINERU_INTER_OP_NUM_THREADS` | `1` | 算子间并发。 |
| `OMP_NUM_THREADS` | `4` | OpenMP 线程数。 |
| `MKL_NUM_THREADS` | `4` | MKL 线程数。 |
| `OPENBLAS_NUM_THREADS` | `4` | OpenBLAS 线程数。 |
| `CUDA_VISIBLE_DEVICES` | 空字符串 | CPU 容器不暴露 CUDA 设备。 |
| `NVIDIA_VISIBLE_DEVICES` | `void` | 禁止 NVIDIA runtime 注入 GPU。 |

这些线程值不能全部独立放大，否则会发生 CPU oversubscription。四核容器以 4/1/4/4/4 为初值；若开多个 MinerU worker，需按 worker 数重新分配。
| `NVIDIA_DRIVER_CAPABILITIES` | 空字符串 | CPU 容器不请求 NVIDIA driver capability。 |

## 可选外部读取配置

| 配置项 | 生产推荐值 | 含义 |
|---|---|---|
| `JINA_API_KEY` | 留空，确需 Jina Reader 时由 Secret 注入 | Jina Reader key。当前本地解析链路通常不需要。 |
| `JINA_READER_BASE_URL` | `https://r.jina.ai/` | Jina Reader 地址。只有启用对应能力时使用。 |

## GPU 与命令行资源项

以下不是当前 `.env` 变量，但统一 Compose 必须显式决定：

- GPU 设备：当前示例固定 `device_ids: ["0"]`。目标服务器上应确认 GPU 编号和是否与其他模型服务共享。
- `--gpu-memory-utilization`：显存不足时按 MinerU 注释从 0.5 或更低开始调节；这是命令行参数，不应伪装成环境变量。
- `ipc: host`、`memlock=-1`、较大的 stack：GPU 示例当前依赖这些资源设置。
- 健康检查：使用容器内 `http://127.0.0.1:8000/health`。
- SSRF：生产不要启用 `--allow-public-http-client`，除非明确接受并控制远程 URL 访问风险。当前 CPU Compose 启用了该选项，只适合作为隔离开发模板。

## 上线检查

1. 镜像使用固定 tag/digest，模型已预置且容器启动不依赖临时公网下载。
2. 只在内部网络暴露 8000；宿主机无需映射端口时删除 `ports`，使用 `expose` 即可。
3. GPU 型号、显存、驱动和 NVIDIA Container Toolkit 已在目标机验证。
4. 入库侧 MinerU client 并发不高于服务承载能力；用真实 PDF 页数分布压测 P95 和 OOM。
5. 输出目录有持久化/临时卷、容量告警和清理策略。
