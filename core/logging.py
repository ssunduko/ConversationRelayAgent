"""Logging setup: everything to logs/server.log, problems to logs/server.err.log.

Configured in the application rather than by shell redirection, so the files
are written the same way however the app is started — foreground, background,
or from an IDE. Console output is kept so a foreground run still shows the call
as it happens.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from core.config import PROJECT_ROOT

LOG_DIR = Path(PROJECT_ROOT) / "logs"
OUTPUT_LOG = LOG_DIR / "server.log"
ERROR_LOG = LOG_DIR / "server.err.log"

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Roll at 5 MB, keep 3 generations. A busy line writes a lot: every caller
# utterance, tool call and knowledge lookup lands here.
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3

# uvicorn attaches its own handlers and stops propagation, so its records never
# reach the root logger unless we say otherwise.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(level: int = logging.INFO) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace rather than add: reloads and repeated calls would otherwise
    # duplicate every line.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    output = logging.handlers.RotatingFileHandler(
        OUTPUT_LOG, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    output.setFormatter(formatter)
    root.addHandler(output)

    # A filtered subset: warnings and errors only, so a problem is findable
    # without reading the whole transcript.
    errors = logging.handlers.RotatingFileHandler(
        ERROR_LOG, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    errors.setLevel(logging.WARNING)
    errors.setFormatter(formatter)
    root.addHandler(errors)

    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # These are chatty at INFO and drown out the call itself.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("twilio.http_client").setLevel(logging.WARNING)
