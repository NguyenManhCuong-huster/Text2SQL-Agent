"""Text2SQL Studio client - a single file with no dependencies (Python 3.9+). Copy it into any project.

Library:
    from text2sql_client import Text2SQL
    t2s = Text2SQL("http://localhost:8000", api_key="...")       # api_key: one of the server's API_KEYS
    out = t2s.ask("shop", "Revenue by month in 2025")
    print(out["sql"]); print(out["result"]["rows"][:5])
    t2s.feedback(out["id"], correct=True)                        # becomes a few-shot example

CLI:
    python text2sql_client.py sources
    python text2sql_client.py ask shop "Top 5 customers by revenue" [--json] [--candidates 3]
    python text2sql_client.py sql shop "SELECT COUNT(*) FROM orders"
    python text2sql_client.py schema shop
Environment: T2S_URL (default http://localhost:8000), T2S_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class Text2SQLError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class Text2SQL:
    def __init__(self, base_url: str = "http://localhost:8000", api_key: str | None = None, timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", e.reason)
            except ValueError:
                detail = e.reason
            raise Text2SQLError(e.code, detail if isinstance(detail, str) else json.dumps(detail)) from None
        return json.loads(raw) if raw else None

    # ------------------------------------------------------------ API
    def sources(self) -> list[dict]:
        return self._request("GET", "/api/sources")

    def schema(self, source_id: str, refresh: bool = False) -> dict:
        return self._request("GET", f"/api/sources/{urllib.parse.quote(source_id)}/schema",
                             params={"refresh": "true" if refresh else None})

    def ask(self, source_id: str, question: str, model: str | None = None, context: list[dict] | None = None,
            candidates: int | None = None, no_cache: bool = False) -> dict:
        """Question -> {status, sql, explanation, result: {columns, rows, row_count, truncated}, id, cost, ...}.

        context: earlier turns [{"question": ..., "sql": ...}] for follow-up questions.
        """
        body = {"source_id": source_id, "question": question, "model": model, "context": context or [],
                "no_cache": no_cache}
        if candidates:
            body["candidates"] = candidates
        return self._request("POST", "/api/ask", body)

    def execute(self, source_id: str, sql: str) -> dict:
        return self._request("POST", "/api/execute", {"source_id": source_id, "sql": sql})

    def answer(self, question: str, sql: str, result: dict, model: str | None = None) -> str:
        """A short natural-language answer for a result."""
        return self._request("POST", "/api/answer", {
            "question": question, "sql": sql, "columns": result["columns"], "rows": result["rows"][:40],
            "row_count": result["row_count"], "truncated": result.get("truncated", False), "model": model})["answer"]

    def feedback(self, history_id: int, correct: bool, corrected_sql: str | None = None) -> None:
        body = {"feedback": 1 if correct else -1}
        if correct and corrected_sql:
            body["sql"] = corrected_sql
        self._request("PATCH", f"/api/history/{history_id}", body)

    def settings(self, source_id: str) -> dict:
        return self._request("GET", f"/api/sources/{urllib.parse.quote(source_id)}/settings")

    def update_settings(self, source_id: str, notes: str | None = None, hidden: list[str] | None = None) -> dict:
        current = self.settings(source_id)
        body = {"notes": current["notes"] if notes is None else notes,
                "hidden": current["hidden"] if hidden is None else hidden}
        return self._request("PUT", f"/api/sources/{urllib.parse.quote(source_id)}/settings", body)

    def stats(self, days: int = 30) -> dict:
        return self._request("GET", "/api/stats", params={"days": days})

    def health(self) -> dict:
        return self._request("GET", "/api/ready")


# ------------------------------------------------------------ CLI

def format_table(columns: list[str], rows: list[list], limit: int = 50) -> str:
    shown = [["NULL" if v is None else str(v) for v in r] for r in rows[:limit]]
    widths = [min(40, max([len(c)] + [len(r[i]) for r in shown])) for i, c in enumerate(columns)]
    line = lambda cells: " | ".join(c[:w].ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    out = [line(columns), "-+-".join("-" * w for w in widths)] + [line(r) for r in shown]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more rows")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Text2SQL Studio client")
    ap.add_argument("--url", default=os.getenv("T2S_URL", "http://localhost:8000"))
    ap.add_argument("--key", default=os.getenv("T2S_API_KEY"), help="API key (or T2S_API_KEY)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources", help="list data sources")
    p = sub.add_parser("schema", help="show tables and columns")
    p.add_argument("source")
    p = sub.add_parser("ask", help="ask a question in natural language")
    p.add_argument("source")
    p.add_argument("question")
    p.add_argument("--model")
    p.add_argument("--candidates", type=int)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--json", action="store_true", help="print the raw JSON response")
    p = sub.add_parser("sql", help="run a read-only SQL query")
    p.add_argument("source")
    p.add_argument("sql")
    p.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    client = Text2SQL(args.url, args.key)
    try:
        if args.cmd == "sources":
            for s in client.sources():
                print(f"{s['id']:<24} {s.get('kind', 'sqlite'):<9} {s['name']}")
        elif args.cmd == "schema":
            for t in client.schema(args.source)["tables"]:
                cols = ", ".join(c["name"] + ("*" if c.get("pk") else "") for c in t["columns"])
                print(f"{t['name']} ({t.get('row_count')} rows): {cols}")
        elif args.cmd == "ask":
            out = client.ask(args.source, args.question, model=args.model, candidates=args.candidates,
                             no_cache=args.no_cache)
            if args.json:
                print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
            else:
                if out.get("explanation"):
                    print(out["explanation"] + "\n")
                if out.get("sql"):
                    print(out["sql"] + "\n")
                if out["status"] == "ok":
                    r = out["result"]
                    print(format_table(r["columns"], r["rows"]))
                    print(f"\n{r['row_count']} rows · {out['duration_ms'] / 1000:.1f}s · ${out.get('cost') or 0:.4f}"
                          + (" · reused" if out.get("cached") else ""))
                else:
                    print(f"[{out['status']}] {out.get('error') or ''}", file=sys.stderr)
                    return 1
        elif args.cmd == "sql":
            r = client.execute(args.source, args.sql)
            print(json.dumps(r, indent=2, ensure_ascii=False) if args.json else format_table(r["columns"], r["rows"]))
    except Text2SQLError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: cannot reach {args.url}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
