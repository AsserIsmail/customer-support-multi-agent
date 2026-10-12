"""One-time provisioning for a fresh local Oracle Free PDB. Never resets users."""

import argparse
from importlib.resources import files
import os
from pathlib import Path

from dotenv import load_dotenv
import oracledb

OWNER = "SUPPORT_OWNER"
READER = "SUPPORT_READER"


def required(name: str) -> str:
    value = os.getenv(name, "")
    if not value.strip():
        raise ValueError(f"Set {name}")
    return value


def quoted_password(value: str) -> str:
    # Oracle CREATE USER does not support password bind variables. Restrict the
    # delimited password token; never echo either SQL or its driver exception.
    if not 12 <= len(value) <= 30 or any(c in value for c in ('"', '\x00', '\r', '\n')):
        raise ValueError("Setup passwords must be 12..30 characters without quotes or line breaks")
    return '"' + value + '"'


def provision(dsn: str, admin_password: str, owner_password: str, reader_password: str) -> None:
    owner_token, reader_token = quoted_password(owner_password), quoted_password(reader_password)
    if len({admin_password, owner_password, reader_password}) != 3:
        raise ValueError("Admin, owner, and reader passwords must be distinct")
    with oracledb.connect(user="system", password=admin_password, dsn=dsn, tcp_connect_timeout=5) as admin:
        admin.call_timeout = 10000
        with admin.cursor() as cursor:
            cursor.execute("SELECT username FROM all_users WHERE username IN (:owner, :reader)",
                           {"owner": OWNER, "reader": READER})
            if cursor.fetchall():
                raise ValueError("Support accounts already exist; setup will not modify them")
            cursor.execute(f"CREATE USER {OWNER} IDENTIFIED BY {owner_token} DEFAULT TABLESPACE users QUOTA 20M ON users")
            cursor.execute(f"GRANT CREATE SESSION, CREATE TABLE TO {OWNER}")
            cursor.execute(f"CREATE USER {READER} IDENTIFIED BY {reader_token}")
            cursor.execute(f"GRANT CREATE SESSION TO {READER}")
    with oracledb.connect(user=OWNER, password=owner_password, dsn=dsn, tcp_connect_timeout=5) as owner:
        owner.call_timeout = 10000
        with owner.cursor() as cursor:
            script = files("support_ai").joinpath("sql/oracle_schema.sql").read_text(encoding="utf-8")
            for statement in script.split(";"):
                if statement.strip():
                    cursor.execute(statement.strip())
            for table in ("customers", "orders", "tickets"):
                cursor.execute(f"GRANT SELECT ON {table} TO {READER}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        provision(required("SUPPORT_ORACLE_DSN"), required("SUPPORT_ORACLE_ADMIN_PASSWORD"),
                  required("SUPPORT_ORACLE_OWNER_PASSWORD"), required("SUPPORT_ORACLE_PASSWORD"))
    except (ValueError, oracledb.Error, OSError):
        parser.exit(1, "Oracle setup failed. Check configuration and account/schema state. "
                    "DDL may have partially completed; existing accounts are never reset.\n")
    print("Oracle support schema and reader grants created; migration can now run.")


if __name__ == "__main__":
    main()
