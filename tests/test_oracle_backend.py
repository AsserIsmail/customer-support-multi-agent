from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock

import oracledb
import pytest

from support_ai.config import OracleSettings, Settings
from support_ai.database import CustomerRepository, DataAccessError
from support_ai.oracle_database import OracleCustomerRepository
from support_ai.repositories import create_repository
from support_ai.web_runtime import Runtime


def oracle_settings():
    return OracleSettings("reader", "secret-password", "private-host/FREEPDB1", "SUPPORT_OWNER")


def configure_oracle(monkeypatch):
    for key, value in {"SUPPORT_DB_BACKEND": "oracle", "SUPPORT_ORACLE_USER": "reader",
                       "SUPPORT_ORACLE_PASSWORD": "secret-password",
                       "SUPPORT_ORACLE_DSN": "private-host/FREEPDB1"}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def driver(monkeypatch):
    connect = MagicMock()
    conn = connect.return_value.__enter__.return_value
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.description = [("ID",), ("JOINED_ON",), ("NAME",)]
    cursor.fetchall.return_value = [(Decimal("1"), datetime(2024, 1, 4), "Emma Wilson")]
    monkeypatch.setattr(oracledb, "connect", connect)
    return connect, conn, cursor


def test_backend_selection_is_explicit_and_lazy(monkeypatch, tmp_path, driver):
    assert isinstance(create_repository(Settings(tmp_path / "support.db")), CustomerRepository)
    configure_oracle(monkeypatch)
    settings = Settings.from_env()
    assert isinstance(create_repository(settings), OracleCustomerRepository)
    driver[0].assert_not_called()
    assert all(secret not in repr(settings) for secret in ("secret-password", "private-host", "reader"))
    monkeypatch.setenv("SUPPORT_DB_BACKEND", "typo")
    with pytest.raises(ValueError, match="sqlite or oracle"):
        Settings.from_env()
    with pytest.raises(ValueError):
        create_repository(Settings(tmp_path / "support.db", db_backend="oracle"))


@pytest.mark.parametrize("key,value", [
    ("SUPPORT_ORACLE_PASSWORD", ""), ("SUPPORT_ORACLE_DSN", ""), ("SUPPORT_ORACLE_USER", ""),
    ("SUPPORT_ORACLE_SCHEMA", "OWNER; DROP TABLE customers"),
    ("SUPPORT_ORACLE_CONNECT_TIMEOUT_SECONDS", "0"), ("SUPPORT_ORACLE_CALL_TIMEOUT_MS", "120001"),
    ("SUPPORT_ORACLE_CALL_TIMEOUT_MS", "private-invalid-value"),
])
def test_invalid_configuration_fails_without_echoing_values(monkeypatch, key, value):
    configure_oracle(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert "private-invalid-value" not in str(error.value)


def test_sqlite_ignores_unused_oracle_settings(monkeypatch):
    monkeypatch.setenv("SUPPORT_ORACLE_CALL_TIMEOUT_MS", "invalid")
    assert Settings.from_env().oracle is None


def test_oracle_binds_converts_and_closes(driver):
    connect, conn, cursor = driver
    repo = OracleCustomerRepository(oracle_settings())
    assert repo.get_customer(1) == {"id": 1, "joined_on": "2024-01-04", "name": "Emma Wilson"}
    assert conn.call_timeout == 5000
    assert connect.call_args.kwargs["tcp_connect_timeout"] == 5
    assert cursor.execute.call_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    sql, binds = cursor.execute.call_args.args
    assert "SUPPORT_OWNER.customers" in sql and binds == {"customer_id": 1}
    conn.cursor.return_value.__exit__.assert_called_once()
    connect.return_value.__exit__.assert_called_once()
    cursor.fetchall.return_value = []
    assert repo.get_customer(999) is None


def test_search_is_literal_and_history_uses_oracle_pagination(driver):
    repo = OracleCustomerRepository(oracle_settings())
    cursor = driver[2]
    repo.find_customers("%_' OR 1=1")
    sql, binds = cursor.execute.call_args.args
    assert "OR 1=1" not in sql
    assert binds == {"pattern": "%\\%\\_' OR 1=1%"}
    assert "LOWER(name)" in sql
    for method, table, date_column in ((repo.get_orders, "orders", "placed_on"),
                                       (repo.get_tickets, "tickets", "opened_on")):
        method(1, limit=2, offset=3)
        sql, binds = cursor.execute.call_args.args
        assert f"SUPPORT_OWNER.{table}" in sql
        assert f"ORDER BY {date_column} DESC, id DESC" in sql
        assert "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY" in sql
        assert binds == {"customer_id": 1, "limit": 2, "offset": 3}


@pytest.mark.parametrize("method,args,kwargs", [
    ("get_customer", (True,), {}), ("get_customer", (0,), {}),
    ("get_orders", (1,), {"limit": 101}), ("get_orders", (1,), {"limit": True}),
    ("get_tickets", (1,), {"offset": -1}), ("get_tickets", (1,), {"offset": "0"}),
    ("find_customers", (" ",), {}), ("find_customers", (None,), {}),
])
def test_invalid_lookup_never_connects(driver, method, args, kwargs):
    with pytest.raises(ValueError):
        getattr(OracleCustomerRepository(oracle_settings()), method)(*args, **kwargs)
    driver[0].assert_not_called()


@pytest.mark.parametrize("stage", ["connect", "execute", "fetch"])
def test_driver_failures_are_redacted_and_resources_closed(driver, caplog, stage):
    connect, conn, cursor = driver
    failure = oracledb.DatabaseError("secret-password private-host SELECT private-data")
    if stage == "connect":
        connect.side_effect = failure
    elif stage == "execute":
        cursor.execute.side_effect = failure
    else:
        cursor.fetchall.side_effect = failure
    with pytest.raises(DataAccessError) as error:
        OracleCustomerRepository(oracle_settings()).get_customer(1)
    assert not any(secret in str(error.value) + caplog.text for secret in
                   ("secret-password", "private-host", "private-data"))
    if stage != "connect":
        conn.cursor.return_value.__exit__.assert_called_once()
        connect.return_value.__exit__.assert_called_once()


def test_health_does_not_mistake_sqlite_file_for_oracle_readiness(monkeypatch, driver, tmp_path):
    path = tmp_path / "support.db"
    path.touch()
    monkeypatch.setenv("SUPPORT_DB_PATH", str(path))
    assert Runtime(None).health()["customer_database_present"] is True
    configure_oracle(monkeypatch)
    health = Runtime(None).health()
    assert health["customer_database_backend"] == "oracle"
    assert health["customer_database_present"] is None
    assert health["customer_database_configured"] is True
    driver[0].assert_not_called()


def test_mcp_constructs_selected_backend(monkeypatch):
    from support_ai import mcp_server
    configure_oracle(monkeypatch)
    factory = MagicMock(wraps=create_repository)
    monkeypatch.setattr(mcp_server, "create_repository", factory)
    mcp_server.create_server()
    assert factory.call_args.args[0].db_backend == "oracle"


def test_seed_refuses_implicit_sqlite_when_oracle_selected(monkeypatch, tmp_path):
    from support_ai.seed import main
    configure_oracle(monkeypatch)
    monkeypatch.setattr("sys.argv", ["support-seed"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert not (tmp_path / "data").exists()
