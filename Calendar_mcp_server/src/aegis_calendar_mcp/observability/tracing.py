"""最小进程日志配置。"""

import logging


def configure_logging() -> None:
    """配置启动和运行错误的基础日志。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
