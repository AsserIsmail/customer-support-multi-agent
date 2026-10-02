"""Schema administration and restricted customer record retrieval.

Only initialization/seeding opens a writable connection. Runtime lookups use
SQLite's read-only URI mode and accept values, never SQL supplied by a caller.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    joined_on TEXT NOT NULL,
    plan TEXT NOT NULL CHECK (plan IN ('basic', 'pro', 'enterprise'))
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    placed_on TEXT NOT NULL,
    product TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    status TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'refunded'))
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_id INTEGER,
    opened_on TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'pending', 'closed')),
    FOREIGN KEY (order_id, customer_id) REFERENCES orders(id, customer_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_owner ON orders(id, customer_id);
CREATE INDEX IF NOT EXISTS orders_customer ON orders(customer_id, placed_on);
CREATE INDEX IF NOT EXISTS tickets_customer ON tickets(customer_id, opened_on);
"""


class DataAccessError(RuntimeError):
    """A database lookup could not be completed; distinct from no matching rows."""


@contextmanager
def connection(path: Path, *, readonly: bool = True) -> Iterator[sqlite3.Connection]:
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        with conn:
            yield conn
    finally:
        conn.close()


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connection(path, readonly=False) as conn:
        conn.executescript(SCHEMA)


def _positive_id(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("ID must be a positive integer")


class CustomerRepository:
    """Internal data layer for the future MCP server, not an agent SQL interface."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self, sql: str, parameters: tuple) -> list[dict]:
        try:
            with connection(self.path) as conn:
                return [dict(row) for row in conn.execute(sql, parameters).fetchall()]
        except sqlite3.Error as exc:
            logger.error("Customer database lookup failed (%s)", type(exc).__name__)
            raise DataAccessError("Customer database is unavailable; check setup and configuration.") from exc

    def find_customers(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Customer search must contain 1 to 200 characters")
        # Escape LIKE metacharacters so user input remains a literal substring.
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        return self._read(
            "SELECT * FROM customers WHERE name LIKE ? ESCAPE '\\' "
            "OR email LIKE ? ESCAPE '\\' ORDER BY id", (pattern, pattern)
        )

    def get_customer(self, customer_id: int) -> dict | None:
        _positive_id(customer_id)
        rows = self._read("SELECT * FROM customers WHERE id = ?", (customer_id,))
        return rows[0] if rows else None

    def get_orders(self, customer_id: int, *, limit: int = 50, offset: int = 0) -> list[dict]:
        return self._history("orders", "placed_on", customer_id, limit, offset)

    def get_tickets(self, customer_id: int, *, limit: int = 50, offset: int = 0) -> list[dict]:
        return self._history("tickets", "opened_on", customer_id, limit, offset)

    def _history(self, table: str, date_column: str, customer_id: int, limit: int, offset: int) -> list[dict]:
        _positive_id(customer_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("Offset must be a nonnegative integer")
        # Identifiers originate only from the two fixed methods above.
        return self._read(
            f"SELECT * FROM {table} WHERE customer_id = ? "
            f"ORDER BY {date_column} DESC, id DESC LIMIT ? OFFSET ?",
            (customer_id, limit, offset),
        )
