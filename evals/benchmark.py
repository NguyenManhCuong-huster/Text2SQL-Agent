"""Run public text-to-SQL benchmarks (BIRD, Spider) through the full pipeline.

    # BIRD dev: https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip (unzip, then unzip dev_databases.zip)
    python -m evals.benchmark --format bird --data data/benchmarks/bird/dev/dev.json \
        --db-root data/benchmarks/bird/dev/dev_databases --limit 200 --models anthropic/claude-haiku-5.5

    # Spider: dev.json + database/<db_id>/<db_id>.sqlite from https://yale-lily.github.io/spider
    python -m evals.benchmark --format spider --data spider/dev.json --db-root spider/database --limit 200

Each benchmark database is copied once into DATA_DIR/sources as `bench_<db_id>`. BIRD "evidence" hints are
appended to the question, as in the official setup. Two scores are reported:
  * strict  - the official BIRD/Spider execution metric: the predicted rows equal the gold rows as a set
              (column order matters, extra columns count as wrong);
  * lenient - this project's scorer (evals/run.py): extra columns allowed, numbers within a rounding tolerance.
Hidden-column defaults (HIDDEN_COLUMNS) are switched off unless --keep-policy is given, so the run measures
accuracy only.
"""
import argparse
import asyncio
import json
import os
import random
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

# Keep benchmark databases and history out of the app's own DATA_DIR (they would show up in the UI).
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "benchmarks" / "appdata"))

from app import config, datasources, history, pipeline  # noqa: E402
from evals.run import _is_ordered, results_match  # noqa: E402

HERE = Path(__file__).parent
GOLD_ROWS = 100_000


def load_cases(fmt: str, path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for i, r in enumerate(raw):
        question = r["question"].strip()
        if fmt == "bird" and (r.get("evidence") or "").strip():
            question += f"\nHint: {r['evidence'].strip()}"
        cases.append({
            "id": str(r.get("question_id", i)), "db_id": r["db_id"], "question": question,
            "sql": r.get("SQL") or r.get("query") or r.get("sql"),
            "level": r.get("difficulty") or r.get("hardness") or "all",
        })
    return cases


def register(db_root: Path, db_id: str) -> str:
    sid = "bench_" + datasources.slugify(db_id)[:56]
    target = config.SOURCES_DIR / f"{sid}.sqlite"
    if not target.exists():
        src = db_root / db_id / f"{db_id}.sqlite"
        if not src.exists():
            raise FileNotFoundError(src)
        shutil.copyfile(src, target)
    return sid


def strict_match(gold: dict, pred: dict) -> bool:
    """Official metric: set(predicted rows) == set(gold rows)."""
    def as_set(rows):
        return {tuple(r) for r in rows}
    return as_set(gold["rows"]) == as_set(pred["rows"])


async def run_case(case: dict, sid: str, model: str, sem: asyncio.Semaphore, candidates: int,
                   style: str = "exact") -> dict:
    out_row = {"id": case["id"], "db_id": case["db_id"], "level": case["level"], "model": model,
               "question": case["question"], "gold_sql": case["sql"]}
    try:
        gold = await asyncio.to_thread(datasources.execute, sid, case["sql"], GOLD_ROWS)
    except datasources.QueryError as e:
        return {**out_row, "skipped": f"gold query failed: {e}"}
    async with sem:
        t = time.perf_counter()
        try:
            out = await pipeline.ask(sid, case["question"], model, [], use_cache=False, candidates=candidates,
                                     style=style)
        except Exception as e:  # noqa: BLE001 - provider/network errors count as failures
            out = {"status": "error", "error": str(e), "sql": None, "result": None, "attempts": [], "cost": 0}
        latency = time.perf_counter() - t
    pred = out.get("result")
    if pred and pred["truncated"]:
        try:
            pred = await asyncio.to_thread(datasources.execute, sid, out["sql"], GOLD_ROWS)
        except datasources.QueryError:
            pred = None
    return {
        **out_row, "status": out["status"], "sql": out.get("sql"), "error": out.get("error"),
        "strict": bool(pred) and strict_match(gold, pred),
        "lenient": bool(pred) and results_match(gold, pred, _is_ordered(case["sql"])),
        "retries": len(out.get("attempts") or []), "latency_s": round(latency, 2), "cost": out.get("cost") or 0,
    }


def summarize(results: list[dict], models: list[str]) -> str:
    scored = [r for r in results if "skipped" not in r]
    levels = sorted({r["level"] for r in scored}, key=lambda lv: ["simple", "easy", "moderate", "medium", "hard",
                                                                  "challenging", "extra", "all"].index(lv)
                    if lv in ["simple", "easy", "moderate", "medium", "hard", "challenging", "extra", "all"] else 99)
    lines = ["| Model | n | Strict EX | Lenient EX | " + " | ".join(f"{lv} (strict)" for lv in levels)
             + " | Avg latency | Cost / question |",
             "|---|---|---|---|" + "---|" * len(levels) + "---|---|"]
    for m in models:
        rs = [r for r in scored if r["model"] == m]
        if not rs:
            continue

        def pct(sub, key="strict"):
            return f"{100 * sum(r[key] for r in sub) / len(sub):.1f}%" if sub else "-"
        lines.append(f"| `{m}` | {len(rs)} | **{pct(rs)}** | {pct(rs, 'lenient')} | "
                     + " | ".join(pct([r for r in rs if r["level"] == lv]) for lv in levels)
                     + f" | {sum(r['latency_s'] for r in rs) / len(rs):.1f}s | ${sum(r['cost'] for r in rs) / len(rs):.4f} |")
    skipped = len(results) - len(scored)
    if skipped:
        lines.append(f"\n{skipped} case(s) skipped because the gold query failed or timed out.")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["bird", "spider"], required=True)
    ap.add_argument("--data", required=True, help="dev.json")
    ap.add_argument("--db-root", required=True, help="directory with <db_id>/<db_id>.sqlite")
    ap.add_argument("--models", default=config.DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="random sample of N questions (0 = all)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--level", default="", help="only this difficulty (simple/moderate/challenging, easy/...)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--candidates", type=int, default=1)
    ap.add_argument("--style", choices=["exact", "friendly"], default="exact",
                    help="output style: exact (default, what benchmarks expect) or friendly (the UI's style)")
    ap.add_argument("--timeout", type=float, default=30, help="SQL time limit in seconds (BIRD has large tables)")
    ap.add_argument("--keep-policy", action="store_true", help="keep HIDDEN_COLUMNS defaults during the run")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    history.init()
    config.QUERY_TIMEOUT_S = args.timeout
    if not args.keep_policy:
        config.HIDDEN_COLUMNS = []
    cases = load_cases(args.format, Path(args.data))
    if args.level:
        cases = [c for c in cases if c["level"] == args.level]
    if args.limit and args.limit < len(cases):
        cases = random.Random(args.seed).sample(cases, args.limit)
    db_root = Path(args.db_root)
    sids = {db: register(db_root, db) for db in sorted({c["db_id"] for c in cases})}
    print(f"{len(cases)} questions on {len(sids)} databases; levels: {dict(Counter(c['level'] for c in cases))}")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sem = asyncio.Semaphore(args.concurrency)

    results = []
    for model in models:
        print(f"\n== {model}")
        tasks = [run_case(c, sids[c["db_id"]], model, sem, args.candidates, args.style) for c in cases]
        for n, coro in enumerate(asyncio.as_completed(tasks), 1):
            r = await coro
            results.append(r)
            if "skipped" in r:
                mark = "SKIP"
            else:
                mark = "PASS" if r["strict"] else ("LENIENT" if r["lenient"] else "FAIL")
            print(f"  [{n}/{len(tasks)}] {mark:<7} {r['db_id']:<24} {r['question'][:60].replace(chr(10), ' ')}")
            if args.verbose and mark in ("FAIL", "LENIENT"):
                print(f"       gold: {r['gold_sql']}\n       pred: {(r.get('sql') or r.get('error') or '').replace(chr(10), ' ')}")

    table = summarize(results, models)
    print("\n" + table)
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = f"{args.format}-{datetime.now():%Y%m%d-%H%M%S}"
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / f"{stamp}.md").write_text(table + "\n", encoding="utf-8")
    print(f"\nSaved evals/results/{stamp}.json")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
