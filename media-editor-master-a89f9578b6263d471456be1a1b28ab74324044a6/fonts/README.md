# 硬字幕字体目录

Flow B 合成与 `visual/preview` 预览时，FFmpeg/libass 从此目录加载字体文件，烧录硬字幕。

- **本地开发**：默认 `<项目根>/fonts`
- **Docker**：`docker-compose.yml` 将 `./fonts` 只读挂载到 `/app/fonts`（环境变量 `FONTS_DIR=/app/fonts`）

> **CodeUp 仓库已包含完整 `fonts/` 目录**（约 17 个 `.ttf`/`.otf`，~150MB）。部署时 `git pull` 即可，**勿**只手动放思源黑体而忽略其余文件。  
> 字体**不打进 Docker 镜像**（见 `.dockerignore`），运行时依赖宿主机 `./fonts` bind mount。

---

## 1. 支持格式

| 格式 | 支持 |
|------|------|
| `.ttf` | ✅ |
| `.otf` | ✅ |
| `.ttc` | ❌（请解压/转换为 `.otf` 或 `.ttf`） |

可放在本目录根下，或子目录（无根级字体时会递归扫描）。

---

## 2. 推荐字体（与 API 默认一致）

| 用途 | API `font_name` | 建议文件名示例 | 说明 |
|------|-----------------|----------------|------|
| **默认** | `思源黑体` | `SourceHanSansSC-Regular.otf` | 竖屏口播最常用 |
| 备选 | `迷茫体` | `新愚公迷茫体.ttf` | 别名，映射 ASS 族名「新愚公迷茫体」 |
| 备选 | `拼搏体` | `新愚公拼搏体.ttf` | 同上 |
| 备选 | `文楷` / `霞鹜文楷` | `霞鹜文楷（LXGWWenKai-Medium）.ttf` | 开源楷体 |
| 备选 | `竹石体` | `杨任东竹石体-Medium.ttf` | |
| 备选 | `星汉等宽` | `星汉等宽(milky-term-cn-heavyitalic).ttf` | |

完整可选列表（含别名）：**GET** `/api/v1/subtitle/fonts`

```bash
curl -s http://127.0.0.1:8787/api/v1/subtitle/fonts | python3 -m json.tool
```

> **Docker 注意**：容器内无 `fontconfig`/`fontTools` 时，上述接口可能返回 **空列表 `choices: []`**，但硬字幕烧录仍可用（别名在代码中静态维护）。**判断字体是否就绪请用下方「部署检查」列文件数，不要单靠 `/subtitle/fonts`。**

在 `visual/preview` 请求体中设置 `"font_name": "思源黑体"`（或上表别名）；compose 会自动沿用 preview 保存的样式。

---

## 3. 获取字体（开源示例）

### 思源黑体 Source Han Sans SC（推荐）

Adobe 与 Google 联合发布，免费可商用（OFL）。

1. 打开 [Source Han Sans 发布页](https://github.com/adobe-fonts/source-han-sans/releases)
2. 下载 **Language Specific OTFs Simplified Chinese (简体中文)** 压缩包
3. 解压后将 **Regular**（或 Medium）的 `.otf` 复制到本目录，例如：

   ```
   fonts/SourceHanSansSC-Regular.otf
   ```

### 霞鹜文楷 LXGW WenKai

1. 打开 [LXGW WenKai 发布页](https://github.com/lxgw/LxgwWenKai/releases)
2. 下载 `LXGWWenKai-Regular.ttf`（或 OTF）到本目录

### 新愚公迷茫体 / 拼搏体

通常为项目采购或设计侧提供的 `.ttf`/`.otf`，请按授权范围使用。放入本目录后，API 会通过文件名与内置别名自动识别。

---

## 4. 目录示例（与 CodeUp 一致）

```
fonts/
├── README.md
├── SourceHanSansSC-Bold.otf
├── 新愚公迷茫体.ttf
├── 新愚公拼搏体.ttf
├── 杨任东竹石体-Medium.ttf
├── 星汉等宽(milky-term-cn-heavyitalic).ttf
├── 霞鹜文楷（LXGWWenKai-Medium）.ttf
└── …（共约 17 个字体文件，见 `git ls-files fonts/`）
```

---

## 5. 部署检查

### 从 CodeUp 拉代码后（推荐）

```bash
git pull origin master

# 宿主机应有约 17 个字体（勿只保留思源黑体）
ls fonts/*.{ttf,otf} 2>/dev/null | wc -l
du -sh fonts
```

若文件不全，恢复仓库版本：

```bash
git checkout origin/master -- fonts/
```

### 本地开发

```bash
# 克隆后 fonts/ 已随仓库；若缺失见上节 git checkout
curl -s http://127.0.0.1:8787/api/v1/subtitle/fonts
```

### Docker

```bash
git pull origin master
ls fonts/*.{ttf,otf} 2>/dev/null | wc -l   # 期望 ≥ 17

docker compose up -d --build

# 容器内应与宿主机一致
docker compose exec app ls -la /app/fonts
docker compose exec app sh -c 'ls /app/fonts/*.{ttf,otf} 2>/dev/null | wc -l'
```

`scripts/deploy.sh` 在 `fonts/` **为空或文件数少于 17** 时会警告；发版仍建议人工核对容器内挂载。

### 烧录抽检（最可靠）

调 `POST /api/v1/visual/preview/run`，`subtitle_style.font_name` 设为 `竹石体` 或 `星汉等宽`，打开返回的 `preview_image_url` JPG：字幕正常即 fonts 挂载无误；若整行方块则检查宿主机 `./fonts` 是否缺对应 `.ttf`。

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| 预览/成片字幕变方块 | 宿主机 `./fonts` 缺文件（常见：只放了思源+迷茫体） | `git checkout origin/master -- fonts/` 后 `docker compose up -d` |
| 预览/成片字体不对但非方块 | `fonts/` 为空或未挂载 | 确认 `docker-compose.yml` 中 `./fonts:/app/fonts:ro` |
| 部分字体正常、部分方块 | 部署时未拉全 Git 中 fonts | 核对 `ls fonts/*.ttf` 是否含竹石体/星汉等宽/文楷 |
| `subtitle/fonts` 返回 `choices: []` | Docker 无 fontconfig（已知） | **可忽略**；以文件数 + preview JPG 为准 |
| Docker 内看不到新字体 | 未挂载或 cwd 不对 | 必须在项目根执行 compose；改 fonts 后无需 rebuild |
| 换了字体但 compose 仍是旧样式 | 未重新 preview | 再调 `POST .../visual/preview` 后 `compose/run` |

---

## 7. 相关文档

- [DOCKER_OPS.md](../DOCKER_OPS.md) — Docker 挂载说明
- [api-flow-b-fastapi-walkthrough.md](../docs/api-flow-b-fastapi-walkthrough.md) — 步骤 6 字幕预览
- [api-flow-b-fastapi-service-api.md](../docs/api-flow-b-fastapi-service-api.md) — `GET /api/v1/subtitle/fonts`
