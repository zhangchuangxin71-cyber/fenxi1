FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PYTHONPATH=/app \
    EMBEDDING_SERVICE_HOST=0.0.0.0 \
    EMBEDDING_SERVICE_PORT=8030 \
    CHINESE_CLIP_MODEL_PATH=/app/models \
    VECTOR_STORE=none

WORKDIR /app

RUN set -eux; \
    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "$file" ] || continue; \
        sed -i \
            -e 's|http://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
            -e 's|https://deb.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
            -e 's|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
            -e 's|https://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' \
            -e 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
            -e 's|https://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' \
            "$file"; \
    done; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip config set global.index-url "${PIP_INDEX_URL}" \
    && python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --prefer-binary -r requirements.txt

COPY embed_core ./embed_core
COPY embedding_service ./embedding_service
COPY start_embedding_service.py pyproject.toml README.md ./

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/models /app/runtime/logs /app/runtime/tmp \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8030

HEALTHCHECK --interval=45s --timeout=30s --start-period=900s --retries=6 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"EMBEDDING_SERVICE_PORT\", \"8030\")}/health', timeout=20).read()"

CMD ["python", "start_embedding_service.py"]
