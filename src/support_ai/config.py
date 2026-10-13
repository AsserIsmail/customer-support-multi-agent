"""Configuration without reading or logging secrets."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
import re

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_level: str = "INFO"
    db_backend: str = "sqlite"
    oracle: "OracleSettings | None" = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path.cwd() / ".env", override=False)
        raw_path = os.getenv("SUPPORT_DB_PATH", "data/support.db").strip()
        if not raw_path:
            raise ValueError("SUPPORT_DB_PATH cannot be empty")
        level = os.getenv("SUPPORT_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("SUPPORT_LOG_LEVEL must be a standard logging level")
        backend = os.getenv("SUPPORT_DB_BACKEND", "sqlite").strip().lower()
        if backend not in {"sqlite", "oracle"}:
            raise ValueError("SUPPORT_DB_BACKEND must be sqlite or oracle")
        return cls(Path(raw_path).expanduser().resolve(), level, backend,
                   OracleSettings.from_env() if backend == "oracle" else None)


@dataclass(frozen=True)
class OracleSettings:
    # Exclude connection details from representations and incidental logging.
    user: str = field(repr=False)
    password: str = field(repr=False)
    dsn: str = field(repr=False)
    schema: str
    connect_timeout_seconds: int = 5
    call_timeout_ms: int = 5000
    allowed_tables: tuple[str, ...] = ("CUSTOMERS", "ORDERS", "TICKETS")
    schema_cache_ttl_seconds: int = 300
    query_timeout_seconds: int = 10

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.user, self.password, self.dsn)):
            raise ValueError("Set SUPPORT_ORACLE_USER, SUPPORT_ORACLE_PASSWORD, and SUPPORT_ORACLE_DSN")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", self.schema):
            raise ValueError("SUPPORT_ORACLE_SCHEMA must be a simple unquoted Oracle identifier")
        if not 1 <= self.connect_timeout_seconds <= 60 or not 1 <= self.call_timeout_ms <= 120000:
            raise ValueError("Oracle connection timeout must be 1..60 seconds and call timeout 1..120000 ms")
        if (not isinstance(self.allowed_tables, tuple) or not 1 <= len(self.allowed_tables) <= 50
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
                       for name in self.allowed_tables) or len(set(self.allowed_tables)) != len(self.allowed_tables)):
            raise ValueError("Oracle allowed tables must be 1..50 distinct uppercase unquoted identifiers")
        if type(self.schema_cache_ttl_seconds) is not int or not 1 <= self.schema_cache_ttl_seconds <= 3600:
            raise ValueError("Oracle schema cache TTL must be 1..3600 seconds")
        if type(self.query_timeout_seconds) is not int or not 1 <= self.query_timeout_seconds <= 60:
            raise ValueError("Oracle query timeout must be 1..60 seconds")

    @classmethod
    def from_env(cls) -> "OracleSettings":
        try:
            connect_timeout = int(os.getenv("SUPPORT_ORACLE_CONNECT_TIMEOUT_SECONDS", "5"))
            call_timeout = int(os.getenv("SUPPORT_ORACLE_CALL_TIMEOUT_MS", "5000"))
            schema_ttl = int(os.getenv("SUPPORT_ORACLE_SCHEMA_CACHE_TTL_SECONDS", "300"))
            query_timeout = int(os.getenv("SUPPORT_ORACLE_QUERY_TIMEOUT_SECONDS", "10"))
        except ValueError:
            raise ValueError("Oracle timeout settings must be integers") from None
        return cls(os.getenv("SUPPORT_ORACLE_USER", "").strip(),
                   os.getenv("SUPPORT_ORACLE_PASSWORD", ""),
                   os.getenv("SUPPORT_ORACLE_DSN", "").strip(),
                   os.getenv("SUPPORT_ORACLE_SCHEMA", "SUPPORT_OWNER").strip().upper(),
                   connect_timeout, call_timeout,
                   tuple(name.strip().upper() for name in os.getenv(
                       "SUPPORT_ORACLE_ALLOWED_TABLES", "CUSTOMERS,ORDERS,TICKETS").split(",")), schema_ttl, query_timeout)


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
