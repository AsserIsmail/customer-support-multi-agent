from copy import deepcopy
from datetime import datetime
from unittest.mock import MagicMock

import oracledb
import pytest

from support_ai.database import connection
from support_ai.oracle_migrate import MigrationError, canonical, copy_snapshot, read_source
from support_ai.oracle_setup import provision, quoted_password
from support_ai.seed import seed_database


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    seed_database(path)
    return path


def test_source_preserves_existing_values_and_bytes(source):
    with connection(source, readonly=False) as conn:
        conn.execute("UPDATE customers SET name = 'Custom Customer' WHERE id = 1")
        conn.execute("UPDATE tickets SET order_id = NULL, description = ? WHERE id = 101", ("x" * 5000,))
    before = source.read_bytes()
    snapshot = read_source(source)
    assert {table: len(rows) for table, rows in snapshot.items()} == {"customers": 25, "orders": 75, "tickets": 75}
    assert snapshot["customers"][0][1] == "Custom Customer"
    assert snapshot["tickets"][0][2] is None
    assert snapshot["tickets"][0][5] == "x" * 5000
    assert source.read_bytes() == before


@pytest.mark.parametrize("sql", [
    "UPDATE customers SET name = '' WHERE id = 1",
    "UPDATE customers SET joined_on = '2024-99-99' WHERE id = 1",
    "UPDATE customers SET joined_on = '20240104' WHERE id = 1",
    "UPDATE orders SET amount_cents = 1.5 WHERE id = 101",
])
def test_lossy_source_is_rejected(source, sql):
    with connection(source, readonly=False) as conn:
        conn.execute(sql)
    with pytest.raises(ValueError):
        read_source(source)


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(Exception):
        read_source(path)
    assert not path.exists()


def fake_connection(monkeypatch, snapshots):
    from support_ai import oracle_migrate
    conn = MagicMock()
    snapshot = MagicMock(side_effect=snapshots)
    monkeypatch.setattr(oracle_migrate, "target_snapshot", snapshot)
    return conn, conn.cursor.return_value.__enter__.return_value


def test_copy_verifies_every_row_before_commit(monkeypatch, source):
    data = read_source(source)
    conn, cursor = fake_connection(monkeypatch, [{key: [] for key in data}, data])
    assert copy_snapshot(conn, data) == "migrated"
    assert cursor.executemany.call_count == 3
    sql, rows = cursor.executemany.call_args_list[0].args
    assert ":1" in sql and "Emma" not in sql
    assert rows[0][3].isoformat() == "2024-01-04"
    assert "EXCLUSIVE MODE NOWAIT" in cursor.execute.call_args.args[0]
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_identical_rerun_is_noop(monkeypatch, source):
    data = read_source(source)
    conn, cursor = fake_connection(monkeypatch, [data])
    assert copy_snapshot(conn, data) == "unchanged"
    cursor.executemany.assert_not_called()
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_conflicting_target_never_written(monkeypatch, source):
    data = read_source(source)
    different = deepcopy(data)
    different["tickets"] = []
    conn, cursor = fake_connection(monkeypatch, [different])
    with pytest.raises(MigrationError, match="different"):
        copy_snapshot(conn, data)
    cursor.executemany.assert_not_called()
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.parametrize("failure", ["insert", "verify", "lock"])
def test_failure_rolls_back_all_tables(monkeypatch, source, failure):
    data = read_source(source)
    empty = {key: [] for key in data}
    conn, cursor = fake_connection(monkeypatch, [empty, empty])
    if failure == "insert":
        cursor.executemany.side_effect = [None, oracledb.DatabaseError("failure")]
    elif failure == "lock":
        cursor.execute.side_effect = oracledb.DatabaseError("busy")
    with pytest.raises((MigrationError, oracledb.Error)):
        copy_snapshot(conn, data)
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_target_comparison_does_not_hide_time():
    assert canonical(datetime(2024, 1, 1)) == "2024-01-01"
    with pytest.raises(MigrationError):
        canonical(datetime(2024, 1, 1, 1))


@pytest.mark.parametrize("password", ['short', 'long-password"; DROP USER x', 'password-with\nnewline'])
def test_unsafe_password_token_rejected(password):
    with pytest.raises(ValueError):
        quoted_password(password)


def test_setup_refuses_existing_accounts(monkeypatch):
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("SUPPORT_OWNER",)]
    monkeypatch.setattr(oracledb, "connect", connect)
    with pytest.raises(ValueError, match="already exist"):
        provision("dsn", "admin-password", "owner-password", "reader-password")
    assert cursor.execute.call_count == 1


def test_setup_grants_reader_only_session_and_table_select(monkeypatch):
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    monkeypatch.setattr(oracledb, "connect", connect)
    provision("dsn", "admin-password", "owner-password", "reader-password")
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    grants = [sql for sql in statements if sql.startswith("GRANT") and sql.endswith("TO SUPPORT_READER")]
    assert grants == ["GRANT CREATE SESSION TO SUPPORT_READER",
                      "GRANT SELECT ON customers TO SUPPORT_READER",
                      "GRANT SELECT ON orders TO SUPPORT_READER",
                      "GRANT SELECT ON tickets TO SUPPORT_READER"]
    assert any("FOREIGN KEY (order_id, customer_id)" in sql for sql in statements)
    assert not any("DROP " in sql or "ALTER USER" in sql for sql in statements)
