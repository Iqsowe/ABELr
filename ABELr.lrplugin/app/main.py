"""Entry point — starts the FastAPI server (thread) + the PySide6 GUI (main thread).

Launch:
    python -m app.main          (from ABELr/)
    or  cd app && python main.py
"""

from __future__ import annotations

import logging
import sys
import threading

import uvicorn


class _PollFilter(logging.Filter):
    """Suppresses /jobs/pending polling logs (204) — too verbose."""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("GET /jobs/pending" in msg and "204" in msg)

HOST = "127.0.0.1"
PORT = 5000


def _run_server() -> None:
    """Uvicorn server — blocking, so launched in a daemon thread.

    Any exception at startup (port already held by an orphaned Python
    process, etc.) is surfaced: otherwise the thread dies silently and the
    bridge looks "broken" on the plugin side when the real cause is that the
    server never listened.
    """
    from app.server.api import app

    logging.getLogger("uvicorn.access").addFilter(_PollFilter())
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    except Exception as exc:
        import traceback

        print(
            f"\n[ABELr] ERROR: the HTTP server could not start on "
            f"{HOST}:{PORT}.\n  Cause: {exc!r}\n  Is an orphaned python.exe "
            f"process already holding port {PORT}? (Task Manager -> end "
            f"python.exe, then relaunch)\n",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exc()


def main() -> int:
    from app.logging_setup import configure as configure_logging

    configure_logging()

    # HTTP server in a daemon thread (stops with the GUI).
    server_thread = threading.Thread(target=_run_server, daemon=True, name="fastapi")
    server_thread.start()

    # GUI on the main thread (required by Qt).
    from PySide6.QtWidgets import QApplication

    from app.gui.main_window import MainWindow

    qt_app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    # Enables `python app/main.py` by adding the project root to the path.
    if __package__ in (None, ""):
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
