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
    HOST=0.0.0.0 \
    PORT=8115

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
        libpq5 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip config set global.index-url "${PIP_INDEX_URL}" \
    && python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --prefer-binary -r requirements.txt

COPY app ./app
COPY main.py pyproject.toml README.md ./

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8115

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8115\")}/health', timeout=3).read()"

CMD ["sh", "-c", "uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8115}"]
