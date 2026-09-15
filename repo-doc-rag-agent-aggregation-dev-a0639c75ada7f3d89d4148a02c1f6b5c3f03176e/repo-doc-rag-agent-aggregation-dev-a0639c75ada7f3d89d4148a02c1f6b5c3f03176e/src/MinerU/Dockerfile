ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ARG TORCH_VERSION=2.8.0
ARG TORCHVISION_VERSION=0.23.0

ARG DEBIAN_MIRROR=http://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ARG PYTORCH_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pytorch-wheels/cpu

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINERU_MODEL_SOURCE=modelscope \
    MINERU_API_MAX_CONCURRENT_REQUESTS=1 \
    MINERU_API_TASK_RETENTION_SECONDS=3600 \
    MINERU_PDF_RENDER_THREADS=2 \
    MINERU_PROCESSING_WINDOW_SIZE=8 \
    MINERU_INTRA_OP_NUM_THREADS=4 \
    MINERU_INTER_OP_NUM_THREADS=1 \
    MINERU_API_OUTPUT_ROOT=/data/output/_api_tasks \
    CUDA_VISIBLE_DEVICES="" \
    NVIDIA_VISIBLE_DEVICES=void \
    NVIDIA_DRIVER_CAPABILITIES=""

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-noto-cjk \
        git \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libreoffice-calc \
        libreoffice-impress \
        libreoffice-writer \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE.md MinerU_CLA.md SECURITY.md ./

RUN pip config set global.index-url "${PIP_INDEX_URL}"

RUN python - <<'PY'
from pathlib import Path
import tomllib

with Path("pyproject.toml").open("rb") as stream:
    project = tomllib.load(stream)["project"]

requirements = [
    *project["dependencies"],
    *project["optional-dependencies"]["pipeline"],
]
# CPU torch and torchvision are installed from the dedicated PyTorch mirror below.
requirements = [item for item in requirements if not item.strip().lower().startswith("torch")]
Path("/tmp/mineru-requirements.txt").write_text(
    "\n".join(requirements) + "\n",
    encoding="utf-8",
)
PY

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install \
        --index-url "${PYTORCH_INDEX_URL}" \
        --extra-index-url "${PIP_EXTRA_INDEX_URL}" \
        "torch==${TORCH_VERSION}" \
        "torchvision==${TORCHVISION_VERSION}" \
    && python -m pip install \
        -i "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        -r /tmp/mineru-requirements.txt \
    && rm -f /tmp/mineru-requirements.txt

COPY mineru ./mineru
COPY demo ./demo

RUN python -m pip install --no-deps -e . \
    && python - <<'PY'
import torch
import mineru.cli.fast_api
import mineru.model

assert not torch.cuda.is_available(), "CPU image must not expose CUDA"
print(f"Verified CPU-only torch: {torch.__version__}")
PY


RUN mkdir -p /data/input /data/output

EXPOSE 8135

CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8135"]
