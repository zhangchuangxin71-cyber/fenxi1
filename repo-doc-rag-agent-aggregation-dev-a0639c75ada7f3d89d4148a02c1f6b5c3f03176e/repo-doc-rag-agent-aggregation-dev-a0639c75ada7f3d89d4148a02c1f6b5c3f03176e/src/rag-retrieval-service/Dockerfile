ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG UV_VERSION=0.11.29
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    PATH="/app/.venv/bin:$PATH" \
    APP_PORT=8120

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    --index-url "${UV_DEFAULT_INDEX}" \
    "uv==${UV_VERSION}"

RUN addgroup --system app \
    && adduser --system --ingroup app app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts

USER app

EXPOSE 8120

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('PORT') or os.getenv('APP_PORT', '8120'); urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=4).read()"

# Keep one worker: the LLM gateway, circuit breaker, DB executor and user
# limiter are intentionally process-local and globally shared by this worker.
CMD ["sh", "-c", "uvicorn app.api.app:app --host 0.0.0.0 --port ${PORT:-${APP_PORT:-8120}} --workers 1"]
