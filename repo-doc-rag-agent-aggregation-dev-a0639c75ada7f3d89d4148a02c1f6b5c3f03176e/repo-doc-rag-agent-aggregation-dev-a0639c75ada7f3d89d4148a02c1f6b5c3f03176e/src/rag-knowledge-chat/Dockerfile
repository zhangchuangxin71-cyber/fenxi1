ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG UV_VERSION=0.9.27
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8130

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    --index-url "${UV_DEFAULT_INDEX}" \
    "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

EXPOSE 8130

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('PORT', '8130'); urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=3).read()"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8130}"]
