import json
from pathlib import Path

import pytest

from app import datasources
from evals.run import _is_ordered, results_match

CASES = json.loads((Path(__file__).parent.parent / "evals" / "shop.json").read_text(encoding="utf-8"))


def res(columns, rows):
    return {"columns": columns, "rows": rows, "row_count": len(rows)}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_gold_sql_runs_and_is_nonempty(case):
    r = datasources.execute("shop", case["sql"], max_rows=10000)
    assert r["row_count"] > 0 and not r["truncated"]


def test_match_ignores_column_names_order_and_extra_columns():
    gold = res(["a", "b"], [["x", 1.0], ["y", 2.004]])
    pred = res(["id", "total", "name"], [[7, 2.0, "y"], [8, 1, "x"]])
    assert results_match(gold, pred, ordered=False)


def test_match_respects_order_when_required():
    gold = res(["a"], [[1], [2]])
    assert not results_match(gold, res(["a"], [[2], [1]]), ordered=True)
    assert results_match(gold, res(["a"], [[2], [1]]), ordered=False)


def test_match_rejects_wrong_values_or_row_counts():
    gold = res(["a"], [[1], [2]])
    assert not results_match(gold, res(["a"], [[1], [3]]), ordered=False)
    assert not results_match(gold, res(["a"], [[1]]), ordered=False)


def test_is_ordered():
    assert _is_ordered("SELECT a FROM t ORDER BY a")
    assert not _is_ordered("SELECT a FROM t ORDER BY a DESC LIMIT 1")
    assert not _is_ordered("SELECT COUNT(*) FROM (SELECT a FROM t ORDER BY a)")


def test_match_tolerates_rounding_at_boundaries():
    gold = res(["rev"], [[1234.5501], [3.4449]])
    assert results_match(gold, res(["r"], [[1234.55], [3.44]]), ordered=True)
    assert not results_match(gold, res(["r"], [[1240.0], [3.44]]), ordered=True)


def test_match_is_lenient_about_numeric_types_across_engines():
    gold = res(["y", "n"], [["2023", 734], ["2024", 1781]])  # SQLite strftime returns text
    assert results_match(gold, res(["order_year", "orders"], [[2023, 734], [2024, 1781]]), ordered=True)
    assert not results_match(gold, res(["order_year", "orders"], [[2022, 734], [2024, 1781]]), ordered=True)
