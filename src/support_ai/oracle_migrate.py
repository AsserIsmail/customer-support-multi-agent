"""Copy an existing SQLite snapshot into an empty Oracle support schema."""

import argparse
from datetime import date, datetime, time
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from dotenv import load_dotenv
import oracledb

from support_ai.database import connection
from support_ai.oracle_database import _COLUMNS
from support_ai.oracle_setup import OWNER, required

DATES = {"joined_on", "placed_on", "opened_on"}
INTEGERS = {"id", "customer_id", "order_id", "amount_cents"}


class MigrationError(ValueError):
    pass


def read_source(path: Path) -> dict[str, list[tuple]]:
    with connection(path) as conn:
        conn.execute("BEGIN")  # Keep all three tables in one consistent read snapshot.
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("Source integrity check failed")
        result = {}
        for table, columns in _COLUMNS.items():
            rows = [tuple(row) for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY id")]
            for row in rows:
                for column, value in zip(columns.split(", "), row):
                    if value is None and column == "order_id":
                        continue
                    if column in INTEGERS:
                        if type(value) is not int:
                            raise MigrationError("Source has a non-integer numeric value")
                    elif not isinstance(value, str) or value == "":
                        # Oracle maps empty strings to NULL; refuse lossy conversion.
                        raise MigrationError("Source has null or empty text that Oracle cannot preserve")
                    elif column in DATES:
                        if date.fromisoformat(value).isoformat() != value:
                            raise MigrationError("Source dates must use YYYY-MM-DD")
                    elif column != "description" and len(value) > 1000:
                        raise MigrationError("Source text exceeds the Oracle column capacity")
            result[table] = rows
        return result


def canonical(value):
    if isinstance(value, oracledb.LOB):
        return value.read()
    if isinstance(value, datetime):
        if value.time() != time.min:
            raise MigrationError("Target date includes a time; refusing lossy comparison")
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise MigrationError("Target numeric value is not integral")
        return int(value)
    return value


def target_snapshot(cursor) -> dict[str, list[tuple]]:
    result = {}
    for table, columns in _COLUMNS.items():
        cursor.execute(f"SELECT {columns} FROM {table} ORDER BY id")
        result[table] = [tuple(canonical(value) for value in row) for row in cursor.fetchall()]
    return result


def copy_snapshot(conn, source: dict[str, list[tuple]]) -> str:
    """Only DML here: verify before commit, rollback any conflict or failure."""
    try:
        with conn.cursor() as cursor:
            cursor.execute("LOCK TABLE customers, orders, tickets IN EXCLUSIVE MODE NOWAIT")
            before = target_snapshot(cursor)
            if before == source:
                conn.rollback()
                return "unchanged"
            if any(before.values()):
                raise MigrationError("Target contains different records; no overwrite is allowed")
            for table, columns in _COLUMNS.items():
                names = columns.split(", ")
                rows = [tuple(date.fromisoformat(value) if name in DATES else value
                              for name, value in zip(names, row)) for row in source[table]]
                if rows:
                    # Explicit CLOB typing also supports descriptions over 4000 bytes.
                    cursor.setinputsizes(*[oracledb.DB_TYPE_CLOB if name == "description" else
                                           oracledb.DB_TYPE_DATE if name in DATES else
                                           oracledb.DB_TYPE_NUMBER if name in INTEGERS else
                                           oracledb.DB_TYPE_VARCHAR for name in names])
                    binds = ", ".join(f":{i + 1}" for i in range(len(names)))
                    cursor.executemany(f"INSERT INTO {table} ({columns}) VALUES ({binds})", rows)
            if target_snapshot(cursor) != source:
                raise MigrationError("Verification failed; copied values do not match the source")
        conn.commit()
        return "migrated"
    except Exception:
        conn.rollback()
        raise


def migrate(source_path: Path, dsn: str, owner_password: str) -> dict:
    source = read_source(source_path)
    with oracledb.connect(user=OWNER, password=owner_password, dsn=dsn, tcp_connect_timeout=5) as conn:
        conn.call_timeout = 10000
        status = copy_snapshot(conn, source)
    return {"status": status, "counts": {table: len(rows) for table, rows in source.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing SQLite database (opened read-only)")
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        result = migrate(args.source, required("SUPPORT_ORACLE_DSN"), required("SUPPORT_ORACLE_OWNER_PASSWORD"))
    except (ValueError, OSError, sqlite3.Error, oracledb.Error):
        parser.exit(1, "Migration failed; no source records were changed. Check source integrity, "
                    "Oracle setup, credentials, and conflicting target records.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
