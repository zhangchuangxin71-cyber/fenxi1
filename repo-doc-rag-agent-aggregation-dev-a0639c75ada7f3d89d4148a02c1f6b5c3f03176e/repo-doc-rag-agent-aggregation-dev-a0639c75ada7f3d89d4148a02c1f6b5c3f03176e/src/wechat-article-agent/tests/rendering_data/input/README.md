# 排版烟测输入

这里保存一组从开发环境 `article_artifacts` 只读抽取的真实模型产物，供多主题确定性排版烟测使用。

- `article.md`：LLM 生成且已审批的未排版 Markdown 正文。
- `images.json`：Seedream 图片 URL、图注及插入位置，结构与业务 artifact 一致。
- `task_spec.json`：对应的已审批任务书。
- `manifest.json`：不含业务 ID 的来源说明和文件 SHA-256。

普通测试只读取这些文件。需要刷新时，在本地开发数据库可用且确认目标数据不敏感后执行：

```bash
uv run python tests/rendering_data/capture_fixture.py --artifact-id <development_artifact_id>
uv run python tests/rendering_data/render_all.py
```

图片是业务层实际保存的短期签名 URL，不在 fixture 中下载或改写。URL 失效后，HTML 的结构、内容和图片
一致性测试仍然有效，但浏览器视觉验收前应从一次新的真实生成整体刷新 fixture。
