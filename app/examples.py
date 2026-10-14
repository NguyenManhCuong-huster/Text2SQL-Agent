"""Few-shot retrieval: question/SQL pairs that users confirmed as correct, ranked by similarity to a new question.

Similarity is a dependency-free bag of words (accents folded, so Vietnamese with and without diacritics match)
plus word bigrams, scored with an overlap coefficient. It is cheap enough to run on every question.
"""
import math
import re
import unicodedata

from . import config, history

_WORD = re.compile(r"\w+", re.U)
_STOP = {
    "the", "a", "an", "of", "in", "on", "for", "to", "by", "and", "or", "is", "are", "was", "were", "what", "which",
    "how", "many", "much", "show", "list", "me", "give", "find", "with", "per", "each", "all", "that", "this", "do",
    "does", "there", "be", "from", "as", "at", "it", "its", "their", "who", "whom",
    "cac", "nhung", "cua", "la", "co", "bao", "nhieu", "va", "cho", "trong", "theo", "moi", "nao", "gi", "duoc",
    "khong", "ra", "hay", "xem", "toi", "nhat",
}


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def features(text: str) -> set[str]:
    words = [w for w in _WORD.findall(fold(text)) if w not in _STOP]
    return set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


def find(source_id: str, question: str, k: int | None = None, min_score: float = 0.25) -> list[dict]:
    """Up to k confirmed examples most similar to the question, best first."""
    k = config.FEW_SHOT_K if k is None else k
    if k <= 0:
        return []
    q = features(question)
    scored = []
    seen = set()
    for ex in history.confirmed_examples(source_id, limit=1000):
        key = fold(ex["question"]).strip()
        if key in seen:  # newest confirmation of the same question wins
            continue
        seen.add(key)
        score = similarity(q, features(ex["question"]))
        if score >= min_score:
            scored.append((score, ex))
    scored.sort(key=lambda x: -x[0])
    return [dict(ex, score=round(score, 3)) for score, ex in scored[:k]]


def prompt_block(examples: list[dict]) -> str:
    parts = [f"Q: {ex['question']}\n```sql\n{ex['sql'].strip()}\n```" for ex in examples]
    return ("Verified examples on this database (confirmed correct by users). Reuse their logic, joins and "
            "filters when a question is similar:\n<examples>\n" + "\n\n".join(parts) + "\n</examples>\n\n")
