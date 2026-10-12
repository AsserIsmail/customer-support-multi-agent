"""Opt-in tests against a provisioned, dedicated Oracle demo PDB.

Run with SUPPORT_ORACLE_TESTS=1. Target must be empty or match the synthetic seed.
No setup, reset, or destructive cleanup is performed by this suite.
"""

import asyncio
import os

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
                assert error.value.args[0].code == 1031
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
    asyncio.run(run())
