from __future__ import annotations

from hashlib import md5

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(title="Mock Image MCP", version="0.1.0")


class ImageSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=3, ge=1, le=10)
    section_title: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/images/search")
def search_images(req: ImageSearchRequest):
    # This mock returns stable placeholder image URLs so report insertion can be tested end-to-end.
    images = []
    for idx in range(1, req.top_k + 1):
        seed = md5(f"{req.query}-{idx}".encode("utf-8")).hexdigest()[:8]
        width = 960
        height = 540
        title = f"{req.query} 配图 {idx}"
        images.append(
            {
                "image_id": f"mock_{seed}",
                "title": title,
                "url": f"https://picsum.photos/seed/{seed}/{width}/{height}",
                "description": f"与“{req.query}”相关的测试占位图片，适用章节：{req.section_title or '未指定'}",
                "source": "mock-image-mcp",
                "license": "test-only",
            }
        )
    return {"images": images}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8012)
