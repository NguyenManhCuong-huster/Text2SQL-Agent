# Text2SQL Studio

Ask questions about a database in plain English or Vietnamese: a built-in sample, your own SQLite/CSV/JSON files, or a live **PostgreSQL / MySQL** database. The app writes the SQL with an LLM (OpenRouter, or any OpenAI-compatible server such as a local Ollama), runs it read-only, fixes the query itself if it fails, and shows the result as a table, a chart, and a short written answer.

It also plugs into other tools: a REST API with API keys, a one-file Python client and CLI, and an **MCP server** for Claude Desktop, Claude Code and other agents.

## Architecture

This is a monolith: one FastAPI process serves both the JSON API and the static web UI. The UI is vanilla JS with no build step.

```
browser ──► FastAPI (app/main.py)  ── auth middleware (optional password) + per-IP rate limit
              ├─ /                 static SPA (app/static: index.html, app.js, styles.css)
              ├─ /api/ask/stream   SSE progress ─┐
              ├─ /api/ask          plain JSON  ──┴─► pipeline.py ──► llm.py ──► OpenRouter
              │                                         └──────► datasources.py ─┬─► SQLite files (read-only)
              │                                                                  └─► connectors.py ──► PostgreSQL / MySQL
              ├─ /api/execute, /api/answer, /api/sources, /api/history, /api/login
              └─ history.py ──► /data/app.db  (history, cached suggestions)
```

| Module | Responsibility |
|---|---|
| `app/pipeline.py` | Builds the prompt (DDL, row counts, sample rows, and the allowed values of categorical columns), generates SQL, runs it, and retries with the error message when a query fails (up to `MAX_RETRIES`). When a query returns 0 rows, it asks the model to check its filters. For large schemas, a first LLM step picks the relevant tables, and their foreign-key neighbours are added. Progress is emitted as events, so the UI can stream the steps. |
| `app/datasources.py` | Manages data sources, reads their schemas, imports CSV/SQLite uploads, and runs queries safely. Every function takes a source id and dispatches to SQLite or to an external connector, so the rest of the app doesn't depend on the engine. |
| `app/connectors.py` | PostgreSQL and MySQL support: connecting, reading the schema (columns, PK/FK, row counts, samples, categorical values) and running guarded read-only queries. |
| `app/seed_external.py` | Copies the sample database into PostgreSQL or MySQL with real types (DATE, TIMESTAMP, NUMERIC, foreign keys). |
| `app/llm.py` | Async OpenRouter client. Records tokens and cost, and can cap hidden reasoning (`reasoning.effort`). |
| `app/auth.py` | Optional login with a single password (HMAC-signed cookie), API keys, and the rate limiters. |
| `app/policy.py` | Hidden tables/columns: removed from the schema the model sees, and enforced on every query. |
| `app/examples.py` | Finds answers that users confirmed as correct and are similar to a new question (few-shot examples). |
| `app/compare.py` | Compares result sets; used by cross-check voting and the benchmarks. |
| `app/audit.py`, `app/crypto.py` | Audit log of executed SQL, and encryption of stored connection passwords. |
| `clients/` | `text2sql_client.py` (Python client + CLI, standard library only) and `mcp_server.py` (MCP over stdio). |
| `app/history.py` | Query history (with stars and cost) and cached suggestions. |
| `app/sample_data.py` | Builds the deterministic `shop` e-commerce sample database (8 tables, ~24k rows, 2023–2025). |

### Query safety (defence in depth)
**SQLite:**
1. The database is opened with `mode=ro` and `PRAGMA query_only`.
2. A SQLite **authorizer** allows only `SELECT`/`READ`/`FUNCTION`/`RECURSIVE`, so writes hidden in a CTE, `ATTACH`, `PRAGMA` and `load_extension` are all denied.
3. Only one statement is allowed per request.
4. A progress handler stops queries that run longer than `QUERY_TIMEOUT_S`, and at most `MAX_ROWS` rows are returned.

**PostgreSQL / MySQL:**
1. **sqlglot** parses the SQL in the target dialect and requires exactly one statement with a query at the root. Anywhere in the tree it rejects DML/DDL (including writes inside CTEs), `SELECT … INTO`, `INTO OUTFILE`, `FOR UPDATE`, `SET`, `COPY` and `GRANT`. It also blocks dangerous functions: `pg_terminate_backend`, `pg_read_file`, `set_config`, `dblink`, `lo_*`, `LOAD_FILE`, `GET_LOCK` and others.
2. Every query runs in a **READ ONLY transaction** (`default_transaction_read_only`, `START TRANSACTION READ ONLY`) that is always rolled back. On Postgres it uses the extended protocol, so the server also rejects multiple statements.
3. A server-side timeout (`statement_timeout` / `MAX_EXECUTION_TIME`) and the row cap apply.
4. Even so, **connect with a user that only has SELECT rights**. Connection passwords are stored in `DATA_DIR/app.db` (mode 0600) on your machine.

These checks are verified live: with the superuser account, 16 of 16 attack queries were blocked on both engines and the data was left intact.

**On every engine:**
- **Hidden tables and columns.** `HIDDEN_COLUMNS` (by default `*.password*`, `*.secret*`, `*.api_key*`, `*.access_token*`, `*.ssn`, `*.card_number*`, …) plus each source's own list (**Settings** in the sidebar). Hidden objects are removed from the schema, sample rows, value hints, keys and DDL that the model sees. A sqlglot check expands `*` against the real schema and rejects any query that reads them. On SQLite the authorizer also denies them, and returns NULL when they are read through a view.
- **Prompt-injection hardening.** Values read from the database (samples, value hints, result rows) are kept on one line, length-capped, and stripped of anything that looks like a prompt fence. The schema and the results are fenced, and the model is told to treat their content as data only. Even a successful injection can only produce another read-only, policy-checked query.
- **Audit log.** Every statement run for a client (generated, retried or hand-written, including blocked ones) is stored with request id, client IP, status and timing: `GET /api/audit`.
- **Limits.** Per-IP limits on LLM calls (`RATE_LIMIT_PER_MIN`) and on hand-written SQL (`EXEC_RATE_LIMIT_PER_MIN`), and an optional daily LLM budget (`DAILY_BUDGET_USD`).
- **Secrets.** Connection passwords are stored Fernet-encrypted with a key derived from `SECRET_KEY` (or `DATA_DIR/.secret`). API keys are compared in constant time.

## Features
- Answers stream in step by step ("Writing SQL…", "Running query…", "Fixing the query…"), with a live SQL preview.
- Chat-style feed with follow-up questions: the last 4 question/SQL pairs are sent as context, so you can ask "now only Electronics, by quarter".
- SQL with syntax highlighting that you can edit and re-run (Ctrl+Enter), plus a copy button.
- Result table that sorts and paginates, with CSV export.
- Charts picked automatically (line for time series, bar, horizontal bar, doughnut, area, scatter). Long-format results such as quarter × category × revenue are **pivoted** into one series per category. Helper columns (ids, `quarter_number`, ranks) are not used as the Y axis. You can change the X, Y and Series columns, and a second axis appears when two different metrics have very different scales.
- A short written answer that summarises the result in the language of the question.
- Shows when a query was corrected automatically and what the failed attempts were. Each answer shows the token count and its cost in USD.
- Upload your own data: one `.sqlite`/`.db` file, or several CSV files (each one becomes a table, with types inferred).
- **Connect PostgreSQL or MySQL**, with a "Test connection" button and clear error messages (wrong password, unknown database, unreachable host). The prompt switches to the engine's SQL dialect, so the model writes `date_trunc`/`::numeric`/`percentile_cont` on Postgres and `DATE_FORMAT`/`YEAR()` on MySQL. A "Refresh" button re-reads the schema.
- Schema explorer with PK/FK badges, row counts and a filter.
- History drawer with stars, model switcher (or any custom OpenRouter model id), light/dark theme, and a mobile layout.
- Optional password login and a per-IP rate limit on LLM calls.
- **Getting more accurate with use:** mark an answer **Correct** or **Wrong**. Correct question/SQL pairs (including SQL you fixed by hand) are added as examples when someone asks a similar question. Matching works for Vietnamese with or without diacritics.
- **Business notes per data source** (Settings): metric definitions and jargon, for example "revenue excludes cancelled orders", are sent with every question.
- **Cross-check** toggle: the model writes 3 candidate queries, all of them run, and the answer most of them agree on wins. Disagreement is shown, as a sign that the question is ambiguous.
- **Repeated questions are free:** the stored SQL is run again for fresh data, with no LLM call. Use **Regenerate** to force new SQL.
- **Usage** dialog: answered rate, self-corrections, feedback, latency, LLM spend per model and per day, and the questions that fail most often.
- Upload JSON / JSON Lines as well as CSV and SQLite, and export results as CSV or JSON.

## Accuracy benchmark

`evals/shop.json` contains 36 questions on the sample DB, each with gold SQL: 8 easy, 9 medium, 9 hard and 10 expert. The expert questions cover top-N per group, running totals, a median (SQLite has no MEDIAN function), month-over-month change with LAG, cohorts and anti-joins. A few questions are in Vietnamese. Scoring is **execution accuracy**: a prediction is correct when its result matches the gold result. Column names and extra columns are ignored, row order counts only if the gold SQL has a top-level ORDER BY, and numbers are compared within a rounding tolerance.

```bash
python -m evals.run --models anthropic/claude-haiku-5.5,openai/gpt-5.5 --concurrency 8 -v
```

Results from 2026-10-12, run through the full pipeline:

| Model | Overall | easy | medium | hard | expert | Avg latency | Cost / question |
|---|---|---|---|---|---|---|---|
| `anthropic/claude-haiku-5.5` (default) | **100%** | 8/8 | 9/9 | 9/9 | 10/10 | 3.0s | $0.0005 |
| `anthropic/claude-sonnet-5.5` | **100%** | 8/8 | 9/9 | 9/9 | 10/10 | 3.2s | $0.0095 |
| `openai/gpt-5.5` | **100%** | 8/8 | 9/9 | 9/9 | 10/10 | 3.6s | $0.0078 |
| `google/gemini-3.8-flash` | **100%** | 8/8 | 9/9 | 9/9 | 10/10 | 5.6s | $0.0037 |
| `deepseek/deepseek-v4.1-flash` | **100%** | 8/8 | 9/9 | 9/9 | 10/10 | 2.4s | $0.0001 |

The same 36 questions asked against the **PostgreSQL and MySQL copies**. Gold answers come from SQLite, so this measures each SQL dialect:

| Model | SQLite | PostgreSQL 16 | MySQL 8.4 |
|---|---|---|---|
| `anthropic/claude-haiku-5.5` | 100% | 100% | 100% |
| `deepseek/deepseek-v4.1-flash` | 100% | 100% | 97% (returned 10 rows when asked for "the customer with the largest total") |

```bash
python -m evals.run --db-url postgresql://analyst:ropass@localhost:5432/shopdb --models anthropic/claude-haiku-5.5
```

**Public benchmarks (BIRD, Spider).** `evals/benchmark.py` runs them through the same pipeline. It reports the official strict execution accuracy (the predicted rows equal the gold rows as a set) next to the lenient score above, and keeps its databases in a separate `DATA_DIR` (`data/benchmarks/appdata`).
```bash
# BIRD dev: download https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip into data/benchmarks/bird, unzip it and its dev_databases.zip
python -m evals.benchmark --format bird --data data/benchmarks/bird/dev_20240627/dev.json \
    --db-root data/benchmarks/bird/dev_20240627/dev_databases --limit 200 --models anthropic/claude-haiku-5.5
```

First results: a random sample of 60 BIRD dev questions (seed 7), `anthropic/claude-haiku-5.5`, 2026-10-14:

| Setting | Strict EX | Lenient EX | simple | moderate | challenging | Cost / question |
|---|---|---|---|---|---|---|
| `style=friendly` (the UI's style) | 21.7% | 61.7% | 24.3% | 10.0% | 66.7% | $0.0007 |
| `style=exact` (benchmark default) | **68.3%** | 68.3% | 75.7% | 50.0% | 100.0% | $0.0007 |
| `style=exact`, `--candidates 3` | 68.3% | 66.7% | 75.7% | 55.0% | 66.7% | $0.0020 |

- The friendly style adds name/id columns, rounds numbers and adds `LIMIT 100`. That helps people, but strict matching fails on the extra columns. Programs that consume results should use `style: "exact"`.
- On this sample, voting did not improve accuracy and cost 3×. 60 questions are too few to judge, so run a larger sample before turning `CANDIDATES` on by default.
- Several remaining "failures" come from the known noise in BIRD's gold SQL. For example, "the highest LDH in the normal range" is ordered ASC, and "posts created by" counts `postHistory` rows.

What we learned:
- With clearly worded questions, current models get every question right, so the real trade-off between them is cost and latency.
- In the first run, every remaining failure came from **ambiguous wording**. One question said "which customers… count them", and another said "days between", which can mean whole calendar days or exact elapsed time. In one other case a model added a filter nobody asked for (`paid_at IS NOT NULL`). This is why the app always shows the assumptions it made next to the SQL.
- Engines return different types for the same answer: `strftime('%Y')` gives the text `'2023'`, while `EXTRACT`/`YEAR()` give the integer `2023`. The scorer now treats numeric strings as numbers.
- The scorer first rounded numbers to 1 decimal place, which failed correct answers whose values fell on a rounding boundary (1234.5501 vs `ROUND(x,2)`). It now compares numbers within a tolerance. `tests/test_evals.py` covers this case.

## Integrate it

**REST API with an API key.** Set `API_KEYS=key1,key2` and send `Authorization: Bearer key1` (or `X-API-Key`). With keys but no `APP_PASSWORD`, the deployment is API-only. Every response carries an `X-Request-ID`, which you can also set yourself and then find in the logs and the audit log.

```bash
curl -s localhost:8000/api/ask -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"source_id": "shop", "question": "Revenue by month in 2025"}'
```

**Python** (`clients/text2sql_client.py` is one file with no dependencies; copy it anywhere):
```python
from text2sql_client import Text2SQL
t2s = Text2SQL("http://localhost:8000", api_key="...")
out = t2s.ask("shop", "Top 5 customers by revenue")      # sql, explanation, result rows, id, cost
t2s.feedback(out["id"], correct=True)                      # becomes an example for similar questions
```
```bash
python clients/text2sql_client.py ask shop "Doanh thu theo tháng năm 2025"    # T2S_URL / T2S_API_KEY
```

**MCP (Claude Desktop, Claude Code, other agents).** The tools are `list_sources`, `get_schema`, `ask`, `run_sql` and `mark_answer`. Calls go through the HTTP API, so every safety check still applies.
```json
{"mcpServers": {"text2sql": {"command": "python", "args": ["/path/to/clients/mcp_server.py"],
                             "env": {"T2S_URL": "http://localhost:8000", "T2S_API_KEY": "..."}}}}
```

**Local or private LLMs.** Point `LLM_URL` at any OpenAI-compatible endpoint, for example `http://localhost:11434/v1/chat/completions` for Ollama, with `OPENROUTER_MODEL=qwen2.5-coder:7b`. No key is needed, and no data leaves the machine.

## Run with Docker (recommended)

```bash
cp .env.example .env          # then set OPENROUTER_KEY (and APP_PASSWORD if others can reach it)
docker compose up -d --build
# open http://localhost:8000
```

Data (uploads, history and the sample DB) is stored in the `text2sql-data` volume. The key is passed in at runtime through `env_file`; `.dockerignore` keeps `.env` out of the image. The container runs as a non-root user with a read-only root filesystem, `no-new-privileges` and a healthcheck.

Useful commands:
```bash
docker compose logs -f          # logs
docker compose down             # stop (keeps data)
docker compose down -v          # stop and delete data volume
HOST_PORT=9000 docker compose up -d   # different host port
```

Exposing the app publicly:
- Set `APP_PASSWORD`. Without it, anyone who can reach the app can use your OpenRouter credits.
- Put it behind a TLS reverse proxy (Caddy, nginx or Traefik), and set `FORWARDED_ALLOW_IPS` to the proxy's IP so the rate limiter sees the real client IPs.
- The session cookie gets the `Secure` flag automatically when the app is served over HTTPS.

### Demo: ask questions on a live PostgreSQL

```bash
docker compose --profile demo up -d        # app + demo-postgres (with a read-only "analyst" user)
docker compose run --rm text2sql python -m app.seed_external postgresql://postgres:demo@demo-postgres:5432/shopdb
```
In the app, click **+ Upload → Connect database** and enter PostgreSQL, host `demo-postgres`, port `5432`, database `shopdb`, user `analyst`, password `analyst`.

To use your own database running on this computer, enter host **`host.docker.internal`** (the app runs inside Docker). For a database elsewhere, use its normal hostname.

## Run locally (without Docker)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Windows (use .venv/bin/pip on Linux/macOS)
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
.venv/Scripts/python -m pytest -q                   # ~185 tests, offline (the LLM is mocked)
# optional live tests against seeded databases:
T2S_PG_URL=postgresql://... T2S_MYSQL_URL=mysql://... .venv/Scripts/python -m pytest -q tests/test_connectors.py
```

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `OPENROUTER_KEY` | — | Required. |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-5.5` | Default model. |
| `OPENROUTER_MODELS` | haiku-5.5, sonnet-5.5, gpt-5.5, gemini-3.8-flash, deepseek-v4.1-flash | Models listed in the UI dropdown. |
| `APP_PASSWORD` | *(empty = no login)* | Turns on the login screen. |
| `SECRET_KEY` | auto-generated in `DATA_DIR/.secret` | Key that signs session cookies. |
| `RATE_LIMIT_PER_MIN` | `20` | LLM requests per IP per minute, counted per worker (`0` turns it off). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For` header is trusted. |
| `LLM_URL` | OpenRouter | Any OpenAI-compatible chat-completions URL (Ollama, vLLM, LM Studio, OpenAI). Local servers need no key. |
| `API_KEYS` | *(empty)* | Comma-separated keys for programmatic access (`Authorization: Bearer …`). |
| `HIDDEN_COLUMNS` | passwords, secrets, tokens, SSN, card numbers | Globs (`table` or `table.column`) hidden from the model and blocked in queries; empty = none. |
| `EXEC_RATE_LIMIT_PER_MIN` | `60` | Hand-written SQL runs per IP per minute (`0` turns it off). |
| `DAILY_BUDGET_USD` | `0` (off) | Stop LLM calls once today's spend (UTC) reaches this. |
| `AUDIT_RETENTION_DAYS` | `90` | How long the audit log of executed SQL is kept (`0` = forever). |
| `FEW_SHOT_K` | `3` | Confirmed examples added to the prompt (`0` turns it off). |
| `CACHE_TTL_S` | `86400` | Reuse the SQL of a repeated question for this long (confirmed answers: always; `0` = off). |
| `CANDIDATES` | `1` | Candidate queries voted on per question (1–5). The UI's Cross-check uses 3. |
| `MAX_ROWS` | `1000` | Maximum rows returned per query. |
| `QUERY_TIMEOUT_S` | `10` | Time limit for each SQL execution. |
| `MAX_RETRIES` | `2` | Number of times the model may correct a failing query. |
| `MAX_UPLOAD_MB` | `50` | Maximum upload size. |
| `SCHEMA_SELECT_MIN_TABLES` / `SCHEMA_SELECT_CHARS` | `15` / `16000` | Above these sizes, the model first picks the relevant tables. |
| `WORKERS` | `2` | Uvicorn workers (Docker). |

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/ask/stream` | Server-sent events: `step` and `sql` progress events, then `result` (or `error`). |
| `POST` | `/api/ask` | `{source_id, question, model?, context?[], candidates?, no_cache?, style?: friendly|exact}` → SQL, explanation, result, attempts, tokens, cost, votes, cached |
| `POST` | `/api/execute` | `{source_id, sql}` → runs edited SQL |
| `POST` | `/api/answer` | Natural-language summary of a result |
| `GET/POST/DELETE` | `/api/sources[...]` | List, upload or delete data sources; `/schema?refresh=true`, `/suggestions` |
| `POST` | `/api/connections`, `/api/connections/test` | Add or test a PostgreSQL/MySQL connection |
| `GET/PATCH/DELETE` | `/api/history[...]` | History, stars and feedback (`{feedback: 1\|-1\|0, sql?}`) |
| `GET/PUT` | `/api/sources/{id}/settings` | Business notes and hidden tables/columns of a source |
| `GET` | `/api/stats?days=30` | Usage: outcomes, feedback, latency, LLM spend per model and day |
| `GET` | `/api/audit` | Executed SQL statements (`source_id`, `status`, `limit` filters) |
| `GET` | `/api/health`, `/api/ready` | Liveness; readiness (app store, sample DB, LLM key; 503 when degraded) |
| `GET` `POST` `POST` | `/api/auth`, `/api/login`, `/api/logout` | Login status, sign in, sign out |

Interactive docs are at `/docs`.
