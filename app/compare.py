"""Result-set comparison shared by self-consistency voting (pipeline) and the accuracy benchmarks (evals).

Two results agree when they have the same number of rows and every column of the first has a matching column
in the second: names and extra columns are ignored, numbers match within a rounding tolerance, and numeric
strings count as numbers (engines disagree: strftime('%Y') gives '2023', EXTRACT/YEAR() give 2023).
"""
import re

_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")


def norm(v):
    if isinstance(v, str):
        v = v.strip()
        if _NUMERIC.match(v):
            return float(v) if "." in v else int(v)
        return v
    if isinstance(v, bool):
        return int(v)
    return v


def same(a, b) -> bool:
    """Numbers match within rounding tolerance (models often ROUND(x, 2)); everything else exactly."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.01, 1e-4 * max(abs(a), abs(b)))
    return a == b


def _sort_key(v):
    return (v is None, not isinstance(v, (int, float)), v if isinstance(v, (int, float)) else str(v))


def _column(rows, i, ordered):
    col = [norm(r[i]) for r in rows]
    return col if ordered else sorted(col, key=_sort_key)


def results_match(gold: dict, pred: dict, ordered: bool) -> bool:
    """Every gold column is matched by a distinct predicted column (extra predicted columns are allowed)."""
    if gold["row_count"] != pred["row_count"]:
        return False
    pred_cols = [_column(pred["rows"], j, ordered) for j in range(len(pred["columns"]))]
    used = set()
    for i in range(len(gold["columns"])):
        g = _column(gold["rows"], i, ordered)
        match = next((j for j, c in enumerate(pred_cols)
                      if j not in used and all(same(x, y) for x, y in zip(g, c))), None)
        if match is None:
            return False
        used.add(match)
    return True


def agree(a: dict, b: dict) -> bool:
    """Same answer up to extra columns on either side (e.g. one query also returns an id or an email)."""
    if a["truncated"] != b["truncated"]:
        return False
    return results_match(a, b, ordered=False) or results_match(b, a, ordered=False)
