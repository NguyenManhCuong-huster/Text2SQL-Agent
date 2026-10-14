import asyncio
import json
import sqlite3

from evals import benchmark
from tests.conftest import sql_reply


def _bench(tmp_path):
    db_dir = tmp_path / "dbs" / "zoo"
    db_dir.mkdir(parents=True)
    conn = sqlite3.connect(db_dir / "zoo.sqlite")
    conn.execute("CREATE TABLE animals (id INTEGER PRIMARY KEY, name TEXT, legs INTEGER)")
    conn.executemany("INSERT INTO animals (name, legs) VALUES (?, ?)", [("cat", 4), ("bird", 2), ("snake", 0)])
    conn.commit()
    conn.close()
    data = [
        {"question_id": 1, "db_id": "zoo", "question": "How many animals have legs?", "evidence": "legs > 0",
         "SQL": "SELECT COUNT(*) FROM animals WHERE legs > 0", "difficulty": "simple"},
        {"question_id": 2, "db_id": "zoo", "question": "Names of animals with 4 legs", "evidence": "",
         "SQL": "SELECT name FROM animals WHERE legs = 4", "difficulty": "moderate"},
    ]
    (tmp_path / "dev.json").write_text(json.dumps(data))
    return tmp_path / "dev.json", tmp_path / "dbs"


def test_bird_cases_and_scoring(tmp_path, fake_llm):
    data, root = _bench(tmp_path)
    cases = benchmark.load_cases("bird", data)
    assert cases[0]["question"].endswith("Hint: legs > 0") and cases[1]["question"] == "Names of animals with 4 legs"
    sid = benchmark.register(root, "zoo")
    assert sid == "bench_zoo"

    sem = asyncio.Semaphore(1)
    fake_llm(sql_reply("SELECT COUNT(*) FROM animals WHERE legs > 0"),
             sql_reply("SELECT name, legs FROM animals WHERE legs = 4"))  # extra column: lenient only
    r1 = asyncio.run(benchmark.run_case(cases[0], sid, "m", sem, 1))
    r2 = asyncio.run(benchmark.run_case(cases[1], sid, "m", sem, 1))
    assert r1["strict"] and r1["lenient"]
    assert not r2["strict"] and r2["lenient"]
    table = benchmark.summarize([r1, r2], ["m"])
    assert "| `m` | 2 | **50.0%** | 100.0% |" in table


def test_gold_failure_is_skipped(tmp_path, fake_llm):
    data, root = _bench(tmp_path)
    case = {**benchmark.load_cases("bird", data)[0], "sql": "SELECT nope FROM animals"}
    r = asyncio.run(benchmark.run_case(case, benchmark.register(root, "zoo"), "m", asyncio.Semaphore(1), 1))
    assert "skipped" in r
