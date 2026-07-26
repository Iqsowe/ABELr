"""PLAN.md L2 — `app/gui/log_bridge.py` unit test.

No `MainWindow`/window needed (COV5: GUI workers stay manual-only) — this
tests the handler itself. Emission from a foreign thread is auto-queued to
the handler's thread affinity, so the test pumps `processEvents()` once
after joining the emitting thread to deliver it.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QCoreApplication

from app.gui.log_bridge import QtLogHandler

# A QCoreApplication instance is required for Qt to resolve signal/slot thread
# affinity even without an event loop running.
_qt_app = QCoreApplication.instance() or QCoreApplication([])


def test_error_from_background_thread_reaches_signal():
    handler = QtLogHandler(level=logging.WARNING)
    received = []
    handler.record.connect(received.append)

    logger = logging.getLogger("abelr.test_log_bridge")
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    logger.propagate = False

    try:
        def _log():
            logger.error("boom from worker thread")

        t = threading.Thread(target=_log)
        t.start()
        t.join(timeout=5)
        # Emission from a foreign thread is auto-queued to the handler's
        # (main-thread) affinity — pump the event loop once to deliver it.
        _qt_app.processEvents()
    finally:
        logger.removeHandler(handler)

    assert len(received) == 1
    assert "boom from worker thread" in received[0]
    assert "ERROR" in received[0]


def test_below_level_is_not_emitted():
    handler = QtLogHandler(level=logging.WARNING)
    received = []
    handler.record.connect(received.append)

    logger = logging.getLogger("abelr.test_log_bridge_info")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False

    try:
        logger.info("quiet info message")
    finally:
        logger.removeHandler(handler)

    assert received == []
