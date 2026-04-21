from dataclasses import dataclass
import json
import logging
import os
import socket

import structlog

logging_levels = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging_levels[os.getenv("LOG_LEVEL", "INFO")]
    ),
    processors=[
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(sort_keys=False),
    ],
)
log: structlog.BoundLogger = structlog.get_logger()


@dataclass
class Session:
    cookies: dict[str, str]
    headers: dict[str, str]
    proxy: str | None = None
    extra: dict[str, str] | None = None


def send_heartbeat(source: str) -> None:
    magent_running = os.getenv("MAGENT_RUNNING", "false").lower() in ("true", "1", "yes")
    if not magent_running:
        return
    payload = {
        "type": "heartbeat",
        "scraper": source,
        "msg": None,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect("/tmp/itxpmonitor.sock")
            sock.sendall(json.dumps(payload).encode())
    except Exception:
        log.exception("failed_to_send_heartbeat", source=source)

