"""Logging setup shared by the command line entry points.

Kept out of module import time on purpose: configuring the root logger as a
side effect of an import hijacks logging for whoever imports this package.
Entry points call setup_logging(); library code just asks for a logger.
"""

import logging
import os

import coloredlogs

DEFAULT_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s [%(filename)17s:%(lineno)3d] - %(levelname)-7s - %(message)s"


def setup_logging() -> logging.Logger:
    level = getattr(logging, os.getenv("LOG_LEVEL", "").upper(), DEFAULT_LEVEL)

    logger = logging.getLogger("fwtrack")
    logger.setLevel(level)
    coloredlogs.install(level=level, logger=logger, fmt=LOG_FORMAT)

    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fwtrack.{name}")
