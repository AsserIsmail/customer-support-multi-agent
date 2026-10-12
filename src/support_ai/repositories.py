"""Backend selection for fixed customer lookups; no implicit fallback."""

from typing import Protocol

from support_ai.config import Settings
from support_ai.database import CustomerRepository


class CustomerRecords(Protocol):
    def find_customers(self, query: str) -> list[dict]: ...
    def get_customer(self, customer_id: int) -> dict | None: ...
    def get_orders(self, customer_id: int, *, limit: int = 50, offset: int = 0) -> list[dict]: ...
    def get_tickets(self, customer_id: int, *, limit: int = 50, offset: int = 0) -> list[dict]: ...


def create_repository(settings: Settings) -> CustomerRecords:
    if settings.db_backend == "sqlite":
        return CustomerRepository(settings.db_path)
    if settings.db_backend == "oracle" and settings.oracle is not None:
        from support_ai.oracle_database import OracleCustomerRepository
        return OracleCustomerRepository(settings.oracle)
    raise ValueError("Invalid database backend configuration")
