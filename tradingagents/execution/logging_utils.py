from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from tradingagents.execution.models import ExecutionConfig


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            }
        }
        if extras:
            base["extra"] = extras
        if record.exc_info:
            base["exception"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


class AuditTrail:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **payload: Any) -> None:
        entry = {"event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str))
            handle.write("\n")


def setup_logging(
    config: ExecutionConfig,
    *,
    run_id: str,
) -> tuple[logging.Logger, Path]:
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    logger = logging.getLogger(f"tradingagents.execution.{run_id}")
    logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    formatter = StructuredJsonFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger, log_path


def redact_secrets(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted = {}
        for key, value in payload.items():
            if "secret" in key.lower() or "key" in key.lower():
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact_secrets(value)
        return redacted
    if isinstance(payload, list):
        return [redact_secrets(item) for item in payload]
    return payload
