"""Configuration without reading or logging secrets."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        raw_path = os.getenv("SUPPORT_DB_PATH", "data/support.db").strip()
        if not raw_path:
            raise ValueError("SUPPORT_DB_PATH cannot be empty")
        level = os.getenv("SUPPORT_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("SUPPORT_LOG_LEVEL must be a standard logging level")
        return cls(Path(raw_path).expanduser().resolve(), level)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
