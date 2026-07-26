"""Bridges the ``abelr`` logging tree to a Qt signal for the GUI's Log tab.

`QtLogHandler` is a plain `logging.Handler` — no dependency on any window. Its
`record` signal is a `QObject.Signal`, so Qt auto-queues cross-thread
emission: a worker `QThread` can log and the slot still runs safely on the
GUI thread. Wired into the "Log" panel by `U2`.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal


class QtLogHandler(logging.Handler, QObject):
    record = Signal(str)

    def __init__(self, level: int = logging.WARNING) -> None:
        logging.Handler.__init__(self, level=level)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.record.emit(msg)
