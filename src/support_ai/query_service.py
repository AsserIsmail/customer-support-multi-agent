"""Bounded Oracle query execution on a fresh read-only runtime connection."""

import asyncio
from datetime import date, datetime, time
from decimal import Decimal
import json
import logging
import math
from time import monotonic
from uuid import uuid4

import oracledb

from support_ai.config import OracleSettings
from support_ai.sql_validation import QueryError, validate_query, qualify_query

logger = logging.getLogger(__name__)
MAX_ROWS = 100
MAX_COLUMNS = 50
MAX_CELL_CHARS = 4000
MAX_RESULT_BYTES = 65536


async def _cell(value, truncation: set[str]):
    if isinstance(value, oracledb.AsyncLOB):
        if value.type not in (oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_NCLOB):
            raise QueryError("unsupported_result", "Query returned an unsupported data type")
        value = await value.read(amount=MAX_CELL_CHARS + 1)
    if isinstance(value, str):
        if len(value) > MAX_CELL_CHARS:
            truncation.add("cell_limit")
        return value[:MAX_CELL_CHARS]
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min and value.tzinfo is None else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryError("unsupported_result", "Query returned a non-finite number")
        return int(value) if value == value.to_integral_value() else str(value)
    if value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value):
        return value
    raise QueryError("unsupported_result", "Query returned an unsupported data type")


class ReadonlyQueryService:
    def __init__(self, settings: OracleSettings):
        self.settings = settings
        self._slots = asyncio.Semaphore(2)

    async def execute(self, sql: str, parameters: dict | None = None, row_limit: int = 50) -> dict:
        query_id, started = uuid4().hex, monotonic()
        try:
            if type(row_limit) is not int or not 1 <= row_limit <= MAX_ROWS:
                raise QueryError("invalid_parameters", "Row limit must be 1..100")
            query = validate_query(sql, parameters, self.settings)
            async with asyncio.timeout(self.settings.query_timeout_seconds):
                async with self._slots:
                    async with oracledb.connect_async(
                        user=self.settings.user, password=self.settings.password, dsn=self.settings.dsn,
                        tcp_connect_timeout=self.settings.connect_timeout_seconds,
                    ) as conn:
                        conn.call_timeout = min(self.settings.call_timeout_ms, self.settings.query_timeout_seconds * 1000)
                        # The async driver sends a break and resets its protocol on
                        # cancellation. A second cancel would interrupt cleanup.
                        result = await self._run(conn, query, row_limit)
            result["query_id"] = query_id
            logger.info("SQL execution id=%s outcome=success rows=%d elapsed_ms=%d",
                        query_id, len(result["rows"]), (monotonic() - started) * 1000)
            return result
        except QueryError as exc:
            logger.warning("SQL execution id=%s outcome=%s", query_id, exc.code)
            raise
        except TimeoutError:
            logger.warning("SQL execution id=%s outcome=timeout", query_id)
            raise QueryError("timeout", "Query exceeded its execution deadline; narrow the request") from None
        except asyncio.CancelledError:
            logger.info("SQL execution id=%s outcome=cancelled", query_id)
            raise
        except oracledb.Error as exc:
            code = getattr(exc.args[0], "full_code", "") if exc.args else ""
            if code in {"DPY-4024", "DPI-1067", "DPI-1080", "ORA-01013"}:
                logger.warning("SQL execution id=%s outcome=timeout", query_id)
                raise QueryError("timeout", "Query exceeded a database call deadline; narrow the request") from None
            logger.warning("SQL execution id=%s outcome=database_error", query_id)
            raise QueryError("database_error", "Query could not be completed; check SQL, schema, and reader grants") from None
        except Exception as exc:
            logger.warning("SQL execution id=%s outcome=database_error class=%s", query_id, type(exc).__name__)
            raise QueryError("database_error", "Query could not be completed; check SQL, schema, and reader grants") from None

    async def _run(self, conn, query, row_limit):
        with conn.cursor() as cursor:
            cursor.arraysize = row_limit + 1
            cursor.prefetchrows = row_limit + 1
            await cursor.execute("SET TRANSACTION READ ONLY")
            binds = {"owner": self.settings.schema.upper(), **{f"t{i}": name for i, name in enumerate(query.tables)}}
            placeholders = ", ".join(f":t{i}" for i in range(len(query.tables)))
            await cursor.execute("SELECT table_name FROM all_tables WHERE owner = :owner "
                                 f"AND table_name IN ({placeholders})", binds)
            accessible = {row[0] for row in await cursor.fetchmany(len(query.tables) + 1)}
            if accessible != set(query.tables):
                raise QueryError("unauthorized_object", "An approved table is unavailable to the runtime account")
            await cursor.execute("SELECT table_name, column_name, data_type FROM all_tab_columns "
                                 f"WHERE owner = :owner AND table_name IN ({placeholders}) ORDER BY table_name, column_id", binds)
            column_rows = await cursor.fetchmany(8001)
            if len(column_rows) > 8000:
                raise QueryError("unsupported_result", "Query schema exceeds supported column limits")
            schema = {table: {} for table in query.tables}
            for table, column, data_type in column_rows:
                schema[table][column] = data_type
            qualified_sql = qualify_query(query, self.settings.schema.upper(), schema)
            # Outer stopkey cannot be defeated by a caller-supplied LIMIT/FETCH.
            # A numeric literal here is application-validated, never user SQL text.
            bounded = f"SELECT * FROM ({qualified_sql}) WHERE ROWNUM <= {row_limit + 1}"
            await cursor.execute(bounded, query.parameters, fetch_lobs=True, fetch_decimals=True)
            if len(cursor.description) > MAX_COLUMNS:
                raise QueryError("unsupported_result", "Select at most 50 result columns")
            columns = [{"name": info[0], "data_type": info[1].name} for info in cursor.description]
            rows, reasons = [], set()
            tables = [{"owner": self.settings.schema.upper(), "table": name} for name in query.tables]
            size = len(json.dumps(columns, ensure_ascii=True).encode()) + len(json.dumps(tables).encode()) + 2048
            # Fetch only one bounded batch. LOB content is read separately with a cap.
            fetched = await cursor.fetchmany(row_limit + 1)
            if len(fetched) > row_limit:
                reasons.add("row_limit")
            for row in fetched[:row_limit]:
                converted = [await _cell(value, reasons) for value in row]
                row_bytes = len(json.dumps(converted, ensure_ascii=True).encode()) + 2
                if size + row_bytes > MAX_RESULT_BYTES:
                    reasons.add("byte_limit")
                    break
                rows.append(converted)
                size += row_bytes
            return {"status": "found" if rows else "truncated" if reasons else "empty", "columns": columns,
                    "rows": rows, "row_count": len(rows), "row_limit": row_limit,
                    "truncated": bool(reasons), "truncation_reasons": sorted(reasons),
                    "tables": tables}
