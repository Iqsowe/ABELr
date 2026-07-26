"""Central logging config for the ``abelr`` logger tree.

Attaches a rotating file sink (INFO) plus a stderr sink (WARNING) once, at
process start. Workers under the tree (``abelr.fresh_render``,
``abelr.neutral_preview``, ``abelr.exif_profile``, ``abelr.response``, ...)
already log via ``logging.getLogger("abelr.*")``, so a single ``configure()``
call covers all of them through propagation.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOGGER_NAME = "abelr"
LOG_FILENAME = "abelr_app.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


def configure() -> logging.Logger:
    """Attach handlers to the ``abelr`` logger. Idempotent — safe to call more than once."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.INFO)

    plugin_root = Path(__file__).resolve().parent.parent
    log_path = plugin_root / LOG_FILENAME

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    _configured = True
    return logger
