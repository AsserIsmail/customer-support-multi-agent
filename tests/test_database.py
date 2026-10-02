import sqlite3

import pytest

from support_ai.config import Settings
from support_ai.database import CustomerRepository, DataAccessError, connection
from support_ai.seed import seed_database
from support_ai import seed


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "records with spaces.db"
    assert seed_database(path)
    return path


def snapshot(path):
    with connection(path) as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("customers", "orders", "tickets")}


def test_seed_is_reproducible_and_non_destructive(database, tmp_path):
    before = snapshot(database)
    assert {table: len(rows) for table, rows in before.items()} == {
        "customers": 25, "orders": 75, "tickets": 75}
    other = tmp_path / "other.db"
    assert seed_database(other)
    assert snapshot(other) == before
    with connection(database, readonly=False) as conn:
        conn.execute("UPDATE customers SET plan = 'enterprise' WHERE id = 1")
    changed = snapshot(database)
    assert not seed_database(database)
    assert snapshot(database) == changed


def test_search_ambiguity_missing_and_history(database):
    repo = CustomerRepository(database)
    matches = repo.find_customers("emma")
    assert [row["name"] for row in matches] == ["Emma Wilson", "Emma Chen"]
    assert len(repo.find_customers("emma.wilson@example.com")) == 1
    assert repo.find_customers("Unknown Person") == []
    assert repo.get_customer(9999) is None
    assert repo.get_orders(9999) == []
    assert repo.get_customer(1)["name"] == "Emma Wilson"
    orders = repo.get_orders(1)
    assert len(orders) == len(repo.get_tickets(1)) == 3
    assert [row["placed_on"] for row in orders] == sorted(
        [row["placed_on"] for row in orders], reverse=True)
    assert repo.get_orders(1, limit=1, offset=1) == orders[1:2]
    assert all(row["customer_id"] == 1 for row in repo.get_tickets(1))


@pytest.mark.parametrize("query", ["' OR 1=1 --", "%", "_", "\\", "'; DROP TABLE customers;--"])
def test_search_input_is_literal(database, query):
    repo = CustomerRepository(database)
    assert repo.find_customers(query) == []
    assert repo.get_customer(1) is not None


@pytest.mark.parametrize("query", ["", "   ", "x" * 201, None])
def test_invalid_search(database, query):
    with pytest.raises(ValueError):
        CustomerRepository(database).find_customers(query)


@pytest.mark.parametrize("value", [0, -1, True, "1 OR 1=1", 1.5])
def test_invalid_ids(database, value):
    repo = CustomerRepository(database)
    for method in (repo.get_customer, repo.get_orders, repo.get_tickets):
        with pytest.raises(ValueError):
            method(value)


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True},
                                    {"offset": -1}, {"offset": "0"}])
def test_invalid_pagination(database, kwargs):
    with pytest.raises(ValueError):
        CustomerRepository(database).get_orders(1, **kwargs)


def test_runtime_connection_cannot_write_even_if_query_only_disabled(database):
    with connection(database) as conn:
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM customers")


def test_constraints_and_relationships(database):
    with connection(database, readonly=False) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE orders SET customer_id = 9999 WHERE id = 101")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tickets SET order_id = 201 WHERE id = 101")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE orders SET amount_cents = -1 WHERE id = 101")


def test_missing_database_is_error_not_empty_result(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(DataAccessError):
        CustomerRepository(path).find_customers("Emma")
    assert not path.exists()


def test_seed_failure_rolls_back_all_records(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    monkeypatch.setattr(seed, "NAMES", ("Emma Wilson", "Emma Wilson"))
    with pytest.raises(sqlite3.IntegrityError):
        seed_database(path)
    assert snapshot(path) == {"customers": [], "orders": [], "tickets": []}


def test_corrupt_database_is_error(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_text("This is not a SQLite database", encoding="utf-8")
    with pytest.raises(DataAccessError):
        CustomerRepository(path).get_customer(1)


def test_environment_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORT_DB_PATH", str(tmp_path / "custom.db"))
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "debug")
    assert Settings.from_env() == Settings(tmp_path / "custom.db", "DEBUG")
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "invalid")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "INFO")
    monkeypatch.setenv("SUPPORT_DB_PATH", "  ")
    with pytest.raises(ValueError):
        Settings.from_env()
