"""Fixed Oracle lookups using bound values and a separately provisioned reader.

Connections are opened lazily per operation and closed on success or failure.
This module neither creates a schema nor accepts caller-supplied SQL.
"""

from datetime import date, datetime
from decimal import Decimal
import logging

import oracledb

from support_ai.config import OracleSettings
from support_ai.database import DataAccessError, _positive_id

logger = logging.getLogger(__name__)

_COLUMNS = {
    "customers": "id, name, email, joined_on, plan",
    "orders": "id, customer_id, placed_on, product, amount_cents, currency, status",
    "tickets": "id, customer_id, order_id, opened_on, subject, description, status",
}


def _value(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("Expected integral customer data")
        return int(value)
    if isinstance(value, oracledb.LOB):
        return value.read()
    return value


class OracleCustomerRepository:
    def __init__(self, settings: OracleSettings):
        self.settings = settings

    def _read(self, sql: str, parameters: dict) -> list[dict]:
        try:
            with oracledb.connect(user=self.settings.user, password=self.settings.password,
                                  dsn=self.settings.dsn,
                                  tcp_connect_timeout=self.settings.connect_timeout_seconds) as conn:
                conn.call_timeout = self.settings.call_timeout_ms
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(sql, parameters)
                    columns = [item[0].lower() for item in cursor.description]
                    return [dict(zip(columns, (_value(value) for value in row))) for row in cursor.fetchall()]
        except (oracledb.Error, ValueError) as exc:
            logger.error("Oracle customer lookup failed (%s)", type(exc).__name__)
            raise DataAccessError("Customer database is unavailable; check setup and configuration.") from None

    def _select(self, table: str) -> str:
        # Both table and column names come exclusively from module constants.
        return f"SELECT {_COLUMNS[table]} FROM {self.settings.schema}.{table}"

    def find_customers(self, query: str) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Customer search must contain 1 to 200 characters")
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._read(self._select("customers") +
            " WHERE LOWER(name) LIKE LOWER(:pattern) ESCAPE '\\'"
            " OR LOWER(email) LIKE LOWER(:pattern) ESCAPE '\\' ORDER BY id",
            {"pattern": f"%{escaped}%"})

    def get_customer(self, customer_id: int) -> dict | None:
        _positive_id(customer_id)
        rows = self._read(self._select("customers") + " WHERE id = :customer_id", {"customer_id": customer_id})
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
        return self._read(self._select(table) + f" WHERE customer_id = :customer_id"
            f" ORDER BY {date_column} DESC, id DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
            {"customer_id": customer_id, "offset": offset, "limit": limit})
