# Video Clip Frontend

本目录是仓库当前唯一保留的完整手动/自动工作台前端。

- `src/api/client.ts` 已对齐 `/api/v1` 与 OSS `clips/` 预存对象列表工作流。
- 产品流程：`landing → editing → execute`（见 `src/App.tsx`）。

本地启动：

```bash
cd frontend/reference-ui
npm install
npm run dev
```

如果当前已经在仓库根目录，必须先进入 `frontend/reference-ui`；不要在仓库根目录或 `frontend/` 父目录直接执行 `npm run dev`，否则会因为找不到当前前端的 `package.json` 而启动失败。

如果当前已经位于 `frontend/` 目录，则执行：

```bash
cd reference-ui
npm run dev
```

开发服务器默认把 `/api` 代理到 `http://127.0.0.1:8010`。Windows 前端与 Linux 后端分离时，可使用 SSH 隧道，或将 `vite.config.ts` 中的 proxy target 改为 Linux API 地址。
