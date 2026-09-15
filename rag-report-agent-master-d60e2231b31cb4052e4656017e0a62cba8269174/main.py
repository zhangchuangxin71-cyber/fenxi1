import uvicorn
from fastapi import FastAPI

from app.api.routes import router
from app.config import settings
from app.llm.registry import init_providers
from app.logging_ext.setup import setup_logging


def create_app(*, init_llm: bool = True) -> FastAPI:
    setup_logging()
    if init_llm:
        init_providers()
    app = FastAPI(title=settings.app_name)
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
