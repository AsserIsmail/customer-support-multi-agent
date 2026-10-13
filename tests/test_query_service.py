import asyncio
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
from unittest.mock import AsyncMock, MagicMock

import oracledb
import pytest

from support_ai.config import OracleSettings
from support_ai.query_service import ReadonlyQueryService, MAX_RESULT_BYTES
from support_ai.sql_validation import QueryError

SCHEMA_ROWS = [("ORDERS", "ID", "NUMBER"), ("ORDERS", "AMOUNT_CENTS", "NUMBER"), ("ORDERS", "PLACED_ON", "DATE")]


@pytest.fixture
def driver(monkeypatch):
    connect = MagicMock()
    conn = connect.return_value.__aenter__.return_value
    conn.cancel = MagicMock()
    cursor = MagicMock()
    conn.cursor = MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=cursor)))
    cursor.execute = AsyncMock()
    cursor.fetchmany = AsyncMock(side_effect=[[("ORDERS",)], SCHEMA_ROWS, [(1, Decimal("12.50"), datetime(2025, 1, 1))]])
    cursor.description = [("ID", oracledb.DB_TYPE_NUMBER), ("AMOUNT", oracledb.DB_TYPE_NUMBER), ("PLACED_ON", oracledb.DB_TYPE_DATE)]
    monkeypatch.setattr(oracledb, "connect_async", connect)
    settings = OracleSettings("reader", "private-password", "private-host/service", "SUPPORT_OWNER")
    return ReadonlyQueryService(settings), connect, conn, cursor


def test_execution_is_readonly_bounded_and_json_safe(driver, caplog):
    service, connect, conn, cursor = driver
    caplog.set_level("INFO")
    result = asyncio.run(service.execute("SELECT id, amount_cents/100 amount, placed_on FROM orders WHERE id=:id", {"id": 1}, 2))
    assert result["rows"] == [[1, "12.50", "2025-01-01"]]
    assert result["truncated"] is False
    sql, binds = cursor.execute.call_args.args
    assert sql.endswith("WHERE ROWNUM <= 3")
    assert '"SUPPORT_OWNER"."ORDERS"' in sql and binds == {"id": 1}
    assert cursor.execute.call_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    assert "all_tables" in cursor.execute.call_args_list[1].args[0]
    assert cursor.execute.call_args.kwargs == {"fetch_lobs": True, "fetch_decimals": True}
    assert "private-password" not in caplog.text and "SELECT" not in caplog.text
    assert result["query_id"] in caplog.text
    connect.return_value.__aexit__.assert_awaited_once()
    assert len(json.dumps(result).encode()) < MAX_RESULT_BYTES


def test_unsafe_sql_never_connects(driver):
    service, connect, _, _ = driver
    with pytest.raises(QueryError):
        asyncio.run(service.execute("DELETE FROM orders"))
    connect.assert_not_called()


def test_missing_grant_is_checked_without_using_catalog_cache(driver):
    service, _, _, cursor = driver
    cursor.fetchmany.side_effect = [[]]
    with pytest.raises(QueryError) as error:
        asyncio.run(service.execute("SELECT * FROM orders"))
    assert error.value.code == "unauthorized_object"
    assert cursor.execute.await_count == 2


@pytest.mark.parametrize("kind", ["rows", "cells", "bytes"])
def test_result_truncation_is_explicit(driver, kind):
    service, _, _, cursor = driver
    cursor.description = [("DATA", oracledb.DB_TYPE_VARCHAR)]
    values = [(i,) for i in range(4)] if kind == "rows" else [("x" * 4001,)] if kind == "cells" else [("x" * 4000,)] * 100
    limit = 3 if kind == "rows" else 100
    cursor.fetchmany.side_effect = [[("ORDERS",)], SCHEMA_ROWS, values]
    result = asyncio.run(service.execute("SELECT id FROM orders", row_limit=limit))
    assert result["truncated"] is True
    assert {"rows": "row_limit", "cells": "cell_limit", "bytes": "byte_limit"}[kind] in result["truncation_reasons"]
    assert len(result["rows"]) <= limit
    assert len(json.dumps(result).encode()) <= MAX_RESULT_BYTES


def test_database_error_does_not_leak_sql_values_or_credentials(driver, caplog):
    service, connect, _, cursor = driver
    cursor.execute.side_effect = oracledb.DatabaseError("private-password private-host sensitive-record")
    with pytest.raises(QueryError) as error:
        asyncio.run(service.execute("SELECT * FROM orders"))
    assert error.value.code == "database_error"
    assert not any(word in str(error.value) + caplog.text for word in ("private-password", "private-host", "sensitive-record"))
    connect.return_value.__aexit__.assert_awaited_once()


def test_deadline_cancels_query_and_closes_connection(driver):
    service, connect, conn, cursor = driver
    service.settings = replace(service.settings, query_timeout_seconds=1)
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    cursor.execute.side_effect = blocked
    with pytest.raises(QueryError) as error:
        asyncio.run(service.execute("SELECT * FROM orders"))
    assert error.value.code == "timeout"
    conn.cancel.assert_called_once()
    connect.return_value.__aexit__.assert_awaited_once()


def test_external_cancellation_is_not_swallowed(driver):
    service, connect, conn, cursor = driver
    async def run():
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        cursor.execute.side_effect = blocked
        task = asyncio.create_task(service.execute("SELECT * FROM orders"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    conn.cancel.assert_called_once()
    connect.return_value.__aexit__.assert_awaited_once()


def test_unknown_column_or_package_attribute_never_executes_user_query(driver):
    service, _, _, cursor = driver
    with pytest.raises(QueryError) as error:
        asyncio.run(service.execute("SELECT evil_package.secret FROM orders"))
    assert error.value.code == "invalid_sql"
    assert cursor.execute.await_count == 3  # read-only transaction and two metadata checks only


def test_large_lob_is_read_with_a_cap(driver):
    service, _, _, cursor = driver
    lob = MagicMock(spec=oracledb.AsyncLOB)
    lob.type = oracledb.DB_TYPE_CLOB
    lob.read = AsyncMock(return_value="x" * 4001)
    cursor.description = [("ID", oracledb.DB_TYPE_CLOB)]
    cursor.fetchmany.side_effect = [[("ORDERS",)], SCHEMA_ROWS, [(lob,)]]
    result = asyncio.run(service.execute("SELECT id FROM orders"))
    lob.read.assert_awaited_once_with(amount=4001)
    assert len(result["rows"][0][0]) == 4000
    assert result["truncation_reasons"] == ["cell_limit"]


@pytest.mark.parametrize("value", [float("nan"), b"binary", object()])
def test_unsupported_result_values_fail_safely(driver, value):
    service, _, _, cursor = driver
    cursor.fetchmany.side_effect = [[("ORDERS",)], SCHEMA_ROWS, [(value,)]]
    with pytest.raises(QueryError) as error:
        asyncio.run(service.execute("SELECT id FROM orders"))
    assert error.value.code == "unsupported_result"


@pytest.mark.parametrize("value", [0, 101, True, "10"])
def test_invalid_row_limit_never_connects(driver, value):
    service, connect, _, _ = driver
    with pytest.raises(QueryError):
        asyncio.run(service.execute("SELECT id FROM orders", row_limit=value))
    connect.assert_not_called()
