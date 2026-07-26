"""PLAN.md L1 — `app/logging_setup.py` unit tests.

Covers: idempotency (no duplicate handlers on repeated configure()), file
handler is present and rotating, a child logger under the `abelr` tree
propagates to it.
"""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from app import logging_setup


@pytest.fixture(autouse=True)
def _reset_abelr_logger():
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_configured = logging_setup._configured
    logger.handlers.clear()
    logging_setup._configured = False
    yield
    for h in logger.handlers:
        h.close()
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)
    logging_setup._configured = saved_configured


def test_configure_attaches_file_and_stream_handlers():
    logger = logging_setup.configure()
    assert len(logger.handlers) == 2
    file_handlers = [
        h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename.endswith(logging_setup.LOG_FILENAME)


def test_configure_is_idempotent():
    logging_setup.configure()
    logging_setup.configure()
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    assert len(logger.handlers) == 2


def test_child_logger_propagates_to_configured_handlers(caplog):
    logging_setup.configure()
    child = logging.getLogger("abelr.some_worker")

    with caplog.at_level(logging.INFO, logger=logging_setup.LOGGER_NAME):
        child.info("hello from child")

    assert any("hello from child" in rec.message for rec in caplog.records)
