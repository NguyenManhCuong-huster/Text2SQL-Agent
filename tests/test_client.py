"""The standalone client against a real HTTP server (uvicorn in a background thread)."""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app import config
from app.main import app
from tests.conftest import sql_reply

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "clients"))
import text2sql_client as t2s  # noqa: E402


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def test_client_end_to_end(base_url, fake_llm, monkeypatch, capsys):
    monkeypatch.setattr(config, "API_KEYS", ["cli-key"])
    with pytest.raises(t2s.Text2SQLError) as e:
        t2s.Text2SQL(base_url).sources()
    assert e.value.status == 401

    client = t2s.Text2SQL(base_url, api_key="cli-key")
    assert any(s["id"] == "shop" for s in client.sources())
    assert client.execute("shop", "SELECT 3 AS n")["rows"] == [[3]]
    fake_llm(sql_reply("SELECT COUNT(*) AS n FROM customers"))
    out = client.ask("shop", "client: how many customers?", no_cache=True)
    assert out["status"] == "ok" and out["result"]["rows"] == [[500]]
    client.feedback(out["id"], correct=True)
    with pytest.raises(t2s.Text2SQLError) as e:
        client.execute("shop", "DROP TABLE orders")
    assert e.value.status == 400 and "read-only" in e.value.detail

    assert t2s.main(["--url", base_url, "--key", "cli-key", "sql", "shop", "SELECT 1 AS one"]) == 0
    assert "one" in capsys.readouterr().out
    assert t2s.main(["--url", base_url, "--key", "wrong", "sources"]) == 2


def test_format_table():
    text = t2s.format_table(["a", "bb"], [[1, None], [22, "x"]])
    assert text.splitlines()[0].startswith("a ") and "NULL" in text
