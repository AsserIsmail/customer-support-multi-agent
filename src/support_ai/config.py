"""Configuration without reading or logging secrets."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False)
        raw_path = os.getenv("SUPPORT_DB_PATH", "data/support.db").strip()
        if not raw_path:
            raise ValueError("SUPPORT_DB_PATH cannot be empty")
        level = os.getenv("SUPPORT_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("SUPPORT_LOG_LEVEL must be a standard logging level")
        return cls(Path(raw_path).expanduser().resolve(), level)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # SDK debug output can include document text and request bodies.
    for name in ("openai", "httpx", "httpcore", "chromadb"):
        logging.getLogger(name).setLevel(logging.WARNING)


@dataclass(frozen=True)
class PolicySettings:
    chroma_path: Path
    collection: str = "company_policies"
    embedding_model: str = "text-embedding-3-small"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    max_distance: float = 0.6

    @classmethod
    def from_env(cls) -> "PolicySettings":
        load_dotenv(Path.cwd() / ".env", override=False)
        raw_path = os.getenv("CHROMA_PATH", "data/chroma").strip()
        if not raw_path:
            raise ValueError("CHROMA_PATH cannot be empty")
        settings = cls(
            Path(raw_path).expanduser().resolve(),
            os.getenv("CHROMA_COLLECTION", "company_policies").strip(),
            os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small").strip(),
            int(os.getenv("POLICY_CHUNK_SIZE", "1200")),
            int(os.getenv("POLICY_CHUNK_OVERLAP", "200")),
            float(os.getenv("POLICY_MAX_DISTANCE", "0.6")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.collection or not self.embedding_model:
            raise ValueError("Collection and embedding model cannot be empty")
        if not 100 <= self.chunk_size <= 4000 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("Chunk size must be 100..4000 and overlap smaller than size")
        if not 0 <= self.max_distance <= 2:
            raise ValueError("POLICY_MAX_DISTANCE must be between 0 and 2")
