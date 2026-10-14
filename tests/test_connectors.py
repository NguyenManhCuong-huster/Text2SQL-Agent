"""Offline tests for the external-database guard; live tests run only when T2S_PG_URL / T2S_MYSQL_URL are set."""
import os
from urllib.parse import unquote, urlparse

import pytest

from app import connectors

ATTACKS = [
    "DELETE FROM orders",
    "UPDATE products SET unit_price = 0",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE orders",
    "TRUNCATE orders",
    "SELECT 1; DROP TABLE orders",
    "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
    "SELECT * INTO evil FROM orders",
    "SELECT * FROM orders FOR UPDATE",
    "SELECT pg_terminate_backend(1)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT set_config('default_transaction_read_only', 'off', false)",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "COPY orders TO '/tmp/x'",
    "SET SESSION TRANSACTION READ WRITE",
    "GRANT ALL ON orders TO public",
    "CREATE TABLE x (a int)",
]
MYSQL_ATTACKS = ["SELECT LOAD_FILE('/etc/passwd')", "SELECT * FROM orders INTO OUTFILE '/tmp/x'", "SELECT GET_LOCK('x', 10)"]
SAFE = [
    "SELECT 1",
    "WITH t AS (SELECT a FROM x) SELECT COUNT(*) FROM t",
    "SELECT a FROM x UNION ALL SELECT b FROM y ORDER BY 1",
    "SELECT date_trunc('month', d) AS m, SUM(v) FROM x GROUP BY 1",
    "SELECT name, RANK() OVER (PARTITION BY c ORDER BY v DESC) FROM x;",
]


@pytest.mark.parametrize("sql", ATTACKS)
def test_postgres_guard_blocks(sql):
    with pytest.raises(connectors.ConnectorError):
        connectors.validate_sql(sql, "postgres")


@pytest.mark.parametrize("sql", ATTACKS[:9] + MYSQL_ATTACKS)
def test_mysql_guard_blocks(sql):
    with pytest.raises(connectors.ConnectorError):
        connectors.validate_sql(sql, "mysql")


@pytest.mark.parametrize("sql", SAFE)
def test_guard_allows_reads(sql):
    assert connectors.validate_sql(sql, "postgres") == sql.rstrip(";")


def test_guard_allows_mysql_reads():
    sql = "SELECT DATE_FORMAT(order_date, '%Y-%m') AS m, COUNT(*) FROM `orders` GROUP BY m"
    assert connectors.validate_sql(sql, "mysql") == sql


def test_jsonable_conversions():
    import datetime as dt
    import decimal
    assert connectors._jsonable(decimal.Decimal("12.50")) == 12.5
    assert connectors._jsonable(decimal.Decimal("3")) == 3
    assert connectors._jsonable(dt.datetime(2025, 1, 2, 3, 4, 5)) == "2025-01-02 03:04:05"
    assert connectors._jsonable(dt.date(2025, 1, 2)) == "2025-01-02"
    assert connectors._jsonable(b"abc") == "<blob 3 bytes>"


def _info(env: str, dialect: str):
    url = os.environ.get(env)
    if not url:
        pytest.skip(f"{env} not set")
    u = urlparse(url)
    return connectors.ConnInfo(dialect, u.hostname, u.port, u.path.lstrip("/"), unquote(u.username or ""),
                               unquote(u.password or ""))


@pytest.mark.parametrize("env,dialect", [("T2S_PG_URL", "postgres"), ("T2S_MYSQL_URL", "mysql")])
def test_live_database(env, dialect, monkeypatch):
    """Expects the shop sample seeded via `python -m app.seed_external <url>`."""
    info = _info(env, dialect)
    meta = connectors.introspect(info)
    orders = next(t for t in meta["tables"] if t["name"] == "orders")
    assert orders["row_count"] == 6000 and "status" in orders["value_hints"]
    assert any(c.get("fk") == "customers.customer_id" for c in orders["columns"])
    r = connectors.execute(info, "SELECT COUNT(*) FROM orders WHERE status = 'delivered'", 10)
    assert r["rows"][0][0] > 0
    for sql in ATTACKS[:9]:
        with pytest.raises(connectors.ConnectorError):
            connectors.execute(info, sql, 10)
    assert connectors.execute(info, "SELECT COUNT(*) FROM orders", 10)["rows"] == [[6000]]
    from app import config
    monkeypatch.setattr(config, "QUERY_TIMEOUT_S", 1)
    heavy = "SELECT SUM(a.quantity * b.quantity) FROM order_items a CROSS JOIN order_items b WHERE a.unit_price + b.unit_price > 3"
    with pytest.raises(connectors.ConnectorError, match="time limit"):
        connectors.execute(info, heavy, 10)
