"""Uvicorn logging for `lazy serve`: every line timestamped, the health probe kept out.

Uvicorn's default config prints ``INFO:     1.2.3.4:5 - "GET /x HTTP/1.1" 200 OK`` with no time,
which is useless next to the app's own ``%(asctime)s`` lines when reading a night's log back.
"""

from __future__ import annotations

import logging
from typing import Any

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
ACCESS_FORMAT = (
    '%(asctime)s %(levelname)-7s access: %(client_addr)s "%(request_line)s" %(status_code)s'
)
HEALTH_PATH = "/health"


class DropHealthProbes(logging.Filter):
    """Keeps the container healthcheck (every 30 s) out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access passes (client_addr, method, path, http_version, status_code)
        return not (isinstance(args, tuple) and len(args) >= 3 and args[2] == HEALTH_PATH)


def uvicorn_log_config(*, quiet: bool = False) -> dict[str, Any]:
    """A dictConfig for ``uvicorn.run(log_config=...)``; ``quiet`` silences the access log."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"drop_health": {"()": f"{__name__}.DropHealthProbes"}},
        "formatters": {
            "default": {"format": FORMAT},
            "access": {"()": "uvicorn.logging.AccessFormatter", "fmt": ACCESS_FORMAT},
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "filters": ["drop_health"],
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "WARNING" if quiet else "INFO",
                "propagate": False,
            },
        },
    }


__all__ = ["ACCESS_FORMAT", "DropHealthProbes", "FORMAT", "uvicorn_log_config"]
