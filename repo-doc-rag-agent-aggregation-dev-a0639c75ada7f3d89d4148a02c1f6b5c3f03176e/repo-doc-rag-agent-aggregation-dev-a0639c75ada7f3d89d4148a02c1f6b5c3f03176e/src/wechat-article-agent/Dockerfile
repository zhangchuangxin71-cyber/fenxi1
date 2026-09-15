ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG UV_VERSION=0.11.29
ARG UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_MIRROR_BASE=https://pypi.tuna.tsinghua.edu.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH" \
    APP_HOST=0.0.0.0 \
    APP_PORT=8140

WORKDIR /app

RUN python -m pip install --no-cache-dir \
    --index-url "${UV_DEFAULT_INDEX}" \
    "uv==${UV_VERSION}"

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app

COPY pyproject.toml uv.lock README.md ./
RUN set -eux; \
    mirror="${PYPI_MIRROR_BASE%/}"; \
    sed -i \
        -e "s|https://files.pythonhosted.org/packages|${mirror}/packages|g" \
        -e "s|https://pypi.org/simple|${mirror}/simple|g" \
        uv.lock; \
    uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app app ./app

RUN chown -R app:app /app

USER app

EXPOSE 8140

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('APP_PORT', '8140'); urllib.request.urlopen('http://127.0.0.1:%s/health/ready' % port, timeout=4).read()"

# Keep one worker: admission, fairness, RPM buckets and breakers are process-local.
CMD ["sh", "-c", "uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8140} --workers 1"]
