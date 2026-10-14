from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import MagicMock

import oracledb
import pytest

from support_ai.config import OracleSettings, Settings
from support_ai.schema_catalog import OracleMetadataLoader, SchemaCatalog, SchemaError, MAX_METADATA_ROWS


def settings(**kwargs):
    return replace(OracleSettings("reader", "private-password", "private-host/service", "SUPPORT_OWNER"), **kwargs)


def metadata():
    relation = {"owner": "SUPPORT_OWNER", "table": "TICKETS", "constraint_name": "TICKETS_ORDER_OWNER",
                "referenced_owner": "SUPPORT_OWNER", "referenced_table": "ORDERS", "status": "ENABLED",
                "validated": "VALIDATED", "delete_rule": "NO ACTION", "column_pairs": [
                    {"position": 1, "column": "ORDER_ID", "referenced_column": "ID"},
                    {"position": 2, "column": "CUSTOMER_ID", "referenced_column": "CUSTOMER_ID"}]}
    return {name: {"owner": "SUPPORT_OWNER", "table": name, "comments": "Customer support data",
                   "columns": [{"column_name": "ID", "data_type": "NUMBER", "comments": None}],
                   "constraints": [], "indexes": [], "relationships": [relation] if name == "TICKETS" else []}
            for name in ("CUSTOMERS", "ORDERS", "TICKETS")}


def test_lazy_cache_search_and_defensive_copies():
    loader = MagicMock(side_effect=lambda: metadata())
    catalog = SchemaCatalog(settings(), loader=loader)
    loader.assert_not_called()
    first = catalog.list_tables()
    assert [item["table"] for item in first["tables"]] == ["CUSTOMERS", "ORDERS", "TICKETS"]
    description = catalog.describe_table("orders")
    assert description["schema_version"] == first["schema_version"]
    description["table"]["columns"].clear()
    assert catalog.describe_table("ORDERS")["table"]["columns"]
    assert catalog.search_schema("order")["matches"][0]["table"] == "ORDERS"
    assert catalog.search_schema("number")["matches"][0]["kind"] == "column"
    assert catalog.search_schema("nothing-matches-here")["status"] == "no_matches"
    assert catalog.search_schema("customer support", limit=1)["truncated"] is True
    loader.assert_called_once()
    json.dumps(catalog.get_relationships())


def test_composite_relationships_include_inbound_edges():
    catalog = SchemaCatalog(settings(), loader=metadata)
    for table in (None, "orders", "tickets"):
        relations = catalog.get_relationships(table)["relationships"]
        assert len(relations) == 1
        assert [pair["column"] for pair in relations[0]["column_pairs"]] == ["ORDER_ID", "CUSTOMER_ID"]
    assert catalog.get_relationships("customers")["status"] == "empty"


def test_refresh_and_ttl_rebuild_search_index():
    now = [0]
    current = metadata()
    loader = MagicMock(side_effect=lambda: deepcopy(current))
    catalog = SchemaCatalog(settings(schema_cache_ttl_seconds=10), loader=loader, clock=lambda: now[0])
    original = catalog.list_tables()["schema_version"]
    current["ORDERS"]["columns"].append({"column_name": "DELIVERED_ON", "data_type": "DATE", "comments": "Delivery date"})
    assert catalog.search_schema("delivered")["status"] == "no_matches"
    refreshed = catalog.refresh_schema_index()
    assert refreshed["schema_version"] != original
    assert catalog.search_schema("delivered")["matches"][0]["column"] == "DELIVERED_ON"
    stable_version = refreshed["schema_version"]
    assert catalog.refresh_schema_index()["schema_version"] == stable_version
    del current["TICKETS"]
    now[0] = 11
    assert catalog.describe_table("tickets")["status"] == "not_found"
    assert catalog.get_relationships("tickets")["status"] == "not_found"
    assert catalog.search_schema("tickets")["status"] == "no_matches"
    assert loader.call_count == 4


def test_failed_refresh_invalidates_cache_and_redacts(caplog):
    loader = MagicMock(side_effect=[metadata(), oracledb.DatabaseError("private-password private-host"),
                                    oracledb.DatabaseError("private-password private-host"), metadata()])
    catalog = SchemaCatalog(settings(), loader=loader)
    catalog.list_tables()
    for operation in (catalog.refresh_schema_index, catalog.list_tables):
        with pytest.raises(SchemaError) as error:
            operation()
        assert "private-password" not in str(error.value) + caplog.text
        assert "private-host" not in str(error.value) + caplog.text
    assert catalog.list_tables()["status"] == "found"


def test_concurrent_first_reads_share_one_snapshot():
    loader = MagicMock(side_effect=metadata)
    catalog = SchemaCatalog(settings(), loader=loader)
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(lambda _: catalog.list_tables()["schema_version"], range(20)))
    assert len(set(versions)) == 1
    loader.assert_called_once()


@pytest.mark.parametrize("name", ["SYS.USERS", "CUSTOMERS@remote", '"CUSTOMERS"', "ORDERS; DROP TABLE X", "SECRETS", "", None])
def test_invalid_or_unauthorized_table_never_loads(name):
    loader = MagicMock()
    catalog = SchemaCatalog(settings(), loader=loader)
    with pytest.raises(ValueError):
        catalog.describe_table(name)
    loader.assert_not_called()


@pytest.mark.parametrize("query,limit", [(" ", 1), (";--", 1), ("x" * 401, 1), (None, 1), ("id", 0), ("id", 51), ("id", True)])
def test_invalid_search_never_loads(query, limit):
    loader = MagicMock()
    with pytest.raises(ValueError):
        SchemaCatalog(settings(), loader=loader).search_schema(query, limit)
    loader.assert_not_called()


def test_empty_and_unauthorized_catalog():
    catalog = SchemaCatalog(settings(), loader=lambda: {})
    assert catalog.list_tables()["status"] == "empty"
    assert catalog.describe_table("orders")["status"] == "not_found"
    with pytest.raises(SchemaError):
        SchemaCatalog(settings(), loader=lambda: {"SECRETS": {}}).list_tables()


@pytest.mark.parametrize("tables,ttl", [("", "300"), ("ORDERS,ORDERS", "300"), ("SYS.USERS", "300"),
                                       ("ORDERS", "0"), ("ORDERS", "3601"), ("ORDERS", "secret-invalid")])
def test_invalid_environment_configuration(monkeypatch, tables, ttl):
    for name, value in {"SUPPORT_DB_BACKEND": "oracle", "SUPPORT_ORACLE_USER": "reader",
                        "SUPPORT_ORACLE_PASSWORD": "private-password", "SUPPORT_ORACLE_DSN": "private-host/service",
                        "SUPPORT_ORACLE_ALLOWED_TABLES": tables, "SUPPORT_ORACLE_SCHEMA_CACHE_TTL_SECONDS": ttl}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "secret-invalid" not in str(error.value)


def test_oracle_loader_dictionary_queries_and_composite_order(monkeypatch):
    connect = MagicMock()
    conn = connect.return_value.__enter__.return_value
    cursor = conn.cursor.return_value.__enter__.return_value
    results = [
        [{"table_name": "ORDERS", "comments": None}, {"table_name": "TICKETS", "comments": None}],
        [{"table_name": "ORDERS", "column_name": "ID", "nullable": "N"}],
        [{"table_name": "TICKETS", "constraint_name": "FK", "constraint_type": "R", "status": "ENABLED",
          "validated": "VALIDATED", "condition": None, "column_name": column, "position": i}
         for i, column in enumerate(("ORDER_ID", "CUSTOMER_ID"), 1)],
        [{"table_name": "TICKETS", "index_name": "IX", "index_type": "NORMAL", "uniqueness": "NONUNIQUE",
          "status": "VALID", "column_name": column, "position": i, "direction": "ASC"}
         for i, column in enumerate(("ORDER_ID", "CUSTOMER_ID"), 1)],
        [{"table_name": "TICKETS", "constraint_name": "FK", "status": "ENABLED", "validated": "VALIDATED",
          "delete_rule": "NO ACTION", "referenced_table": "ORDERS", "position": i,
          "column_name": column, "referenced_column": target}
         for i, (column, target) in enumerate((("ORDER_ID", "ID"), ("CUSTOMER_ID", "CUSTOMER_ID")), 1)],
    ]

    def execute(sql, binds=None):
        if binds is not None:
            rows = results.pop(0)
            cursor.description = [(name,) for name in rows[0]]
            cursor.fetchmany.return_value = [tuple(row.values()) for row in rows]
            assert binds == {"owner": "SUPPORT_OWNER", "t0": "CUSTOMERS", "t1": "ORDERS", "t2": "TICKETS"}
            assert "IN (:t0, :t1, :t2)" in sql and "= :owner" in sql
    cursor.execute.side_effect = execute
    monkeypatch.setattr(oracledb, "connect", connect)
    result = OracleMetadataLoader(settings()).load()
    assert result["ORDERS"]["columns"][0]["nullable"] is False
    assert [c["column"] for c in result["TICKETS"]["constraints"][0]["columns"]] == ["ORDER_ID", "CUSTOMER_ID"]
    assert [c["column"] for c in result["TICKETS"]["indexes"][0]["columns"]] == ["ORDER_ID", "CUSTOMER_ID"]
    assert result["TICKETS"]["relationships"][0]["column_pairs"][1]["referenced_column"] == "CUSTOMER_ID"
    assert "pc.position = fc.position" in cursor.execute.call_args.args[0]
    assert "p.owner = :owner" in cursor.execute.call_args.args[0]
    assert "p.table_name IN" in cursor.execute.call_args.args[0]
    assert conn.call_timeout == 5000
    conn.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()


def test_loader_failure_closes_connection_and_bounds_fetch(monkeypatch):
    connect = MagicMock()
    conn = connect.return_value.__enter__.return_value
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.description = [("TABLE_NAME",)]
    cursor.fetchmany.return_value = [("ORDERS",)] * (MAX_METADATA_ROWS + 1)
    monkeypatch.setattr(oracledb, "connect", connect)
    with pytest.raises(SchemaError):
        SchemaCatalog(settings()).list_tables()
    cursor.fetchmany.assert_called_once_with(MAX_METADATA_ROWS + 1)
    connect.return_value.__exit__.assert_called_once()
