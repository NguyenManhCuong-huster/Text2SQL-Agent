"""Text-to-SQL pipeline: prompt -> generate -> execute -> self-correct on errors -> answer."""
import asyncio
import json
import re
import time

from . import config, datasources, llm

DIALECT_RULES = {
    "sqlite": ("SQLite",
               "- Use SQLite syntax and functions (strftime, date, julianday, substr, ifnull, CAST, ...). "
               "There is no YEAR(), MONTH(), DATE_TRUNC or ILIKE.\n"
               "- Quote identifiers containing spaces or special characters with double quotes."),
    "postgres": ("PostgreSQL",
                 "- Use PostgreSQL syntax and functions (date_trunc, EXTRACT, to_char, ILIKE, FILTER (WHERE ...), "
                 "interval arithmetic, ::casts).\n"
                 "- ROUND(x, 2) needs a numeric argument: write ROUND(x::numeric, 2). "
                 "Integer division truncates: cast to numeric for ratios and percentages.\n"
                 "- Quote identifiers containing uppercase letters, spaces or special characters with double quotes."),
    "mysql": ("MySQL 8",
              "- Use MySQL 8 syntax and functions (DATE_FORMAT, YEAR(), MONTH(), TIMESTAMPDIFF, DATEDIFF, IFNULL). "
              "CTEs and window functions are available; there is no FULL OUTER JOIN, ILIKE or :: casts.\n"
              "- Quote identifiers containing spaces or special characters with backticks."),
}

SYSTEM_PROMPT = """You are an expert data analyst who translates natural-language questions into {engine} SQL.

Rules:
- Write exactly ONE read-only query (SELECT or WITH ... SELECT). Never modify data.
- Use only tables and columns that exist in the schema.
{dialect_rules}
- Match text literals to the exact values shown in the schema hints. For fuzzy text search use LIKE (case-insensitive for ASCII).
- Give columns clear aliases (e.g. total_revenue). Round money and averages to 2 decimals.
- For "top N" / ranking questions use ORDER BY + LIMIT. For plain listings without a requested size, add LIMIT 100. Do not limit aggregates the user wants in full.
- Prefer human-readable output: join to show names instead of only IDs.
- For time series, order chronologically.
- If the question is ambiguous, pick the most reasonable interpretation and state your assumption.
- If the question cannot be answered with this schema, do not write SQL; reply with a line starting with `CANNOT_ANSWER:` followed by the reason.

Response format (always):
1. One or two short sentences explaining the approach and any assumption, written in the SAME language as the user's question.
2. The SQL in a single ```sql fenced code block.
"""

FIX_PROMPT = """The query failed when executed on {engine}:

ERROR: {error}

Fix the query. Keep the same response format (short explanation + one ```sql block)."""

EMPTY_PROMPT = """The query ran successfully but returned 0 rows. If this is likely due to a mistake (wrong literal values, wrong date format, overly strict join or filter), return a corrected query. If 0 rows is genuinely the right answer, return the same query unchanged. Keep the same response format."""

ANSWER_PROMPT = """Question: {question}

SQL used:
{sql}

Result ({row_count} rows{truncated}, showing up to {shown}):
{table}

Answer the question in 1-4 concise sentences in the SAME language as the question, citing the key numbers. Do not mention SQL. If the result is empty, say no matching data was found."""

SUGGEST_PROMPT = """Given this database schema, propose 8 diverse, interesting business questions a user could ask in natural language. Mix simple lookups, aggregations, joins, time trends and rankings. Return ONLY a JSON array of strings.

{schema}"""

TABLE_SELECT_PROMPT = """A database has these tables (with columns):

{listing}

Previous SQL in this conversation:
{prior}

Question: {question}

Return ONLY a JSON array with the names of the tables needed to answer the question (include join tables). Example: ["orders", "customers"]"""

_SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*\n?(.*?)```", re.S | re.I)


def parse_response(text: str) -> tuple[str | None, str, str | None]:
    """Returns (sql, explanation, cannot_answer_reason)."""
    m = re.search(r"CANNOT_ANSWER:\s*(.+)", text, re.S)
    blocks = _SQL_BLOCK.findall(text)
    if m and not blocks:
        return None, "", m.group(1).strip()
    if blocks:
        sql = blocks[-1].strip()
        explanation = _SQL_BLOCK.sub("", text).strip()
    else:
        # No fence: accept a bare query if the reply starts with one.
        stripped = text.strip()
        if re.match(r"^(select|with)\b", stripped, re.I):
            return stripped, "", None
        return None, "", stripped or "The model did not return a SQL query."
    explanation = re.sub(r"^\s*(\d\.|[-*])\s*", "", explanation, flags=re.M).strip()
    return sql or None, explanation, None


def build_messages(source_id: str, question: str, context: list[dict], tables: list[str] | None = None) -> list[dict]:
    schema = datasources.schema_prompt(source_id, tables=tables)
    engine, rules = DIALECT_RULES[datasources.dialect(source_id)]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(engine=engine, dialect_rules=rules)},
        {"role": "user", "content": f"Database schema:\n\n{schema}\n\nI will now ask questions about this database."},
        {"role": "assistant", "content": "Understood. Ask your question."},
    ]
    # Prior turns enable follow-ups like "now break that down by month".
    for turn in context[-4:]:
        if turn.get("question") and turn.get("sql"):
            messages.append({"role": "user", "content": turn["question"]})
            messages.append({"role": "assistant", "content": f"```sql\n{turn['sql']}\n```"})
    messages.append({"role": "user", "content": question})
    return messages


class _Usage:
    def __init__(self):
        self.tokens = 0
        self.cost = 0.0

    def add(self, reply: dict) -> dict:
        self.tokens += reply["prompt_tokens"] + reply["completion_tokens"]
        self.cost += reply.get("cost") or 0.0
        return reply


def needs_table_selection(source_id: str) -> bool:
    return (len(datasources.get_schema(source_id)["tables"]) > config.SCHEMA_SELECT_MIN_TABLES
            or len(datasources.schema_prompt(source_id)) > config.SCHEMA_SELECT_CHARS)


async def select_tables(source_id: str, question: str, context: list[dict], model: str,
                        usage: _Usage) -> list[str] | None:
    """For large schemas, ask the model which tables matter, then add FK neighbours. None = use all."""
    schema = datasources.get_schema(source_id)
    by_name = {t["name"]: t for t in schema["tables"]}
    listing = "\n".join(f"{t['name']}({', '.join(c['name'] for c in t['columns'])})" for t in schema["tables"])
    prior = "\n".join(t["sql"] for t in context[-4:] if t.get("sql"))
    reply = usage.add(await llm.chat(
        [{"role": "user", "content": TABLE_SELECT_PROMPT.format(
            listing=listing, question=question, prior=prior or "(none)")}],
        model, max_tokens=2000, reasoning_effort="low"))
    m = re.search(r"\[.*\]", reply["content"], re.S)
    try:
        picked = [n for n in json.loads(m.group(0)) if n in by_name] if m else []
    except json.JSONDecodeError:
        picked = []
    if not picked:
        return None
    # Keep tables used earlier in the conversation so follow-ups still work.
    picked += [n for n in by_name if re.search(rf"\b{re.escape(n.lower())}\b", prior.lower()) and n not in picked]
    neighbours = []
    for n in picked:
        neighbours += [f["ref_table"] for f in by_name[n]["foreign_keys"] if f["ref_table"] in by_name]
    return list(dict.fromkeys(picked + neighbours))


async def ask_events(source_id: str, question: str, model: str, context: list[dict]):
    """Async generator of progress events; the last one has type 'result' with the full outcome."""
    started = time.perf_counter()
    usage = _Usage()
    attempts: list[dict] = []
    tables = None
    result = sql = None
    explanation, error = "", None

    def finish(status: str, **kw) -> dict:
        out = {"type": "result", "status": status, "sql": sql, "explanation": explanation, "result": None,
               "error": None, "attempts": attempts, "model": model, "tokens": usage.tokens,
               "cost": round(usage.cost, 6), "tables_used": tables}
        out.update(kw)
        out["duration_ms"] = round((time.perf_counter() - started) * 1000)
        return out

    yield {"type": "step", "message": "Reading schema…"}
    if needs_table_selection(source_id):
        yield {"type": "step", "message": "Large schema – selecting relevant tables…"}
        tables = await select_tables(source_id, question, context, model, usage)
        if tables:
            yield {"type": "step", "message": f"Using tables: {', '.join(tables)}"}

    messages = build_messages(source_id, question, context, tables)
    retried_empty = False
    for attempt in range(config.MAX_RETRIES + 1):
        yield {"type": "step", "message": "Writing SQL…" if attempt == 0 else f"Fixing the query (attempt {attempt + 1})…"}
        reply = usage.add(await llm.chat(messages, model))
        sql, expl, cannot = parse_response(reply["content"])
        if expl:
            explanation = expl
        if cannot:
            sql = None
            yield finish("unanswerable", explanation=cannot)
            return
        messages.append({"role": "assistant", "content": reply["content"]})
        yield {"type": "sql", "sql": sql, "attempt": attempt + 1}
        yield {"type": "step", "message": "Running query…"}
        try:
            result = await asyncio.to_thread(datasources.execute, source_id, sql)
            error = None
        except datasources.QueryError as e:
            error = str(e)
            attempts.append({"sql": sql, "error": error})
            yield {"type": "step", "message": f"Query failed: {error}"}
            messages.append({"role": "user", "content": FIX_PROMPT.format(
                engine=DIALECT_RULES[datasources.dialect(source_id)][0], error=error)})
            continue
        if result["row_count"] == 0 and not retried_empty and attempt < config.MAX_RETRIES:
            retried_empty = True
            yield {"type": "step", "message": "No rows returned – double-checking filters…"}
            prev_sql, prev_result = sql, result
            messages.append({"role": "user", "content": EMPTY_PROMPT})
            reply = usage.add(await llm.chat(messages, model))
            new_sql, expl, _ = parse_response(reply["content"])
            if new_sql and datasources.clean_sql(new_sql) != datasources.clean_sql(prev_sql):
                try:
                    result = await asyncio.to_thread(datasources.execute, source_id, new_sql)
                    attempts.append({"sql": prev_sql, "error": "Returned 0 rows – the model revised its filters."})
                    sql = new_sql
                    if expl:
                        explanation = expl
                    yield {"type": "sql", "sql": sql, "attempt": attempt + 2}
                except datasources.QueryError:
                    sql, result = prev_sql, prev_result
        break

    if error:
        yield finish("error", error=error)
    else:
        yield finish("ok", result=result)


async def ask(source_id: str, question: str, model: str, context: list[dict]) -> dict:
    final = None
    async for event in ask_events(source_id, question, model, context):
        final = event
    final.pop("type", None)
    return final


def _table_text(columns: list[str], rows: list[list], limit: int = 40) -> str:
    lines = [" | ".join(columns)]
    for r in rows[:limit]:
        lines.append(" | ".join("NULL" if v is None else str(v)[:60] for v in r))
    return "\n".join(lines)


async def answer(question: str, sql: str, columns: list[str], rows: list[list],
                 row_count: int, truncated: bool, model: str) -> str:
    shown = min(len(rows), 40)
    prompt = ANSWER_PROMPT.format(
        question=question, sql=sql, row_count=row_count, truncated="+ (truncated)" if truncated else "",
        shown=shown, table=_table_text(columns, rows),
    )
    reply = await llm.chat([{"role": "user", "content": prompt}], model, temperature=0.2, max_tokens=2000,
                           reasoning_effort="low")
    return reply["content"].strip()


async def suggest(source_id: str, model: str) -> list[str]:
    schema = datasources.schema_prompt(source_id, max_chars=12000)
    reply = await llm.chat([{"role": "user", "content": SUGGEST_PROMPT.format(schema=schema)}],
                           model, temperature=0.7, max_tokens=3000, reasoning_effort="low")
    text = reply["content"]
    m = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except json.JSONDecodeError:
        items = []
    return [str(i) for i in items if isinstance(i, str)][:8]
