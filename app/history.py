"""Query history persisted in the app's own SQLite database."""
import json
import os
import sqlite3
from contextlib import contextmanager

from . import config, crypto

DDL = """
CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL,
    question      TEXT NOT NULL,
    sql           TEXT,
    explanation   TEXT,
    model         TEXT,
    status        TEXT NOT NULL,     -- ok | error | unanswerable
    error         TEXT,
    row_count     INTEGER,
    attempts      INTEGER,
    duration_ms   REAL,
    tokens        INTEGER,
    cost          REAL,              -- USD reported by OpenRouter
    starred       INTEGER NOT NULL DEFAULT 0,
    feedback      INTEGER,           -- 1 = confirmed correct (reused as an example), -1 = wrong, NULL = none
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_history_source ON history(source_id, id DESC);
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    dialect       TEXT NOT NULL,     -- postgres | mysql
    host          TEXT NOT NULL,
    port          INTEGER NOT NULL,
    database      TEXT NOT NULL,
    user          TEXT NOT NULL,
    password      TEXT NOT NULL,     -- Fernet-encrypted ("enc1:..."), see crypto.py; use a read-only DB user
    db_schema     TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    request_id    TEXT,
    client        TEXT,              -- client IP
    kind          TEXT NOT NULL,     -- ask (model-generated) | execute (submitted by the client)
    source_id     TEXT NOT NULL,
    sql           TEXT NOT NULL,
    status        TEXT NOT NULL,     -- ok | error
    row_count     INTEGER,
    error         TEXT,
    duration_ms   REAL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at);
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost              REAL NOT NULL      -- USD reported by the provider (0 for local models)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
CREATE TABLE IF NOT EXISTS source_settings (
    source_id     TEXT PRIMARY KEY,
    settings      TEXT NOT NULL      -- JSON: {"hidden": ["table", "table.column", ...], "notes": "..."}
);
CREATE TABLE IF NOT EXISTS suggestions (
    source_id     TEXT PRIMARY KEY,
    items         TEXT NOT NULL
);
"""


_ready = False


@contextmanager
def _conn():
    if not _ready:  # CLI tools and tests may use the store before the app's startup hook ran
        init()
    conn = sqlite3.connect(config.APP_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    global _ready
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.APP_DB)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(DDL)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(history)")}
        if "cost" not in cols:  # migrate databases created by v1.0
            conn.execute("ALTER TABLE history ADD COLUMN cost REAL")
        if "feedback" not in cols:  # v1.2
            conn.execute("ALTER TABLE history ADD COLUMN feedback INTEGER")
        for row in conn.execute("SELECT id, password FROM connections").fetchall():
            if not crypto.is_encrypted(row["password"]):  # encrypt passwords stored in plaintext by v1.1
                conn.execute("UPDATE connections SET password = ? WHERE id = ?",
                             (crypto.encrypt(row["password"]), row["id"]))
        conn.commit()
    finally:
        conn.close()
    try:
        os.chmod(config.APP_DB, 0o600)  # holds database connection passwords
    except OSError:
        pass
    _ready = True


def ping() -> None:
    """Raises if the app database cannot be written."""
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS _ping (x INTEGER)")
        c.execute("DELETE FROM _ping")


def add(**fields) -> int:
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with _conn() as c:
        cur = c.execute(f"INSERT INTO history ({cols}) VALUES ({marks})", list(fields.values()))
        return cur.lastrowid


def list_(source_id: str | None, limit: int = 100, starred: bool = False) -> list[dict]:
    q, args = "SELECT * FROM history WHERE 1=1", []
    if source_id:
        q += " AND source_id = ?"
        args.append(source_id)
    if starred:
        q += " AND starred = 1"
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def set_starred(item_id: int, starred: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE history SET starred = ? WHERE id = ?", (int(starred), item_id))


def set_feedback(item_id: int, feedback: int | None, sql: str | None = None) -> bool:
    """Record a verdict on an answer; `sql` replaces the stored query when the user corrected it."""
    with _conn() as c:
        if sql:
            cur = c.execute("UPDATE history SET feedback = ?, sql = ?, status = 'ok', error = NULL WHERE id = ?",
                            (feedback, sql, item_id))
        else:
            cur = c.execute("UPDATE history SET feedback = ? WHERE id = ?", (feedback, item_id))
        return cur.rowcount > 0


def delete(item_id: int | None = None, source_id: str | None = None) -> None:
    with _conn() as c:
        if item_id is not None:
            c.execute("DELETE FROM history WHERE id = ?", (item_id,))
        elif source_id:
            c.execute("DELETE FROM history WHERE source_id = ? AND starred = 0", (source_id,))


def get_suggestions(source_id: str) -> list[str] | None:
    with _conn() as c:
        row = c.execute("SELECT items FROM suggestions WHERE source_id = ?", (source_id,)).fetchone()
        return json.loads(row["items"]) if row else None


def save_suggestions(source_id: str, items: list[str]) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO suggestions VALUES (?, ?)", (source_id, json.dumps(items)))


def add_audit(**fields) -> None:
    cols = ", ".join(fields)
    with _conn() as c:
        c.execute(f"INSERT INTO audit ({cols}) VALUES ({', '.join('?' for _ in fields)})", list(fields.values()))


def list_audit(source_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM audit WHERE 1=1", []
    if source_id:
        q += " AND source_id = ?"
        args.append(source_id)
    if status:
        q += " AND status = ?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def prune_audit(days: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM audit WHERE created_at < datetime('now', ?)", (f"-{int(days)} days",))


def add_llm_usage(model: str, prompt_tokens: int, completion_tokens: int, cost: float) -> None:
    with _conn() as c:
        c.execute("INSERT INTO llm_usage (model, prompt_tokens, completion_tokens, cost) VALUES (?, ?, ?, ?)",
                  (model, prompt_tokens, completion_tokens, cost))


def spent_today() -> float:
    """USD spent on LLM calls since midnight UTC."""
    with _conn() as c:
        return c.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_usage WHERE created_at >= date('now')").fetchone()[0]


def get_source_settings(source_id: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT settings FROM source_settings WHERE source_id = ?", (source_id,)).fetchone()
    return json.loads(row["settings"]) if row else {}


def save_source_settings(source_id: str, settings: dict) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO source_settings VALUES (?, ?)", (source_id, json.dumps(settings)))


def drop_source(source_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM source_settings WHERE source_id = ?", (source_id,))
        c.execute("DELETE FROM history WHERE source_id = ?", (source_id,))
        c.execute("DELETE FROM suggestions WHERE source_id = ?", (source_id,))


_CONN_FIELDS = ("dialect", "host", "port", "database", "user", "password", "db_schema")


def add_connection(conn_id: str, name: str, **fields) -> None:
    with _conn() as c:
        values = {**fields, "password": crypto.encrypt(fields["password"])}
        c.execute("INSERT INTO connections (id, name, dialect, host, port, database, user, password, db_schema) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (conn_id, name, *[values[f] for f in _CONN_FIELDS]))


def get_connection(conn_id: str) -> dict | None:
    """Connection row with the password decrypted (raises crypto.DecryptError if the key changed)."""
    with _conn() as c:
        row = c.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["password"] = crypto.decrypt(out["password"])
    return out


def connection_dialect(conn_id: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT dialect FROM connections WHERE id = ?", (conn_id,)).fetchone()
    return row["dialect"] if row else None


def list_connections() -> list[dict]:
    """Connection rows without passwords."""
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM connections ORDER BY name")]
    for r in rows:
        r.pop("password", None)
    return rows


def delete_connection(conn_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
