from __future__ import annotations

DOCS_PATH = "/docs"


def docs_url(port: int, path: str = DOCS_PATH) -> str:
    """Use 127.0.0.1 (avoids Windows localhost / IPv6 delay)."""
    return f"http://127.0.0.1:{port}{path}"


def print_startup_urls(port: int) -> None:
    print("=" * 60)
    print(f"  API 文档 (Swagger):  {docs_url(port)}")
    print(f"  健康检查:            http://127.0.0.1:{port}/health")
    print("=" * 60)


def run_server(app, *, host: str, port: int, log_level: str, workers: int = 1) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        workers=workers,
    )
