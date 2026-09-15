# Generated from langgraph-cli 0.4.31 for langgraph-api 0.12.3, then pinned to
# Python 3.12 and the no-Redis in-process runtime required by this service.
ARG LANGGRAPH_API_BASE_IMAGE=langchain/langgraph-api:0.12.3-py3.12-bookworm
FROM ${LANGGRAPH_API_BASE_IMAGE}

ARG PYPI_MIRROR_BASE=https://pypi.tuna.tsinghua.edu.cn
ENV UV_INDEX_URL=${PYPI_MIRROR_BASE}/simple \
    PIP_INDEX_URL=${PYPI_MIRROR_BASE}/simple

ADD . /deps/wechat-article-agent

RUN for dep in /deps/*; do \
        if [ -d "$dep" ]; then \
            (cd "$dep" && PYTHONDONTWRITEBYTECODE=1 uv pip install \
                --system --no-cache-dir --index-url "${UV_INDEX_URL}" -c /api/constraints.txt -e .); \
        fi; \
    done \
    && PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir \
        --index-url "${UV_INDEX_URL}" -c /api/constraints.txt "langgraph-runtime-inmem==0.32.3"

ENV LANGGRAPH_HTTP='{"disable_store":true}' \
    LANGGRAPH_CHECKPOINTER='{"path":"/deps/wechat-article-agent/app/persistence/checkpointer.py:create_checkpointer","ttl":{"strategy":"delete","default_ttl":4320,"sweep_interval_minutes":5,"sweep_limit":100},"serde":{"pickle_fallback":false,"allowed_json_modules":null,"allowed_msgpack_modules":null}}' \
    LANGSERVE_GRAPHS='{"wechat_article":"/deps/wechat-article-agent/app/graph/builder.py:graph","contract_probe":"/deps/wechat-article-agent/app/graph/contract_probe.py:graph"}' \
    LANGGRAPH_RUNTIME_EDITION=inmem \
    LANGGRAPH_DISABLE_FILE_PERSISTENCE=false \
    N_JOBS_PER_WORKER=30

RUN mkdir -p /api/langgraph_api /api/langgraph_runtime /api/langgraph_license \
    && touch /api/langgraph_api/__init__.py /api/langgraph_runtime/__init__.py /api/langgraph_license/__init__.py \
    && PYTHONDONTWRITEBYTECODE=1 uv pip install --system --no-cache-dir --no-deps -e /api \
    && pip uninstall -y pip setuptools wheel \
    && rm -rf /usr/local/lib/python*/site-packages/pip* \
        /usr/local/lib/python*/site-packages/setuptools* \
        /usr/local/lib/python*/site-packages/wheel* \
    && find /usr/local/bin -name 'pip*' -delete \
    && uv pip uninstall --system pip setuptools wheel \
    && rm -f /usr/bin/uv /usr/bin/uvx

WORKDIR /runtime-data

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /runtime-data app \
    && chown -R app:app /runtime-data /deps/wechat-article-agent

USER app

EXPOSE 8141

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8141/ok', timeout=4).read()"

# The upstream image is configured for the distributed postgres runtime and
# inherits /storage/entrypoint.sh. Reset it so the explicit in-memory CLI below
# performs its own startup initialization and does not require Redis.
ENTRYPOINT []

CMD ["python", "-m", "langgraph_api.cli", "--host", "0.0.0.0", "--port", "8141", "--no-reload", "--config", "/deps/wechat-article-agent/langgraph.runtime.json", "--n-jobs-per-worker", "30", "--runtime-edition", "inmem"]
