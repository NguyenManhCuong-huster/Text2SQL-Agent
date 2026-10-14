import sqlite3

import pytest

from app import config, datasources


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE products SET unit_price = 0",
    "DROP TABLE orders",
    "INSERT INTO categories VALUES (99, 'x', 'y')",
    "WITH x AS (SELECT 1) INSERT INTO categories VALUES (99, 'x', 'y')",
    "ATTACH DATABASE 'evil.db' AS evil",
    "PRAGMA writable_schema = ON",
    "SELECT 1; DROP TABLE orders",
    "SELECT load_extension('evil')",
    "  /* comment */ DELETE FROM orders",
    "CREATE TABLE t (x)",
    "VACUUM",
])
def test_rejects_unsafe_sql(sql):
    with pytest.raises(datasources.QueryError):
        datasources.execute("shop", sql)
    # data is untouched
    assert datasources.execute("shop", "SELECT COUNT(*) FROM orders")["rows"][0][0] == 6000


def test_select_and_cte_allowed():
    r = datasources.execute("shop", "WITH t AS (SELECT name FROM categories) SELECT COUNT(*) AS n FROM t;")
    assert r["columns"] == ["n"] and r["rows"] == [[6]]


def test_recursive_cte_allowed():
    r = datasources.execute("shop", "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r WHERE x < 5) SELECT SUM(x) FROM r")
    assert r["rows"] == [[15]]


def test_timeout(monkeypatch):
    monkeypatch.setattr(config, "QUERY_TIMEOUT_S", 0.3)
    with pytest.raises(datasources.QueryError, match="time limit"):
        datasources.execute("shop", "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM r) SELECT COUNT(*) FROM r")


def test_row_cap_and_truncation_flag():
    r = datasources.execute("shop", "SELECT * FROM orders", max_rows=50)
    assert r["row_count"] == 50 and r["truncated"] is True


def test_unknown_and_malicious_source_ids():
    for sid in ["nope", "../app", "..\\x","", "SHOP"]:
        with pytest.raises(datasources.QueryError):
            datasources.execute(sid, "SELECT 1")


def test_schema_introspection():
    schema = datasources.get_schema("shop")
    orders = next(t for t in schema["tables"] if t["name"] == "orders")
    cust = next(c for c in orders["columns"] if c["name"] == "customer_id")
    assert cust["fk"] == "customers.customer_id"
    assert orders["row_count"] == 6000
    assert set(orders["value_hints"]["status"]) == {"delivered", "shipped", "processing", "cancelled", "returned"}


def test_schema_prompt_table_filter():
    full = datasources.schema_prompt("shop")
    part = datasources.schema_prompt("shop", tables=["orders"])
    assert "CREATE TABLE reviews" in full
    assert "CREATE TABLE orders" in part and "CREATE TABLE reviews" not in part


def test_csv_import_infers_types_and_dedupes_headers():
    csv_a = b"name,score,score,joined\nAn,9.5,1,2024-01-01\nBinh,7,2,\n"
    sid = datasources.import_csv_files("Test Upload!", [("a.csv", csv_a), ("2b.csv", "city;population\nHuế;652572\n".encode())])
    try:
        assert sid == "test_upload"
        schema = {t["name"]: t for t in datasources.get_schema(sid)["tables"]}
        assert set(schema) == {"a", "t_2b"}
        types = {c["name"]: c["type"] for c in schema["a"]["columns"]}
        assert types == {"name": "TEXT", "score": "REAL", "score_2": "INTEGER", "joined": "TEXT"}
        assert datasources.execute(sid, "SELECT city FROM t_2b")["rows"] == [["Huế"]]
        # the sniffed ';' delimiter was honoured
        assert datasources.execute(sid, "SELECT population FROM t_2b")["rows"] == [[652572]]
    finally:
        datasources.delete_source(sid)


def test_sqlite_import_validates_header(tmp_path):
    with pytest.raises(ValueError):
        datasources.import_sqlite_file("bad", b"not a database at all")
    db = tmp_path / "ok.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    sid = datasources.import_sqlite_file("ok", db.read_bytes())
    try:
        assert datasources.execute(sid, "SELECT x FROM t")["rows"] == [[42]]
    finally:
        datasources.delete_source(sid)


def test_sample_cannot_be_deleted():
    with pytest.raises(PermissionError):
        datasources.delete_source("shop")
