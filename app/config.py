import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

OPENROUTER_KEY = os.getenv("OPENROUTER_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("LLM_API_KEY", "")
# Any OpenAI-compatible chat-completions endpoint works: OpenRouter (default), OpenAI, or a local server such as
# Ollama (http://localhost:11434/v1/chat/completions), vLLM or LM Studio. Local servers need no key.
OPENROUTER_URL = (os.getenv("LLM_URL") or os.getenv("OPENROUTER_URL")
                  or "https://openrouter.ai/api/v1/chat/completions")
IS_OPENROUTER = "openrouter.ai" in OPENROUTER_URL


def llm_ready() -> bool:
    """A key is mandatory for hosted providers; local OpenAI-compatible servers usually run without one."""
    return bool(OPENROUTER_KEY) or not IS_OPENROUTER

DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
AVAILABLE_MODELS = [
    m.strip()
    for m in os.getenv(
        "OPENROUTER_MODELS",
        "anthropic/claude-haiku-5.5,anthropic/claude-sonnet-5.5,openai/gpt-5.5,"
        "google/gemini-3.8-flash,deepseek/deepseek-v4.1-flash",
    ).split(",")
    if m.strip()
]
if DEFAULT_MODEL not in AVAILABLE_MODELS:
    AVAILABLE_MODELS.insert(0, DEFAULT_MODEL)

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
SOURCES_DIR = DATA_DIR / "sources"
APP_DB = DATA_DIR / "app.db"

MAX_ROWS = int(os.getenv("MAX_ROWS", "1000"))
QUERY_TIMEOUT_S = float(os.getenv("QUERY_TIMEOUT_S", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))
# Above these sizes, an extra LLM step picks the relevant tables before SQL generation.
SCHEMA_SELECT_MIN_TABLES = int(os.getenv("SCHEMA_SELECT_MIN_TABLES", "15"))
SCHEMA_SELECT_CHARS = int(os.getenv("SCHEMA_SELECT_CHARS", "16000"))

# Number of user-confirmed question/SQL examples (feedback "Correct") added to the prompt; 0 = off.
FEW_SHOT_K = int(os.getenv("FEW_SHOT_K", "3"))
# Repeated questions reuse the stored SQL (re-executed for fresh data) instead of calling the LLM again.
# Answers confirmed correct are reused regardless of age; others for CACHE_TTL_S seconds. 0 = off.
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "86400"))
# Self-consistency: generate this many candidate queries, run them all and keep the result most of them agree on.
# Costs N times more tokens on the first attempt; 1 = off. Requests may override it (1-5).
CANDIDATES = max(1, min(5, int(os.getenv("CANDIDATES", "1"))))

SOURCES_DIR.mkdir(parents=True, exist_ok=True)

# Security: set APP_PASSWORD to require a login. SECRET_KEY signs session cookies (auto-generated if unset).
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))  # LLM calls per IP per minute, 0 = off
# Comma-separated keys for programmatic access: send "Authorization: Bearer <key>" or "X-API-Key: <key>".
API_KEYS = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))  # 0 = keep forever
EXEC_RATE_LIMIT_PER_MIN = int(os.getenv("EXEC_RATE_LIMIT_PER_MIN", "60"))  # /api/execute per IP per minute, 0 = off
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "0"))  # stop LLM calls once today's spend reaches this, 0 = off
# Tables/columns hidden from the model and blocked in queries on every source (globs: table, table.column).
# Each source can add its own patterns in its settings. Set HIDDEN_COLUMNS= (empty) to disable the defaults.
HIDDEN_COLUMNS = [p.strip() for p in os.getenv(
    "HIDDEN_COLUMNS",
    "*.password*,*.passwd*,*.pwd,*.secret*,*.api_key*,*.apikey*,*.access_token*,*.refresh_token*,"
    "*.ssn,*.credit_card*,*.card_number*,*.cvv",
).split(",") if p.strip()]
