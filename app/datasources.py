"""Data sources: SQLite files under DATA_DIR/sources plus external PostgreSQL/MySQL connections.

Every function takes a source id and dispatches on its kind, so the pipeline and API stay engine-agnostic.
"""
import csv
import io
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import audit, config, connectors, crypto, history

SAMPLE_ID = "shop"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# sqlite3 authorizer action codes that a read-only query may need.
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}
_BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


class QueryError(Exception):
    pass


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48]
    return slug or "source"


def source_path(source_id: str) -> Path:
    if not _ID_RE.match(source_id or ""):
        raise KeyError(source_id)
    path = config.SOURCES_DIR / f"{source_id}.sqlite"
    if not path.exists():
        raise KeyError(source_id)
    return path


def unique_id(base: str) -> str:
    base = slugify(base)
    candidate, n = base, 2
    while (config.SOURCES_DIR / f"{candidate}.sqlite").exists() or history.get_connection(candidate):
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def _conn_info(source_id: str) -> connectors.ConnInfo | None:
    if not _ID_RE.match(source_id or ""):
        return None
    try:
        row = history.get_connection(source_id)
    except crypto.DecryptError as e:
        raise QueryError(str(e))
    if not row:
        return None
    return connectors.ConnInfo(dialect=row["dialect"], host=row["host"], port=row["port"], database=row["database"],
                               user=row["user"], password=row["password"], db_schema=row["db_schema"])


def exists(source_id: str) -> bool:
    try:
        source_path(source_id)
        return True
    except KeyError:
        return _connection_dialect(source_id) is not None


def _connection_dialect(source_id: str) -> str | None:
    return history.connection_dialect(source_id) if _ID_RE.match(source_id or "") else None


def dialect(source_id: str) -> str:
    return _connection_dialect(source_id) or "sqlite"


def _authorizer(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def connect_ro(source_id: str) -> sqlite3.Connection:
    path = source_path(source_id)
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


# ---------------------------------------------------------------- listing / schema

def list_sources() -> list[dict]:
    out = []
    for path in sorted(config.SOURCES_DIR.glob("*.sqlite")):
        sid = path.stem
        try:
            schema = get_schema(sid)
            tables = len(schema["tables"])
        except Exception:
            tables = 0
        out.append({
            "id": sid,
            "name": sid.replace("_", " ").title() if sid != SAMPLE_ID else "Shop (sample)",
            "tables": tables,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "builtin": sid == SAMPLE_ID,
            "kind": "sqlite",
        })
    for row in history.list_connections():
        cached = _schema_cache.get(row["id"])
        info = connectors.ConnInfo(dialect=row["dialect"], host=row["host"], port=row["port"],
                                   database=row["database"], user=row["user"], db_schema=row["db_schema"])
        out.append({
            "id": row["id"], "name": row["name"], "kind": row["dialect"],
            "tables": len(cached[1]["tables"]) if cached else None,
            "detail": info.label, "builtin": False,
        })
    out.sort(key=lambda s: (not s["builtin"], s.get("kind", "sqlite") != "sqlite", s["name"].lower()))
    return out


_schema_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def get_schema(source_id: str, refresh: bool = False) -> dict:
    """Structured schema with columns, keys, row counts, samples and low-cardinality value hints."""
    info = _conn_info(source_id)
    if info:
        with _cache_lock:
            cached = _schema_cache.get(source_id)
        if cached and not refresh and time.monotonic() - cached[0] < connectors.SCHEMA_TTL_S:
            return cached[1]
        try:
            meta = connectors.introspect(info)
        except connectors.ConnectorError as e:
            if cached:  # serve the last known schema if the database is briefly unreachable
                return cached[1]
            raise QueryError(str(e))
        schema = {"id": source_id, "dialect": info.dialect, "version": meta["version"], "tables": meta["tables"]}
        with _cache_lock:
            _schema_cache[source_id] = (time.monotonic(), schema)
        return schema
    path = source_path(source_id)
    mtime = path.stat().st_mtime
    with _cache_lock:
        cached = _schema_cache.get(source_id)
        if cached and cached[0] == mtime:
            return cached[1]

    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = []
        for kind, name, ddl in rows:
            cols = [
                {"name": c[1], "type": c[2] or "", "notnull": bool(c[3]), "pk": bool(c[5])}
                for c in conn.execute(f"PRAGMA table_info({_qi(name)})")
            ]
            fks = [
                {"column": f[3], "ref_table": f[2], "ref_column": f[4]}
                for f in conn.execute(f"PRAGMA foreign_key_list({_qi(name)})")
            ]
            fk_cols = {f["column"]: f for f in fks}
            for c in cols:
                if c["name"] in fk_cols:
                    c["fk"] = f'{fk_cols[c["name"]]["ref_table"]}.{fk_cols[c["name"]]["ref_column"]}'
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {_qi(name)}").fetchone()[0]
            except sqlite3.Error:
                count = None
            sample_cur = conn.execute(f"SELECT * FROM {_qi(name)} LIMIT 3")
            samples = [list(r) for r in sample_cur.fetchall()]
            # Value hints for categorical text columns help the model match literals exactly.
            hints = {}
            if count and count > 0:
                for c in cols:
                    if "char" in c["type"].lower() or "text" in c["type"].lower() or not c["type"]:
                        vals = conn.execute(
                            f"SELECT DISTINCT {_qi(c['name'])} FROM {_qi(name)} "
                            f"WHERE {_qi(c['name'])} IS NOT NULL LIMIT 13"
                        ).fetchall()
                        if 0 < len(vals) <= 12 and count > len(vals) * 2:
                            hints[c["name"]] = [v[0] for v in vals]
            tables.append({
                "name": name, "kind": kind, "ddl": ddl or "", "columns": cols,
                "foreign_keys": fks, "row_count": count, "samples": samples, "value_hints": hints,
            })
    finally:
        conn.close()

    schema = {"id": source_id, "dialect": "sqlite", "version": f"SQLite {sqlite3.sqlite_version}", "tables": tables}
    with _cache_lock:
        _schema_cache[source_id] = (mtime, schema)
    return schema


def _fmt(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return "<blob>"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


def schema_prompt(source_id: str, max_chars: int = 24000, tables: list[str] | None = None) -> str:
    """Compact schema description for the LLM prompt, optionally restricted to some tables."""
    selected = [t for t in get_schema(source_id)["tables"] if tables is None or t["name"] in tables]
    parts = []
    for t in selected:
        block = [t["ddl"].strip() + ";" if t["ddl"] else f"-- {t['kind']} {t['name']}"]
        if t["row_count"] is not None:
            block.append(f"-- rows: {t['row_count']}")
        if t["value_hints"]:
            for col, vals in t["value_hints"].items():
                block.append(f"-- {col} values: " + ", ".join(repr(v) for v in vals))
        if t["samples"]:
            block.append("-- sample rows (" + ", ".join(c["name"] for c in t["columns"]) + "):")
            for r in t["samples"]:
                block.append("--   " + " | ".join(_fmt(v) for v in r))
        parts.append("\n".join(block))
    text = "\n\n".join(parts)
    if len(text) > max_chars:  # very wide schemas: drop samples/hints, keep DDL
        text = "\n\n".join(t["ddl"].strip() + ";" for t in selected)[:max_chars]
    return text


# ---------------------------------------------------------------- execution

_WRITE_RE = re.compile(
    r"^\s*(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex|analyze|begin|commit)\b",
    re.I,
)


def clean_sql(sql: str) -> str:
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def execute(source_id: str, sql: str, max_rows: int | None = None) -> dict:
    """Run one read-only query; every attempt made during an API request is written to the audit log."""
    started = time.perf_counter()
    try:
        out = _execute(source_id, sql, max_rows)
    except QueryError as e:
        audit.record(source_id, sql, "error", error=str(e), duration_ms=round((time.perf_counter() - started) * 1000, 1))
        raise
    audit.record(source_id, sql, "ok", row_count=out["row_count"], duration_ms=out["duration_ms"])
    return out


def _execute(source_id: str, sql: str, max_rows: int | None = None) -> dict:
    max_rows = max_rows or config.MAX_ROWS
    sql = clean_sql(sql)
    if not sql:
        raise QueryError("Empty query.")
    info = _conn_info(source_id)
    if info:
        try:
            return connectors.execute(info, sql, max_rows)
        except connectors.ConnectorError as e:
            raise QueryError(str(e))
    if _WRITE_RE.match(sql):
        raise QueryError("Only read-only SELECT / WITH queries are allowed.")
    try:
        conn = connect_ro(source_id)
    except KeyError:
        raise QueryError(f"Unknown data source '{source_id}'.")

    deadline = time.monotonic() + config.QUERY_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    conn.set_authorizer(_authorizer)
    start = time.perf_counter()
    try:
        cur = conn.execute(sql)
        if cur.description is None:
            raise QueryError("Statement returned no result set.")
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(max_rows + 1)
    except (sqlite3.Error, sqlite3.Warning) as e:
        msg = str(e)
        if "interrupted" in msg:
            msg = f"Query exceeded the {config.QUERY_TIMEOUT_S:g}s time limit."
        elif "not authorized" in msg or "readonly" in msg:
            msg = "Not authorized: only read-only queries are allowed."
        raise QueryError(msg)
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


def _jsonable(v):
    if isinstance(v, bytes):
        return f"<blob {len(v)} bytes>"
    return v


# ---------------------------------------------------------------- uploads

def _infer_type(values: list[str]) -> str:
    seen = [v for v in values if v != ""]
    if not seen:
        return "TEXT"
    try:
        for v in seen:
            int(v)
        return "INTEGER"
    except ValueError:
        pass
    try:
        for v in seen:
            float(v)
        return "REAL"
    except ValueError:
        return "TEXT"


def _convert(v: str, typ: str):
    if v == "":
        return None
    if typ == "INTEGER":
        return int(v)
    if typ == "REAL":
        return float(v)
    return v


def _unique_names(names: list[str]) -> list[str]:
    out, seen = [], {}
    for i, n in enumerate(names):
        n = re.sub(r"\s+", "_", (n or "").strip()) or f"column_{i + 1}"
        base, k = n, 2
        while n.lower() in seen:
            n, k = f"{base}_{k}", k + 1
        seen[n.lower()] = True
        out.append(n)
    return out


def import_csv_files(name: str, files: list[tuple[str, bytes]]) -> str:
    source_id = unique_id(name)
    path = config.SOURCES_DIR / f"{source_id}.sqlite"
    conn = sqlite3.connect(path)
    try:
        used = set()
        for filename, data in files:
            text = data.decode("utf-8-sig", errors="replace")
            try:
                dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(io.StringIO(text), dialect)
            all_rows = list(reader)
            if not all_rows:
                continue
            header = _unique_names(all_rows[0])
            body = [r + [""] * (len(header) - len(r)) for r in all_rows[1:]]
            body = [r[: len(header)] for r in body]
            types = [_infer_type([r[i] for r in body]) for i in range(len(header))]
            table = slugify(Path(filename).stem)
            if table[0].isdigit():
                table = "t_" + table
            base, k = table, 2
            while table in used:
                table, k = f"{base}_{k}", k + 1
            used.add(table)
            cols = ", ".join(f"{_qi(c)} {t}" for c, t in zip(header, types))
            conn.execute(f"CREATE TABLE {_qi(table)} ({cols})")
            placeholders = ", ".join("?" for _ in header)
            conn.executemany(
                f"INSERT INTO {_qi(table)} VALUES ({placeholders})",
                ([_convert(v, t) for v, t in zip(r, types)] for r in body),
            )
        conn.commit()
        if not used:
            raise ValueError("No usable CSV content found.")
    except Exception:
        conn.close()
        path.unlink(missing_ok=True)
        raise
    conn.close()
    return source_id


def import_sqlite_file(name: str, data: bytes) -> str:
    if not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("File is not a valid SQLite database.")
    source_id = unique_id(name)
    path = config.SOURCES_DIR / f"{source_id}.sqlite"
    path.write_bytes(data)
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
    except sqlite3.Error as e:
        path.unlink(missing_ok=True)
        raise ValueError(f"Cannot open database: {e}")
    return source_id


def add_connection(name: str, info: connectors.ConnInfo) -> str:
    """Introspect first (fails fast on bad credentials), then persist."""
    meta = connectors.introspect(info)
    source_id = unique_id(name or info.database)
    history.add_connection(source_id, name or info.database, dialect=info.dialect, host=info.host, port=info.port,
                           database=info.database, user=info.user, password=info.password, db_schema=info.db_schema)
    with _cache_lock:
        _schema_cache[source_id] = (time.monotonic(), {
            "id": source_id, "dialect": info.dialect, "version": meta["version"], "tables": meta["tables"]})
    return source_id


def delete_source(source_id: str) -> None:
    if source_id == SAMPLE_ID:
        raise PermissionError("The sample database cannot be deleted.")
    if _connection_dialect(source_id):
        history.delete_connection(source_id)
        with _cache_lock:
            _schema_cache.pop(source_id, None)
        return
    path = source_path(source_id)
    with _cache_lock:
        _schema_cache.pop(source_id, None)
    path.unlink()
