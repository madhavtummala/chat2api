"""Entry point: ``python -m src.main`` or ``uvicorn src.main:app``."""

from __future__ import annotations

import logging

import uvicorn

from .api.app import app  # noqa: F401 - re-exported for `uvicorn src.main:app`
from .config import settings


def _configure_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def run() -> None:
    _configure_logging()
    uvicorn.run(
        "src.api.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    run()
