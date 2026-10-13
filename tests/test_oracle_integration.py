"""Opt-in tests against a provisioned, dedicated Oracle demo PDB.

Run with SUPPORT_ORACLE_TESTS=1. Target must be empty or match the synthetic seed.
No setup, reset, or destructive cleanup is performed by this suite.
"""

import asyncio
import os
from uuid import uuid4

import oracledb
import pytest

from support_ai.config import OracleSettings
from support_ai.mcp_client import connect_support_tools
from support_ai.oracle_database import OracleCustomerRepository
from support_ai.oracle_migrate import migrate
from support_ai.seed import seed_database

# Capture opt-in config before the default test fixture isolates environment vars.
LIVE = {key: os.getenv(key, "") for key in (
    "SUPPORT_ORACLE_TESTS", "SUPPORT_ORACLE_DSN", "SUPPORT_ORACLE_OWNER_PASSWORD", "SUPPORT_ORACLE_PASSWORD")}
pytestmark = pytest.mark.skipif(LIVE["SUPPORT_ORACLE_TESTS"] != "1", reason="Dedicated Oracle integration environment not enabled")


@pytest.fixture
def migrated(tmp_path):
    assert all(LIVE.values()), "Set the Oracle test DSN and owner/reader passwords"
    source = tmp_path / "source.db"
    seed_database(source)
    first = migrate(source, LIVE["SUPPORT_ORACLE_DSN"], LIVE["SUPPORT_ORACLE_OWNER_PASSWORD"])
    assert first["counts"] == {"customers": 25, "orders": 75, "tickets": 75}
    assert migrate(source, LIVE["SUPPORT_ORACLE_DSN"], LIVE["SUPPORT_ORACLE_OWNER_PASSWORD"])["status"] == "unchanged"
    return OracleSettings("SUPPORT_READER", LIVE["SUPPORT_ORACLE_PASSWORD"], LIVE["SUPPORT_ORACLE_DSN"], "SUPPORT_OWNER")


def test_live_migration_and_lookup_contract(migrated):
    repo = OracleCustomerRepository(migrated)
    assert len(repo.find_customers("emma")) == 2
    assert repo.find_customers("%") == []
    assert repo.get_customer(1)["joined_on"] == "2024-01-04"
    assert repo.get_orders(1, limit=1, offset=1)[0]["id"] == 102
    assert len(repo.get_tickets(1)) == 3


def test_live_reader_cannot_write_or_create(migrated):
    with oracledb.connect(user=migrated.user, password=migrated.password, dsn=migrated.dsn) as conn:
        with conn.cursor() as cursor:
            for sql in ("UPDATE SUPPORT_OWNER.customers SET name = name WHERE 1 = 0",
                        "DELETE FROM SUPPORT_OWNER.orders WHERE 1 = 0",
                        "CREATE TABLE forbidden_probe (id NUMBER)"):
                with pytest.raises(oracledb.DatabaseError) as error:
                    cursor.execute(sql)
                # Oracle 26ai reports missing object privileges as ORA-41900;
                # ORA-01031 remains the general insufficient-privileges error.
                assert error.value.args[0].code in {1031, 41900}
            conn.rollback()


def test_live_composite_foreign_key(migrated):
    with oracledb.connect(user="SUPPORT_OWNER", password=LIVE["SUPPORT_ORACLE_OWNER_PASSWORD"], dsn=migrated.dsn) as conn:
        try:
            with conn.cursor() as cursor:
                with pytest.raises(oracledb.DatabaseError) as error:
                    cursor.execute("UPDATE tickets SET order_id = 201 WHERE id = 101")
                assert error.value.args[0].code == 2291
        finally:
            conn.rollback()


def test_live_mcp_oracle_path(migrated, tmp_path):
    env = dict(os.environ, SUPPORT_DB_BACKEND="oracle", SUPPORT_ORACLE_USER=migrated.user,
               SUPPORT_ORACLE_PASSWORD=migrated.password, SUPPORT_ORACLE_DSN=migrated.dsn,
               SUPPORT_ORACLE_SCHEMA=migrated.schema)
    # The child runtime needs no setup/migration credentials.
    env.pop("SUPPORT_ORACLE_OWNER_PASSWORD", None)
    env.pop("SUPPORT_ORACLE_ADMIN_PASSWORD", None)

    async def run():
        async with connect_support_tools(cwd=tmp_path, env=env) as tools:
            assert (await tools.call("customer_lookup", {"query": "Emma"}))["status"] == "ambiguous"
            page = await tools.call("order_lookup", {"customer_id": 1, "limit": 1})
            assert page["orders"][0]["id"] == 103 and page["next_offset"] == 1
            discovered = {tool.name for tool in await tools.list_tools()}
            assert {"list_tables", "describe_table", "get_relationships", "search_schema", "refresh_schema_index"} <= discovered
            listing = await tools.call("list_tables", {})
            assert {item["table"] for item in listing["tables"]} == {"CUSTOMERS", "ORDERS", "TICKETS"}
            description = await tools.call("describe_table", {"table": "orders"})
            assert description["schema_version"] == listing["schema_version"]
            columns = {col["column_name"]: col for col in description["table"]["columns"]}
            assert columns["AMOUNT_CENTS"]["data_type"] == "NUMBER"
            assert columns["AMOUNT_CENTS"]["nullable"] is False
            assert any(index["index_name"] == "ORDERS_CUSTOMER" for index in description["table"]["indexes"])
            assert any(c["constraint_type"] == "P" for c in description["table"]["constraints"])
            relations = await tools.call("get_relationships", {"table": "orders"})
            composite = next(rel for rel in relations["relationships"] if rel["constraint_name"] == "TICKETS_ORDER_OWNER")
            assert [(pair["column"], pair["referenced_column"]) for pair in composite["column_pairs"]] == [
                ("ORDER_ID", "ID"), ("CUSTOMER_ID", "CUSTOMER_ID")]
            assert (await tools.call("search_schema", {"query": "amount cents"}))["matches"][0]["column"] == "AMOUNT_CENTS"
            assert (await tools.call("refresh_schema_index", {}))["schema_version"] == listing["schema_version"]
    asyncio.run(run())


def test_live_schema_refresh_tracks_ddl_and_grants(migrated, tmp_path):
    # Only this uniquely named disposable table is changed or dropped, never demo records.
    table = "SCHEMA_PROBE_" + uuid4().hex[:12].upper()
    env = dict(os.environ, SUPPORT_DB_BACKEND="oracle", SUPPORT_ORACLE_USER=migrated.user,
               SUPPORT_ORACLE_PASSWORD=migrated.password, SUPPORT_ORACLE_DSN=migrated.dsn,
               SUPPORT_ORACLE_SCHEMA=migrated.schema,
               SUPPORT_ORACLE_ALLOWED_TABLES=f"CUSTOMERS,ORDERS,TICKETS,{table}")
    env.pop("SUPPORT_ORACLE_OWNER_PASSWORD", None)
    env.pop("SUPPORT_ORACLE_ADMIN_PASSWORD", None)
    with oracledb.connect(user="SUPPORT_OWNER", password=LIVE["SUPPORT_ORACLE_OWNER_PASSWORD"], dsn=migrated.dsn) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {table} (id NUMBER PRIMARY KEY)")
            try:
                async def run():
                    async with connect_support_tools(cwd=tmp_path, env=env) as tools:
                        assert (await tools.call("describe_table", {"table": table}))["status"] == "not_found"
                        cursor.execute(f"GRANT SELECT ON {table} TO SUPPORT_READER")
                        initial = await tools.call("refresh_schema_index", {})
                        assert (await tools.call("describe_table", {"table": table}))["status"] == "found"
                        cursor.execute(f"ALTER TABLE {table} ADD tracking_reference VARCHAR2(100)")
                        assert (await tools.call("search_schema", {"query": "tracking_reference"}))["status"] == "no_matches"
                        refreshed = await tools.call("refresh_schema_index", {})
                        assert refreshed["schema_version"] != initial["schema_version"]
                        matches = (await tools.call("search_schema", {"query": "tracking_reference"}))["matches"]
                        assert matches[0]["table"] == table and matches[0]["column"] == "TRACKING_REFERENCE"
                        # The database grant alone does not authorize metadata exposure.
                        from dataclasses import replace
                        from support_ai.schema_catalog import SchemaCatalog
                        limited = SchemaCatalog(replace(migrated, allowed_tables=("ORDERS",)))
                        assert [item["table"] for item in limited.list_tables()["tables"]] == ["ORDERS"]
                        assert limited.get_relationships()["relationships"] == []
                        assert (await tools.call("execute_readonly_query", {"sql": f"SELECT id FROM {table}"}))["status"] == "empty"
                        cursor.execute(f"REVOKE SELECT ON {table} FROM SUPPORT_READER")
                        # Metadata remains cached, but execution must independently deny access.
                        denied = await tools.session.call_tool("execute_readonly_query", {"sql": f"SELECT id FROM {table}"})
                        assert denied.is_error and denied.structured_content["code"] == "unauthorized_object"
                        await tools.call("refresh_schema_index", {})
                        assert (await tools.call("describe_table", {"table": table}))["status"] == "not_found"
                        assert (await tools.call("search_schema", {"query": "tracking_reference"}))["status"] == "no_matches"
                asyncio.run(run())
            finally:
                cursor.execute(f"DROP TABLE {table} PURGE")


def test_live_readonly_queries_through_mcp(migrated, tmp_path):
    env = dict(os.environ, SUPPORT_DB_BACKEND="oracle", SUPPORT_ORACLE_USER=migrated.user,
               SUPPORT_ORACLE_PASSWORD=migrated.password, SUPPORT_ORACLE_DSN=migrated.dsn,
               SUPPORT_ORACLE_SCHEMA=migrated.schema)
    env.pop("SUPPORT_ORACLE_OWNER_PASSWORD", None)
    env.pop("SUPPORT_ORACLE_ADMIN_PASSWORD", None)

    async def run():
        async with connect_support_tools(cwd=tmp_path, env=env) as tools:
            assert "execute_readonly_query" in {tool.name for tool in await tools.list_tools()}
            result = await tools.call("execute_readonly_query", {
                "sql": "SELECT o.id, o.placed_on, t.description FROM orders o JOIN tickets t "
                       "ON t.order_id=o.id AND t.customer_id=o.customer_id "
                       "WHERE o.customer_id=:customer_id ORDER BY o.id", "parameters": {"customer_id": 1}, "row_limit": 2})
            assert [row[0] for row in result["rows"]] == [101, 102]
            assert result["rows"][0][1] == "2025-01-11"
            assert "second display" in result["rows"][0][2]
            assert result["truncated"] is True and result["truncation_reasons"] == ["row_limit"]
            cte = await tools.call("execute_readonly_query", {"sql":
                "WITH matching AS (SELECT id FROM customers WHERE LOWER(name) LIKE :name) "
                "SELECT COUNT(*) AS total FROM matching", "parameters": {"name": "%emma%"}})
            assert cte["rows"] == [[2]]
            fractional = await tools.call("execute_readonly_query", {"sql": "SELECT id/2 AS half_id FROM orders WHERE id=101"})
            assert fractional["rows"] == [["50.5"]]
            injected = await tools.call("execute_readonly_query", {"sql": "SELECT id FROM customers WHERE name=:name",
                "parameters": {"name": "' OR 1=1 --"}})
            assert injected["status"] == "empty"
            for sql in ("DELETE FROM orders", "SELECT * FROM SYS.USER$", "SELECT * FROM orders FOR UPDATE",
                        "SELECT UTL_HTTP.REQUEST('https://example.com') FROM orders", "SELECT * FROM orders@remote",
                        "SELECT * FROM orders; SELECT * FROM tickets", "SELECT nonexistent_column FROM orders",
                        "SELECT secret_package.read_secret FROM orders"):
                result = await tools.session.call_tool("execute_readonly_query", {"sql": sql})
                assert result.is_error
                assert sql not in result.model_dump_json()
            after = await tools.call("execute_readonly_query", {"sql": "SELECT COUNT(*) AS total FROM orders"})
            assert after["rows"] == [[75]]
    asyncio.run(run())


def test_live_query_deadline_cancels_and_next_query_succeeds(migrated):
    from dataclasses import replace
    from time import monotonic
    from support_ai.query_service import ReadonlyQueryService
    from support_ai.sql_validation import QueryError
    service = ReadonlyQueryService(replace(migrated, query_timeout_seconds=1))

    async def run():
        started = monotonic()
        with pytest.raises(QueryError) as error:
            await service.execute("SELECT SUM(MOD(a.id*b.id*c.id*d.id*e.id*f.id,97)) FROM orders a "
                                  "JOIN orders b ON 1=1 JOIN orders c ON 1=1 JOIN orders d ON 1=1 "
                                  "JOIN orders e ON 1=1 JOIN orders f ON 1=1")
        assert error.value.code == "timeout", repr(error.value.__context__)
        assert monotonic() - started < 15
        result = await service.execute("SELECT id FROM orders ORDER BY id", row_limit=1)
        assert result["rows"] == [[101]]
    asyncio.run(run())
