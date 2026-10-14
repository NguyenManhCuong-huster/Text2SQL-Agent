import os
import sys
import tempfile
from pathlib import Path

# Isolate all app state in a temp dir before the app modules read their config.
_tmp = tempfile.mkdtemp(prefix="t2s-test-")
os.environ["DATA_DIR"] = _tmp
os.environ["OPENROUTER_KEY"] = "test-key"
os.environ["APP_PASSWORD"] = ""
os.environ["RATE_LIMIT_PER_MIN"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import config, llm, sample_data  # noqa: E402

config.OPENROUTER_KEY = "test-key"
config.APP_PASSWORD = ""


class FakeLLM:
    """Replays scripted replies and records the prompts it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, messages, model, **kw):
        self.calls.append(list(messages))  # snapshot: the pipeline keeps appending
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        content = self.replies.pop(0)
        return {"content": content, "model": model, "prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001}


@pytest.fixture
def fake_llm(monkeypatch):
    def install(*replies):
        fake = FakeLLM(replies)
        monkeypatch.setattr(llm, "chat", fake)
        return fake
    return install


@pytest.fixture(scope="session", autouse=True)
def sample_db():
    sample_data.ensure_sample()


def sql_reply(sql: str, text: str = "Approach.") -> str:
    return f"{text}\n```sql\n{sql}\n```"
