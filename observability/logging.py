"""Structured Logging Setup mit strukturierten JSON-Logs und OpenTelemetry-Vorbereitung."""

import logging
import sys
from typing import Any


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    """
    Konfiguriert das globale Logging-System.

    Im 'json'-Modus werden strukturierte JSON-Logs ausgegeben,
    die direkt von Loki, Fluentd oder ähnlichen Systemen verarbeitet werden können.
    """
    if log_format == "json":
        _setup_json_logging(level)
    else:
        _setup_text_logging(level)


def _setup_text_logging(level: str) -> None:
    # force=True überschreibt uvicorn's bereits gesetzten Root-Handler
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _setup_json_logging(level: str) -> None:
    """JSON-Logging via structlog falls verfügbar, sonst Fallback auf stdlib."""
    try:
        import structlog

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.ExceptionRenderer(),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, level)
            ),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        )
    except ImportError:
        # Fallback: stdlib logging mit JSON-ähnlichem Format
        import json

        class JSONFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                log_data: dict[str, Any] = {
                    "timestamp": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                }
                if record.exc_info:
                    log_data["exception"] = self.formatException(record.exc_info)
                return json.dumps(log_data, ensure_ascii=False)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logging.root.setLevel(getattr(logging, level))
        logging.root.handlers = [handler]


def get_logger(name: str) -> logging.Logger:
    """Gibt einen konfigurierten Logger für das angegebene Modul zurück."""
    return logging.getLogger(name)
