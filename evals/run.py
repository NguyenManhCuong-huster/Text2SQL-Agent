"""Execution-accuracy benchmark for the text-to-SQL pipeline.

    python -m evals.run                                   # default model
    python -m evals.run --models anthropic/claude-haiku-5.5,openai/gpt-5.5 --concurrency 4
    python -m evals.run --only h01,h02 -v
    python -m evals.run --db-url postgresql://user:pass@localhost:5432/shopdb   # same questions on PostgreSQL/MySQL

Gold answers always come from the SQLite sample; with --db-url the model queries the external copy
(seed it with `python -m app.seed_external <url>`), so this measures accuracy per SQL dialect.

A prediction counts as correct when its result set matches the gold query's: same number of rows,
and every gold column has a matching predicted column (extra predicted columns are allowed).
Values are compared order-insensitively unless the gold SQL ends with an ORDER BY.
"""
import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from urllib.parse import unquote, urlparse

from app import config, connectors, datasources, history, pipeline, sample_data

HERE = Path(__file__).parent


_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def _norm(v):
    if isinstance(v, str):
        v = v.strip()
        # engines disagree on types: SQLite strftime('%Y') gives '2023', EXTRACT/YEAR() give 2023
        if _NUMERIC.match(v):
            return float(v) if "." in v else int(v)
        return v
    if isinstance(v, bool):
        return int(v)
    return v


def _same(a, b) -> bool:
    """Numbers match within rounding tolerance (models often ROUND(x, 2)); everything else exactly."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.01, 1e-4 * max(abs(a), abs(b)))
    return a == b


def _sort_key(v):
    return (v is None, not isinstance(v, (int, float)), v if isinstance(v, (int, float)) else str(v))


def _column(rows, i, ordered):
    col = [_norm(r[i]) for r in rows]
    return col if ordered else sorted(col, key=_sort_key)


def results_match(gold: dict, pred: dict, ordered: bool) -> bool:
    if gold["row_count"] != pred["row_count"]:
        return False
    pred_cols = [_column(pred["rows"], j, ordered) for j in range(len(pred["columns"]))]
    used = set()
    for i in range(len(gold["columns"])):
        g = _column(gold["rows"], i, ordered)
        match = next((j for j, c in enumerate(pred_cols)
                      if j not in used and all(_same(x, y) for x, y in zip(g, c))), None)
        if match is None:
            return False
        used.add(match)
    return True


def _is_ordered(sql: str) -> bool:
    # ORDER BY after the last closing paren = top-level ordering
    tail = sql.rsplit(")", 1)[-1]
    return bool(re.search(r"\border\s+by\b", tail, re.I)) and not re.search(r"\blimit\s+1\b", tail, re.I)


def register_db(url: str) -> str:
    """Create (or reuse) a connection for an external copy of the shop database; returns its source id."""
    u = urlparse(url)
    dialect = "postgres" if u.scheme.startswith("postgres") else "mysql" if u.scheme.startswith("mysql") else None
    if not dialect:
        raise SystemExit(f"Unsupported URL scheme: {u.scheme}")
    sid = f"eval_{dialect}"
    if history.get_connection(sid):
        datasources.delete_source(sid)
    info = connectors.ConnInfo(dialect, u.hostname or "localhost", u.port or connectors.DIALECTS[dialect]["port"],
                               u.path.lstrip("/"), unquote(u.username or ""), unquote(u.password or ""))
    created = datasources.add_connection(sid, info)
    assert created == sid, created
    return sid


async def run_case(case: dict, model: str, sem: asyncio.Semaphore, source: str = "shop") -> dict:
    gold = datasources.execute("shop", case["sql"], max_rows=10000)
    async with sem:
        t = time.perf_counter()
        try:
            out = await pipeline.ask(source, case["question"], model, [], use_cache=False)
        except Exception as e:  # network / provider errors count as failures
            out = {"status": "error", "error": str(e), "sql": None, "result": None, "attempts": [], "cost": 0, "tokens": 0}
        latency = time.perf_counter() - t
    pred = out.get("result")
    if pred and pred["truncated"]:
        pred = datasources.execute(source, out["sql"], max_rows=10000)
    correct = bool(pred) and results_match(gold, pred, _is_ordered(case["sql"]))
    return {
        "id": case["id"], "level": case["level"], "question": case["question"], "model": model,
        "correct": correct, "status": out["status"], "sql": out.get("sql"), "error": out.get("error"),
        "retries": len(out.get("attempts") or []), "latency_s": round(latency, 2),
        "cost": out.get("cost") or 0, "tokens": out.get("tokens") or 0,
    }


def summarize(results: list[dict], models: list[str]) -> str:
    levels = ["easy", "medium", "hard", "expert"]
    lines = ["| Model | Overall | " + " | ".join(levels) + " | Self-corrected | Avg latency | Cost / question |",
             "|---|---|" + "---|" * len(levels) + "---|---|---|"]
    for m in models:
        rs = [r for r in results if r["model"] == m]
        if not rs:
            continue

        def acc(sub):
            return f"{sum(r['correct'] for r in sub)}/{len(sub)}" if sub else "-"
        pct = 100 * sum(r["correct"] for r in rs) / len(rs)
        lines.append(
            f"| `{m}` | **{pct:.0f}%** ({acc(rs)}) | "
            + " | ".join(acc([r for r in rs if r["level"] == lv]) for lv in levels)
            + f" | {sum(1 for r in rs if r['retries'])} | {sum(r['latency_s'] for r in rs) / len(rs):.1f}s"
            + f" | ${sum(r['cost'] for r in rs) / len(rs):.4f} |")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=config.DEFAULT_MODEL, help="comma-separated OpenRouter model ids")
    ap.add_argument("--cases", default=str(HERE / "shop.json"))
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--db-url", default="", help="run against an external copy (postgresql://... or mysql://...)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print SQL of failed cases")
    args = ap.parse_args()

    sample_data.ensure_sample()
    history.init()
    source = register_db(args.db_url) if args.db_url else "shop"
    print(f"Source: {source} ({datasources.dialect(source)})")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sem = asyncio.Semaphore(args.concurrency)

    results = []
    for model in models:
        print(f"\n== {model} ({len(cases)} cases)")
        for coro in asyncio.as_completed([run_case(c, model, sem, source) for c in cases]):
            r = await coro
            results.append(r)
            mark = "PASS" if r["correct"] else "FAIL"
            extra = f" retries={r['retries']}" if r["retries"] else ""
            print(f"  {mark} {r['id']:<4} {r['latency_s']:>5.1f}s{extra}  {r['question'][:70]}")
            if args.verbose and not r["correct"]:
                print(f"       status={r['status']} error={r['error']}\n       sql: {(r['sql'] or '').replace(chr(10), ' ')}")

    table = summarize(results, models)
    print("\n" + table)
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{stamp}.md").write_text(table + "\n", encoding="utf-8")
    print(f"\nSaved evals/results/{stamp}.json")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
