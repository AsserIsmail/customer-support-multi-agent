import pytest

from support_ai.config import OracleSettings
from support_ai.sql_validation import QueryError, validate_query, qualify_query

SETTINGS = OracleSettings("reader", "private-password", "private-host/service", "SUPPORT_OWNER")


@pytest.mark.parametrize("sql,parameters", [
    ("SELECT id, status FROM orders WHERE customer_id=:customer_id ORDER BY placed_on DESC", {"customer_id": 1}),
    ("SELECT c.id, SUM(o.amount_cents) total FROM customers c JOIN orders o ON c.id=o.customer_id GROUP BY c.id HAVING COUNT(*) > 1", {}),
    ("WITH recent AS (SELECT * FROM orders WHERE amount_cents > :minimum) SELECT id FROM recent", {"minimum": 10}),
    ("SELECT * FROM orders WHERE id IN (SELECT order_id FROM tickets WHERE status=:status)", {"status": "open"}),
    ("SELECT * FROM SUPPORT_OWNER.orders WHERE id=:ID", {"id": 1}),
    ("SELECT CASE WHEN id IS NULL THEN 0 ELSE id END FROM orders", {}),
    ("SELECT COUNT(DISTINCT customer_id), AVG(amount_cents), MIN(id), MAX(id) FROM orders", {}),
    ("SELECT NVL(status, 'missing'), LOWER(status), LENGTH(status) FROM orders", {}),
    ("SELECT * FROM orders OFFSET 2 ROWS FETCH NEXT 5 ROWS ONLY", {}),
    ("SELECT * FROM customers WHERE name LIKE :name ESCAPE '\\'", {"name": "%Emma%"}),
    ("SELECT DATE '2025-01-01', ABS(amount_cents), ROUND(amount_cents/100,2) FROM orders", {}),
    ("SELECT id FROM orders -- harmless comment\n WHERE id=:id", {"id": 1}),
    ("SELECT * FROM orders WHERE id=:id", {"id": "1 OR 1=1"}),
])
def test_supported_queries_are_qualified_and_bound(sql, parameters):
    result = validate_query(sql, parameters, SETTINGS)
    assert '"SUPPORT_OWNER".' in result.sql
    assert result.parameters == parameters
    assert set(result.tables) <= {"ORDERS", "CUSTOMERS", "TICKETS"}
    assert "harmless comment" not in result.sql


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders", "UPDATE orders SET status='shipped'", "DROP TABLE orders", "TRUNCATE TABLE orders",
    "INSERT INTO orders(id) VALUES (1)", "MERGE INTO orders USING tickets ON (orders.id=tickets.id) WHEN MATCHED THEN DELETE",
    "BEGIN NULL; END;", "CALL dbms_session.sleep(1)", "SELECT * FROM orders; DELETE FROM orders",
    "SELECT * FROM orders; SELECT * FROM tickets", "SELECT * FROM orders;;",
    "SELECT * FROM orders FOR UPDATE", "SELECT * FROM orders FOR UPDATE SKIP LOCKED",
    "SELECT * INTO stolen FROM orders", "SELECT * FROM SYS.USER$", "SELECT * FROM all_users", "SELECT * FROM dual",
    "SELECT * FROM OTHER.orders", 'SELECT * FROM "orders"', "SELECT * FROM orders@remote",
    "SELECT * FROM orders AS OF TIMESTAMP SYSDATE", "SELECT /*+ PARALLEL(orders,100) */ * FROM orders",
    "SELECT UTL_HTTP.REQUEST('https://example.com') FROM orders",
    "SELECT DBMS_XMLGEN.GETXML('SELECT * FROM SYS.USER$') FROM orders",
    "SELECT evil_function(id) FROM orders", "SELECT SUPPORT_OWNER.evil_function(id) FROM orders",
    "SELECT dbms_random.value FROM orders", "SELECT some_sequence.NEXTVAL FROM orders",
    "SELECT SYS_CONTEXT('USERENV','SESSION_USER') FROM orders", "SELECT USER FROM orders",
    "SELECT * FROM TABLE(evil_function())", "SELECT * FROM orders NATURAL JOIN customers",
    "SELECT * FROM orders CROSS JOIN customers", "SELECT * FROM orders, customers",
    "SELECT * FROM orders CONNECT BY LEVEL < 10000",
    "WITH RECURSIVE x AS (SELECT * FROM orders) SELECT * FROM x",
    "WITH orders AS (SELECT * FROM secrets) SELECT * FROM orders",
    "WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x",
    "SELECT * FROM orders UNION SELECT * FROM secrets",
    "SELECT * FROM orders WHERE EXISTS (SELECT * FROM secrets)",
    "SELECT * FROM orders FETCH FIRST 50 PERCENT ROWS ONLY",
    "SELECT * FROM orders FETCH FIRST 1 ROW WITH TIES",
    "SELECT 1", "", "x" * 12001,
])
def test_unsafe_or_unsupported_sql_fails_closed(sql, caplog):
    with pytest.raises(QueryError) as error:
        validate_query(sql, {}, SETTINGS)
    assert sql not in str(error.value) if sql else True
    assert "https://example.com" not in caplog.text


@pytest.mark.parametrize("parameters", [{}, {"id": 1, "extra": 2}, {"ID": 1}, {"id": True},
                                       {"id": []}, {"id": float("nan")}, {"id": float("inf")},
                                       {"id": 2**100}, {"id": "x" * 4001}, {"bad-name": 1}])
def test_bind_parameters_are_exact_and_bounded(parameters):
    with pytest.raises(QueryError) as error:
        validate_query("SELECT * FROM orders WHERE id=:id", parameters, SETTINGS)
    assert error.value.code == "invalid_parameters"


def test_cte_shadowing_does_not_bypass_object_validation():
    result = validate_query("WITH orders AS (SELECT id FROM tickets) SELECT * FROM orders", {}, SETTINGS)
    assert result.tables == ("TICKETS",)
    assert '"SUPPORT_OWNER"."TICKETS"' in result.sql
    assert '"SUPPORT_OWNER"."ORDERS"' not in result.sql


def test_parameter_injection_remains_data():
    value = "'; DROP TABLE orders; --"
    result = validate_query("SELECT * FROM customers WHERE name=:name", {"name": value}, SETTINGS)
    assert value not in result.sql
    assert result.parameters["name"] == value


@pytest.mark.parametrize("sql", ["SELECT evil_package.secret FROM orders", "SELECT unknown_function FROM orders",
                                "SELECT missing_column FROM orders", "SELECT id FROM orders o JOIN customers c ON o.id=c.id"])
def test_column_resolution_rejects_functions_unknown_and_ambiguous_columns(sql):
    query = validate_query(sql, {}, SETTINGS)
    columns = {table: {"ID": "NUMBER"} for table in query.tables}
    with pytest.raises(QueryError):
        qualify_query(query, "SUPPORT_OWNER", columns)


def test_column_resolution_expands_stars_and_resolves_ctes():
    query = validate_query("WITH x AS (SELECT id FROM orders) SELECT * FROM x", {}, SETTINGS)
    sql = qualify_query(query, "SUPPORT_OWNER", {"ORDERS": {"ID": "NUMBER"}})
    assert '"X"."ID"' in sql and "*" not in sql
