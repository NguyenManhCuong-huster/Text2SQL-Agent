import pytest

from app import datasources, history, policy
from app.policy import Policy


@pytest.fixture
def hide(monkeypatch):
    def apply(*pats):
        history.save_source_settings("shop", {"hidden": list(pats)})
    yield apply
    history.save_source_settings("shop", {})


def test_schema_hides_columns_and_tables(hide):
    hide("customers.email", "employees")
    schema = datasources.get_schema("shop")
    names = {t["name"] for t in schema["tables"]}
    assert "employees" not in names and schema["hidden_tables"] == 1
    cust = next(t for t in schema["tables"] if t["name"] == "customers")
    assert "email" not in [c["name"] for c in cust["columns"]] and cust["hidden_columns"] == 1
    assert "email" not in cust["ddl"] and all(len(r) == len(cust["columns"]) for r in cust["samples"])
    orders = next(t for t in schema["tables"] if t["name"] == "orders")
    assert all(f["ref_table"] != "employees" for f in orders["foreign_keys"])
    assert "employees" not in datasources.schema_prompt("shop") and "@" not in datasources.schema_prompt("shop")
    # the raw schema still has everything (used for enforcement)
    assert "employees" in {t["name"] for t in datasources.raw_schema("shop")["tables"]}


@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers",
    "SELECT c.email FROM customers c",
    "SELECT * FROM customers",
    "SELECT x.* FROM (SELECT * FROM customers) x",
    "WITH c AS (SELECT email AS e FROM customers) SELECT e FROM c",
    "SELECT COUNT(*) FROM employees",
    "SELECT o.order_id FROM orders o JOIN employees e ON e.employee_id = o.employee_id",
    "SELECT lower(EMAIL) FROM customers WHERE email LIKE '%a%'",
])
def test_restricted_queries_are_blocked(hide, sql):
    hide("customers.email", "employees")
    with pytest.raises(datasources.QueryError, match="restricted|Not authorized"):
        datasources.execute("shop", sql)


@pytest.mark.parametrize("dialect", ["postgres", "mysql"])
@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers",
    "SELECT c.email FROM customers c",
    "SELECT * FROM customers",
    "SELECT x.* FROM (SELECT * FROM customers) x",
    "WITH c AS (SELECT email AS e FROM customers) SELECT e FROM c",
    "SELECT COUNT(*) FROM employees",
    "SELECT lower(EMAIL) FROM customers WHERE email LIKE '%a%'",
])
def test_static_check_for_external_engines(dialect, sql):
    """PostgreSQL/MySQL have no authorizer: the static check alone must catch these."""
    pol = Policy(["customers.email", "employees"])
    with pytest.raises(policy.PolicyError):
        policy.check_sql(sql, dialect, datasources.raw_schema("shop"), pol)
    policy.check_sql("SELECT full_name, city FROM customers", dialect, datasources.raw_schema("shop"), pol)


def test_allowed_queries_still_work(hide):
    hide("customers.email", "employees")
    assert datasources.execute("shop", "SELECT COUNT(*) FROM customers")["rows"] == [[500]]
    out = datasources.execute("shop", "SELECT c.full_name, o.order_id FROM customers c JOIN orders o "
                                      "ON o.customer_id = c.customer_id LIMIT 2")
    assert out["row_count"] == 2


def test_authorizer_catches_what_the_parser_cannot_see(hide):
    # a hidden column referenced only through a correlated subquery alias still fails at the engine
    hide("customers.email")
    pol = policy.for_source("shop")
    import sqlite3
    auth = datasources._make_authorizer(pol)
    assert auth(sqlite3.SQLITE_READ, "customers", "email", "main", None) == sqlite3.SQLITE_DENY
    assert auth(sqlite3.SQLITE_READ, "customers", "email", "main", "some_view") == sqlite3.SQLITE_IGNORE
    assert auth(sqlite3.SQLITE_READ, "customers", "city", "main", None) == sqlite3.SQLITE_OK


def test_glob_patterns():
    pol = Policy(["*.password*", "audit_*", "hr.*"])
    assert pol.column_hidden("users", "password_hash") and pol.column_hidden("USERS", "Password")
    assert pol.table_hidden("audit_log") and pol.table_hidden("hr") and not pol.table_hidden("users")
    assert not pol.column_hidden("users", "name")


def test_settings_api(hide):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.put("/api/sources/shop/settings", json={"hidden": ["customers.email", " "], "notes": "n"})
        assert r.json()["hidden"] == ["customers.email"] and r.json()["notes"] == "n"
        assert client.get("/api/sources/shop/settings").json()["hidden"] == ["customers.email"]
        assert client.put("/api/sources/shop/settings", json={"hidden": ["a.b.c"]}).status_code == 422
        r = client.post("/api/execute", json={"source_id": "shop", "sql": "SELECT email FROM customers"})
        assert r.status_code == 400 and "restricted" in r.json()["detail"]
