"""`lazy serve` log lines carry a timestamp and the health probe stays out of the access log."""

from __future__ import annotations

import logging
import logging.config
import re

from lazy_sleeper.api.logconfig import DropHealthProbes, uvicorn_log_config

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ")


def _access(path: str, status: int = 200) -> logging.LogRecord:
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("192.168.86.27:1091", "GET", path, "1.1", status),
        None,
    )
    return rec


def test_health_probes_are_dropped_and_everything_else_kept() -> None:
    f = DropHealthProbes()
    assert not f.filter(_access("/health"))
    assert f.filter(_access("/draft/1/state"))
    assert f.filter(logging.LogRecord("uvicorn.error", logging.INFO, "x", 1, "started", (), None))


def test_config_timestamps_access_and_default_lines(capsys) -> None:  # noqa: ANN001
    logging.config.dictConfig(uvicorn_log_config())
    logging.getLogger("uvicorn.access").handle(_access("/draft/1/state"))
    logging.getLogger("uvicorn.access").handle(_access("/health"))
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    out, err = capsys.readouterr()
    access = [ln for ln in out.splitlines() if ln]
    assert len(access) == 1 and STAMP.match(access[0])
    assert '"GET /draft/1/state HTTP/1.1" 200' in access[0] and "/health" not in out
    assert STAMP.match(err.strip()) and "startup complete" in err


def test_quiet_silences_the_access_log() -> None:
    cfg = uvicorn_log_config(quiet=True)
    assert cfg["loggers"]["uvicorn.access"]["level"] == "WARNING"
