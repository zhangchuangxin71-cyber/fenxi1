ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ARG DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PORT=8100

WORKDIR /app

# System dependencies:
# - libreoffice: DOCX/PPTX conversion support
# - CJK fonts: avoid Chinese text/layout issues during office conversion
# - libpq5: PostgreSQL client runtime library
RUN set -eux; \
    for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
        [ -f "$file" ] || continue; \
        sed -i \
            -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|https://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
            -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
            -e "s|https://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
            "$file"; \
    done; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice \
        libreoffice-writer \
        libreoffice-impress \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
        libpq5 \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip config set global.index-url "${PIP_INDEX_URL}"
RUN python -m pip install --upgrade pip \
    && python -m pip install --prefer-binary -r requirements.txt

COPY app ./app

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/app/workspace \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8100\")}/healthz', timeout=3).read()"

CMD ["sh", "-c", "uvicorn app.api.app:app --host 0.0.0.0 --port ${PORT:-8100}"]
