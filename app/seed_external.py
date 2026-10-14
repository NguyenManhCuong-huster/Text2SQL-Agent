"""Copy the 'shop' sample database into PostgreSQL or MySQL (for demos and integration tests).

    python -m app.seed_external postgresql://postgres:pass@localhost:5432/shopdb
    python -m app.seed_external mysql://root:pass@localhost:3306/shopdb

Existing shop tables in the target database are dropped and recreated.
"""
import sqlite3
import sys
from urllib.parse import unquote, urlparse

from . import config, sample_data

TABLE_ORDER = ["categories", "products", "customers", "employees", "orders", "order_items", "payments", "reviews"]
TIMESTAMP_COLS = {"order_date", "paid_at", "created_at"}
DATE_COLS = {"birth_date", "signup_date", "hire_date"}


def _col_type(dialect: str, name: str, sqlite_type: str) -> str:
    if name in TIMESTAMP_COLS:
        return "TIMESTAMP" if dialect == "postgres" else "DATETIME"
    if name in DATE_COLS:
        return "DATE"
    t = sqlite_type.upper()
    if t == "INTEGER":
        return "INTEGER"
    if t == "REAL":
        return "NUMERIC(12,2)" if name in {"unit_price", "unit_cost", "amount", "shipping_fee", "salary"} else "DOUBLE PRECISION" if dialect == "postgres" else "DOUBLE"
    return "TEXT" if dialect == "postgres" else "VARCHAR(255)"


def _connect(url: str):
    u = urlparse(url)
    args = dict(host=u.hostname or "localhost", user=unquote(u.username or ""), password=unquote(u.password or ""))
    db = u.path.lstrip("/")
    if u.scheme.startswith("postgres"):
        import psycopg
        return "postgres", psycopg.connect(port=u.port or 5432, dbname=db, autocommit=False, **args)
    if u.scheme.startswith("mysql"):
        import pymysql
        return "mysql", pymysql.connect(port=u.port or 3306, database=db, charset="utf8mb4", **args)
    raise SystemExit(f"Unsupported URL scheme: {u.scheme}")


def seed(url: str) -> None:
    sample_data.ensure_sample()
    src = sqlite3.connect(config.SOURCES_DIR / "shop.sqlite")
    dialect, dst = _connect(url)
    q = (lambda n: f'"{n}"') if dialect == "postgres" else (lambda n: f"`{n}`")
    cur = dst.cursor()
    for table in reversed(TABLE_ORDER):
        cur.execute(f"DROP TABLE IF EXISTS {q(table)}" + (" CASCADE" if dialect == "postgres" else ""))
    for table in TABLE_ORDER:
        cols = src.execute(f"PRAGMA table_info({table})").fetchall()
        fks = {f[3]: (f[2], f[4]) for f in src.execute(f"PRAGMA foreign_key_list({table})")}
        defs = []
        for _, name, typ, notnull, _, pk in cols:
            d = f"{q(name)} {_col_type(dialect, name, typ)}"
            d += " PRIMARY KEY" if pk else (" NOT NULL" if notnull else "")
            defs.append(d)
        for col, (rt, rc) in fks.items():
            defs.append(f"FOREIGN KEY ({q(col)}) REFERENCES {q(rt)}({q(rc)})")
        cur.execute(f"CREATE TABLE {q(table)} ({', '.join(defs)})")
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        marks = ", ".join(["%s"] * len(cols))
        for i in range(0, len(rows), 2000):
            cur.executemany(f"INSERT INTO {q(table)} VALUES ({marks})", rows[i:i + 2000])
        print(f"  {table}: {len(rows)} rows")
    if dialect == "postgres":
        cur.execute("ANALYZE")
    dst.commit()
    dst.close()
    src.close()
    print(f"Seeded shop sample into {dialect}.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    seed(sys.argv[1])
