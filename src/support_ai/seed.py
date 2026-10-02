"""Deterministic, fictional demo records. Run with python -m support_ai.seed."""

import argparse
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from support_ai.config import Settings, configure_logging
from support_ai.database import connection, initialize_database

logger = logging.getLogger(__name__)
NAMES = (
    "Emma Wilson", "Emma Chen", "Liam Martin", "Olivia Patel", "Noah Brown",
    "Ava Garcia", "Elijah Davis", "Sophia Kim", "James Taylor", "Isabella Lee",
    "Lucas Nguyen", "Mia Anderson", "Mason Thomas", "Amelia Moore", "Ethan Clark",
    "Harper Lewis", "Logan Walker", "Evelyn Hall", "Aiden Allen", "Abigail Young",
    "Henry King", "Ella Wright", "Benjamin Scott", "Scarlett Green", "Daniel Adams",
)


def seed_database(path: Path) -> bool:
    """Seed an empty schema atomically; preserve all existing data on repeat runs."""
    initialize_database(path)
    with connection(path, readonly=False) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
               for table in ("customers", "orders", "tickets")):
            logger.info("Database contains records; seed skipped without changing data")
            return False
        for customer_id, name in enumerate(NAMES, start=1):
            conn.execute("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", (
                customer_id, name, name.lower().replace(" ", ".") + "@example.com",
                (date(2024, 1, 1) + timedelta(days=customer_id * 3)).isoformat(),
                ("basic", "pro", "enterprise")[(customer_id - 1) % 3],
            ))
            for index in range(3):
                order_id = customer_id * 100 + index + 1
                placed = date(2025, 1, 10) + timedelta(days=customer_id + index * 60)
                conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    order_id, customer_id, placed.isoformat(),
                    ("USB-C Dock", "Wireless Keyboard", "Monitor")[index],
                    (12900, 7900, 29900)[index], "USD",
                    ("delivered", "refunded", "shipped")[index],
                ))
                conn.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)", (
                    order_id, customer_id, order_id, (placed + timedelta(days=5)).isoformat(),
                    ("Dock setup", "Keyboard return", "Delivery tracking")[index],
                    ("Requested help connecting a second display.",
                     "Reported a faulty key and requested a refund.",
                     "Requested the latest delivery status.")[index],
                    ("closed", "closed", "open")[index],
                ))
    logger.info("Seeded 25 fictional customers, 75 orders, and 75 tickets")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Override SUPPORT_DB_PATH")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        configure_logging(settings.log_level)
        seed_database(args.db or settings.db_path)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Database setup failed ({type(exc).__name__}); check path and configuration.\n")


if __name__ == "__main__":
    main()
