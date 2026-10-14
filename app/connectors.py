"""External databases (PostgreSQL, MySQL): connection, introspection and guarded read-only execution.

Defence in depth for every query:
  1. sqlglot parses the SQL in the target dialect: exactly one statement, a query at the root, and no
     DML/DDL, SELECT INTO, row locks or dangerous functions anywhere in the tree.
  2. The query runs inside a READ ONLY transaction that is always rolled back.
  3. A server-side statement timeout (statement_timeout / MAX_EXECUTION_TIME) and a row cap.
Still, connect with a database user that only has SELECT privileges.
"""
import datetime as dt
import decimal
import re
import socket
import time
import uuid
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from . import config

DIALECTS = {
    "postgres": {"label": "PostgreSQL", "port": 5432, "sqlglot": "postgres"},
    "mysql": {"label": "MySQL", "port": 3306, "sqlglot": "mysql"},
}

_BLOCKED_FUNCS = re.compile(
    r"^(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote|pg_read_file|"
    r"pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_file_\w+|lo_\w+|dblink\w*|set_config|pg_advisory\w*|"
    r"pg_try_advisory\w*|pg_switch_wal|pg_create_\w+|pg_drop_\w+|pg_logical_\w+|query_to_xml\w*|"
    r"load_file|get_lock|release_lock|release_all_locks|sys_exec|sys_eval|benchmark)$", re.I)

_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable", "Command", "Into",
    "Lock", "Set", "Use", "Grant", "Transaction", "Commit", "Rollback", "Copy", "LoadData", "TruncateTable",
) if hasattr(exp, n))

SCHEMA_TTL_S = 300
TEXT_TYPES = ("char", "text", "enum", "user-defined", "set", "citext")


class ConnectorError(Exception):
    pass


@dataclass
class ConnInfo:
    dialect: str
    host: str
    port: int
    database: str
    user: str
    password: str = ""
    db_schema: str = ""  # PostgreSQL schema (search_path); empty = public

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}" + (
            f" ({self.db_schema})" if self.db_schema and self.db_schema != "public" else "")


# ---------------------------------------------------------------- validation

def validate_sql(sql: str, dialect: str) -> str:
    """Return the cleaned single statement or raise ConnectorError."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ConnectorError("Empty query.")
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECTS[dialect]["sqlglot"]) if s is not None]
    except sqlglot.errors.ParseError as e:
        msg = str(e).splitlines()[0]
        raise ConnectorError(f"Could not parse the SQL ({msg}). Write a single standard SELECT query.")
    if len(statements) != 1:
        raise ConnectorError("Only a single statement is allowed.")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise ConnectorError("Only read-only SELECT / WITH queries are allowed.")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise ConnectorError(f"Not allowed in a read-only query: {node.key.upper()}.")
        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            if name and _BLOCKED_FUNCS.match(name):
                raise ConnectorError(f"Function {name}() is not allowed.")
    return sql


# ---------------------------------------------------------------- connections

def _pg_connect(c: ConnInfo, timeout_s: float):
    import psycopg
    options = f"-c default_transaction_read_only=on -c statement_timeout={int(timeout_s * 1000)}"
    if c.db_schema:
        options += f" -c search_path={c.db_schema}"
    # Resolve ourselves, IPv4 first: libpq otherwise reports only the first address's failure (often an
    # unreachable IPv6 address) and hides the real reason, such as a wrong password.
    try:
        infos = socket.getaddrinfo(c.host, c.port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ConnectorError(f"Cannot connect to PostgreSQL: unknown host '{c.host}'.")
    addrs = list(dict.fromkeys(i[4][0] for i in sorted(infos, key=lambda i: i[0] != socket.AF_INET)))
    errors = []
    for addr in addrs:
        try:
            conn = psycopg.connect(host=c.host, hostaddr=addr, port=c.port, dbname=c.database, user=c.user,
                                   password=c.password, connect_timeout=5, options=options,
                                   application_name="text2sql-studio")
            conn.read_only = True
            return conn
        except psycopg.Error as e:
            msg = re.sub(r'^connection (?:is bad|failed): |FATAL: +|'
                         r'connection to server at "[^"]*"(?: \([^)]*\))?, port \d+ failed: ',
                         "", str(e).strip()).splitlines()[0]
            errors.append(msg)
            if not re.search(r"unreachable|refused|timeout|timed out", msg, re.I):
                break  # definitive answer from the server (auth, unknown database, ...)
    best = next((m for m in reversed(errors) if not re.search(r"unreachable", m, re.I)), errors[-1])
    raise ConnectorError(f"Cannot connect to PostgreSQL: {best}")


def _my_connect(c: ConnInfo, timeout_s: float):
    import pymysql
    try:
        conn = pymysql.connect(host=c.host, port=c.port, database=c.database, user=c.user, password=c.password,
                               connect_timeout=5, read_timeout=int(timeout_s) + 5, charset="utf8mb4",
                               autocommit=False)
    except pymysql.MySQLError as e:
        raise ConnectorError(f"Cannot connect to MySQL: {e.args[-1] if e.args else e}")
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_s * 1000)}")
        cur.execute("START TRANSACTION READ ONLY")
    return conn


def _connect(c: ConnInfo, timeout_s: float | None = None):
    timeout_s = timeout_s or config.QUERY_TIMEOUT_S
    if c.dialect == "postgres":
        return _pg_connect(c, timeout_s)
    if c.dialect == "mysql":
        return _my_connect(c, timeout_s)
    raise ConnectorError(f"Unsupported database type '{c.dialect}'.")


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2**53 else float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob {len(bytes(v))} bytes>"
    if isinstance(v, uuid.UUID):
        return str(v)
    return str(v)


def _error_text(e: Exception) -> str:
    args = getattr(e, "args", ())
    if len(args) == 2 and isinstance(args[0], int):  # PyMySQL: (errno, message)
        text = str(args[1])
    else:
        text = (str(e).strip() or repr(e)).splitlines()[0]
    low = text.lower()
    if "statement timeout" in low or "maximum statement execution time" in low or "max_execution_time" in low:
        return f"Query exceeded the {config.QUERY_TIMEOUT_S:g}s time limit."
    if "read-only transaction" in low or "read only transaction" in low:
        return "Not authorized: only read-only queries are allowed."
    return text


def execute(c: ConnInfo, sql: str, max_rows: int) -> dict:
    try:
        sql = validate_sql(sql, c.dialect)
    except ConnectorError as e:
        raise ConnectorError(str(e))
    conn = _connect(c)
    start = time.perf_counter()
    try:
        cur = conn.cursor()
        if c.dialect == "mysql":
            cur.execute(sql)
        else:
            cur.execute(sql, prepare=True)  # extended protocol: the server also rejects multiple statements
        if cur.description is None:
            raise ConnectorError("Statement returned no result set.")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    except ConnectorError:
        raise
    except Exception as e:  # driver errors (syntax, missing column, timeout, ...)
        raise ConnectorError(_error_text(e))
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    elapsed = (time.perf_counter() - start) * 1000
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [[_jsonable(v) for v in r] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "duration_ms": round(elapsed, 1),
    }


# ---------------------------------------------------------------- introspection

def _q(dialect: str, name: str) -> str:
    return f"`{name.replace('`', '``')}`" if dialect == "mysql" else '"' + name.replace('"', '""') + '"'


def _pg_meta(cur, schema: str):
    cur.execute("SELECT c.relname, CASE WHEN c.relkind IN ('v','m') THEN 'view' ELSE 'table' END, c.reltuples::bigint "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m') ORDER BY 2, 1", (schema,))
    tables = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    cur.execute("SELECT table_name, column_name, "
                "CASE WHEN data_type = 'USER-DEFINED' THEN udt_name WHEN data_type = 'ARRAY' THEN udt_name "
                "WHEN character_maximum_length IS NOT NULL THEN data_type || '(' || character_maximum_length || ')' "
                "ELSE data_type END, is_nullable = 'NO' "
                "FROM information_schema.columns WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                (schema,))
    cols = cur.fetchall()
    key_sql = ("SELECT cl.relname, att.attname, clf.relname, attf.attname FROM pg_constraint con "
               "JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace ns ON ns.oid = cl.relnamespace "
               "LEFT JOIN pg_class clf ON clf.oid = con.confrelid "
               "CROSS JOIN LATERAL unnest(con.conkey, coalesce(con.confkey, con.conkey)) AS k(a, b) "
               "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = k.a "
               "LEFT JOIN pg_attribute attf ON attf.attrelid = con.confrelid AND attf.attnum = k.b "
               "WHERE con.contype = %s AND ns.nspname = %s")
    cur.execute(key_sql, ("p", schema))
    pks = {(r[0], r[1]) for r in cur.fetchall()}
    cur.execute(key_sql, ("f", schema))
    fks = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    return tables, cols, pks, fks


def _my_meta(cur):
    cur.execute("SELECT table_name, IF(table_type = 'VIEW', 'view', 'table'), table_rows FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY 2, 1")
    tables = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    cur.execute("SELECT table_name, column_name, column_type, is_nullable = 'NO', column_key = 'PRI' "
                "FROM information_schema.columns WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position")
    raw = cur.fetchall()
    cols = [(r[0], r[1], r[2], bool(r[3])) for r in raw]
    pks = {(r[0], r[1]) for r in raw if r[4]}
    cur.execute("SELECT table_name, column_name, referenced_table_name, referenced_column_name "
                "FROM information_schema.key_column_usage "
                "WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL")
    fks = [tuple(r) for r in cur.fetchall()]
    return tables, cols, pks, fks


def introspect(c: ConnInfo) -> dict:
    """Same structure as datasources.get_schema for SQLite, plus the server version."""
    conn = _connect(c, timeout_s=max(config.QUERY_TIMEOUT_S, 20))
    try:
        cur = conn.cursor()
        if c.dialect == "postgres":
            cur.execute("SHOW server_version")
            version = "PostgreSQL " + str(cur.fetchone()[0]).split()[0]
            tables, cols, pks, fks = _pg_meta(cur, c.db_schema or "public")
        else:
            cur.execute("SELECT VERSION()")
            version = "MySQL " + str(cur.fetchone()[0])
            tables, cols, pks, fks = _my_meta(cur)

        out = []
        for name, kind, estimate in tables[:300]:
            tcols = [{"name": col, "type": str(typ), "notnull": bool(nn), "pk": (name, col) in pks}
                     for t, col, typ, nn in cols if t == name]
            tfks = [{"column": col, "ref_table": rt, "ref_column": rc} for t, col, rt, rc in fks if t == name]
            for col in tcols:
                fk = next((f for f in tfks if f["column"] == col["name"]), None)
                if fk:
                    col["fk"] = f"{fk['ref_table']}.{fk['ref_column']}"
            qn = _q(c.dialect, name)
            count = int(estimate) if estimate is not None and int(estimate) >= 0 else None
            try:
                if count is None or count < 200_000:
                    cur.execute(f"SELECT COUNT(*) FROM {qn}")
                    count = int(cur.fetchone()[0])
                cur.execute(f"SELECT * FROM {qn} LIMIT 3")
                samples = [[_jsonable(v) for v in r] for r in cur.fetchall()]
                hints = {}
                if count and count <= 200_000:
                    for col in tcols:
                        if any(t in col["type"].lower() for t in TEXT_TYPES):
                            qc = _q(c.dialect, col["name"])
                            cur.execute(f"SELECT DISTINCT {qc} FROM {qn} WHERE {qc} IS NOT NULL LIMIT 13")
                            vals = [_jsonable(r[0]) for r in cur.fetchall()]
                            if 0 < len(vals) <= 12 and count > len(vals) * 2:
                                hints[col["name"]] = vals
            except Exception:
                if c.dialect == "postgres":
                    conn.rollback()  # aborted transaction: keep introspecting the remaining tables
                samples, hints = [], {}
            ddl_cols = []
            for col in tcols:
                line = f"  {_q(c.dialect, col['name'])} {col['type']}"
                if col["pk"]:
                    line += " PRIMARY KEY"
                elif col["notnull"]:
                    line += " NOT NULL"
                if col.get("fk"):
                    rt, rc = col["fk"].split(".", 1)
                    line += f" REFERENCES {_q(c.dialect, rt)}({_q(c.dialect, rc)})"
                ddl_cols.append(line)
            ddl = f"CREATE {'VIEW' if kind == 'view' else 'TABLE'} {qn} (\n" + ",\n".join(ddl_cols) + "\n)"
            out.append({"name": name, "kind": kind, "ddl": ddl, "columns": tcols, "foreign_keys": tfks,
                        "row_count": count, "samples": samples, "value_hints": hints})
    except ConnectorError:
        raise
    except Exception as e:
        raise ConnectorError(f"Introspection failed: {_error_text(e)}")
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    return {"tables": out, "version": version}
