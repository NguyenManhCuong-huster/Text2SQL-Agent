"""Access policy: tables and columns that are hidden from the model and blocked at execution.

Patterns are case-insensitive globs, either `table` (the whole table) or `table.column`, e.g. `audit_log`,
`customers.email`, `*.password*`. They come from HIDDEN_COLUMNS (all sources) plus each source's own list
(source settings). Hidden objects are:
  1. removed from the schema shown to the model and the UI (columns, sample values, value hints, keys, DDL);
  2. rejected by a static check of every query (sqlglot, with `*` expanded against the real schema);
  3. on SQLite also denied by the authorizer, which sees reads through views and subqueries.
"""
import copy
import fnmatch
import sqlite3

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from . import config, history

SQLGLOT_DIALECT = {"sqlite": "sqlite", "postgres": "postgres", "mysql": "mysql"}


class PolicyError(Exception):
    pass


def patterns(source_id: str) -> list[str]:
    own = history.get_source_settings(source_id).get("hidden", [])
    return [p.strip().lower() for p in [*config.HIDDEN_COLUMNS, *own] if p and p.strip()]


class Policy:
    def __init__(self, pats: list[str]):
        self.table_pats = [p for p in pats if "." not in p] + [p[:-2] for p in pats if p.endswith(".*")]
        self.column_pats = [p for p in pats if "." in p and not p.endswith(".*")]

    def __bool__(self) -> bool:
        return bool(self.table_pats or self.column_pats)

    def table_hidden(self, table: str) -> bool:
        t = (table or "").lower()
        return any(fnmatch.fnmatchcase(t, p) for p in self.table_pats)

    def column_hidden(self, table: str, column: str) -> bool:
        if self.table_hidden(table):
            return True
        key = f"{(table or '').lower()}.{(column or '').lower()}"
        return any(fnmatch.fnmatchcase(key, p) for p in self.column_pats)


def for_source(source_id: str) -> Policy:
    return Policy(patterns(source_id))


# ---------------------------------------------------------------- schema filtering

def _ddl(table: dict, quote) -> str:
    lines = []
    for c in table["columns"]:
        line = f"  {quote(c['name'])} {c['type']}".rstrip()
        if c.get("pk"):
            line += " PRIMARY KEY"
        elif c.get("notnull"):
            line += " NOT NULL"
        if c.get("fk"):
            rt, rc = c["fk"].split(".", 1)
            line += f" REFERENCES {quote(rt)}({quote(rc)})"
        lines.append(line)
    kind = "VIEW" if table["kind"] == "view" else "TABLE"
    return f"CREATE {kind} {quote(table['name'])} (\n" + ",\n".join(lines) + "\n)"


def filter_schema(schema: dict, pol: Policy) -> dict:
    """Copy of the schema without hidden tables/columns (and without their samples, hints and keys)."""
    if not pol:
        return schema
    quote = ((lambda n: "`" + n.replace("`", "``") + "`") if schema.get("dialect") == "mysql"
             else (lambda n: '"' + n.replace('"', '""') + '"'))
    out = {k: v for k, v in schema.items() if k != "tables"}
    out["tables"] = []
    for t in schema["tables"]:
        if pol.table_hidden(t["name"]):
            continue
        keep = [i for i, c in enumerate(t["columns"]) if not pol.column_hidden(t["name"], c["name"])]
        refs_hidden = any(pol.table_hidden(f["ref_table"]) for f in t["foreign_keys"])
        if len(keep) == len(t["columns"]) and not refs_hidden:
            out["tables"].append(t)
            continue
        nt = copy.deepcopy(t)
        nt["columns"] = [nt["columns"][i] for i in keep]
        names = {c["name"] for c in nt["columns"]}
        for c in nt["columns"]:
            if c.get("fk") and pol.table_hidden(c["fk"].split(".", 1)[0]):
                c.pop("fk")
        nt["foreign_keys"] = [f for f in nt["foreign_keys"] if f["column"] in names and not pol.table_hidden(f["ref_table"])]
        nt["samples"] = [[r[i] for i in keep if i < len(r)] for r in nt["samples"]]
        nt["value_hints"] = {k: v for k, v in nt["value_hints"].items() if k in names}
        nt["ddl"] = _ddl(nt, quote)
        if len(keep) < len(t["columns"]):
            nt["hidden_columns"] = len(t["columns"]) - len(keep)
        out["tables"].append(nt)
    out["hidden_tables"] = sum(1 for t in schema["tables"] if pol.table_hidden(t["name"]))
    return out


# ---------------------------------------------------------------- query checks

def check_sql(sql: str, dialect: str, raw_schema: dict, pol: Policy) -> None:
    """Raise PolicyError if the query reads a hidden table or column (static check, before execution)."""
    if not pol:
        return
    tables = {t["name"].lower(): t for t in raw_schema["tables"]}
    if not any(pol.table_hidden(n) or any(pol.column_hidden(n, c["name"]) for c in t["columns"])
               for n, t in tables.items()):
        return  # nothing in this database is hidden
    try:
        tree = sqlglot.parse_one(sql, read=SQLGLOT_DIALECT[dialect])
    except sqlglot.errors.ParseError:
        return  # unparseable: the engine rejects it anyway (and the SQLite authorizer still applies)
    if tree is None:
        return
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias_to_table = {}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names:
            continue
        if pol.table_hidden(name):
            raise PolicyError(f"Table {t.name} is restricted on this data source.")
        alias_to_table[t.alias_or_name.lower()] = name

    mapping = {n: {c["name"]: (c["type"] or "TEXT") for c in t["columns"]} for n, t in tables.items()}
    try:
        qualified = qualify(tree.copy(), schema=mapping, dialect=SQLGLOT_DIALECT[dialect],
                            validate_qualify_columns=False, identify=False)
    except Exception:  # noqa: BLE001 - fall back to the conservative check below
        qualified = None

    if qualified is not None:
        q_alias = {t.alias_or_name.lower(): t.name.lower() for t in qualified.find_all(exp.Table)}
        for col in qualified.find_all(exp.Column):
            table = q_alias.get(col.table.lower())
            if table and table in tables and pol.column_hidden(table, col.name):
                raise PolicyError(f"Column {table}.{col.name} is restricted on this data source.")
        return

    # Conservative fallback: any reference to a hidden column name, or `*` over a table with hidden columns.
    referenced = set(alias_to_table.values())
    hidden_names = {c["name"].lower() for n in referenced if n in tables
                    for c in tables[n]["columns"] if pol.column_hidden(n, c["name"])}
    for col in tree.find_all(exp.Column):
        if col.name.lower() in hidden_names:
            raise PolicyError(f"Column {col.name} is restricted on this data source.")
    if hidden_names and any(True for _ in tree.find_all(exp.Star)):
        raise PolicyError("SELECT * is not allowed on tables with restricted columns; list the columns.")


def sqlite_authorizer(pol: Policy):
    """Authorizer layer for SQLite: deny direct reads of hidden objects; NULL them out when read via a view."""
    def check(action, table, column, _db, via):
        if action == sqlite3.SQLITE_READ and pol:
            if column == "" and pol.table_hidden(table):
                return sqlite3.SQLITE_DENY
            if column and pol.column_hidden(table, column):
                return sqlite3.SQLITE_IGNORE if via else sqlite3.SQLITE_DENY
        return None
    return check
