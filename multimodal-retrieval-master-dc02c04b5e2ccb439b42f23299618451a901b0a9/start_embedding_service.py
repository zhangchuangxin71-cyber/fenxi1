#!/usr/bin/env python
"""
启动 Embedding Service 的入口脚本。

用法（项目根目录）:
    EMBEDDING_SERVICE_PORT=8030 python start_embedding_service.py

裸机后台启动（推荐，避免终端出现 [1]+ Killed）:
    bash scripts/start_embedding_service.sh
    bash scripts/stop_embedding_service.sh

启动后会在终端打印 http://127.0.0.1:<port>/docs，请自行在浏览器打开。
无 start_embedding_service.ps1；Windows 也可用上述 python 命令。
"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 导入并运行服务
if __name__ == "__main__":
    from embedding_service.api import app
    from embedding_service.config import ServiceConfig
    from embedding_service.launch import run_server
    from embedding_service.logging_config import setup_logging
    import logging

    # 初始化配置（缺失模型时自动下载）
    from embedding_service.model_bootstrap import ensure_models_downloaded

    config = ServiceConfig.from_env()
    config = ensure_models_downloaded(config)
    config.validate()

    # 设置日志（仅控制台，不写 embedding_service.log）
    setup_logging(level=config.log_level)
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("Starting Media Embedding Service")
    logger.info(f"Version: 0.3.0")
    logger.info(f"Host: {config.service_host}")
    logger.info(f"Port: {config.service_port}")
    logger.info(f"CLIP Model: {config.clip_model_path}")
    logger.info(f"CLIP Device: {config.clip_device}")
    logger.info(f"BGE Model: {config.bge_model_name}")
    logger.info(f"BGE Device: {config.bge_device}")
    logger.info("=" * 60)

    run_server(
        app,
        host=config.service_host,
        port=config.service_port,
        log_level=config.log_level,
    )
