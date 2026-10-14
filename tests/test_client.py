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


def test_mcp_server(base_url, fake_llm):
    import io
    import json
    import mcp_server

    server = mcp_server.Server(t2s.Text2SQL(base_url))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    assert init["result"]["protocolVersion"] == "2024-11-05" and "tools" in init["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert names == {"list_sources", "get_schema", "ask", "run_sql", "mark_answer"}

    def call(name, **args):
        return server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": name, "arguments": args}})["result"]
    assert "shop" in call("list_sources")["content"][0]["text"]
    assert "customers" in call("get_schema", source_id="shop")["content"][0]["text"]
    fake_llm(sql_reply("SELECT 11 AS eleven", "Eleven."))
    text = call("ask", source_id="shop", question="mcp: eleven?")["content"][0]["text"]
    assert "Eleven." in text and "SELECT 11" in text and "history_id=" in text
    bad = call("run_sql", source_id="shop", sql="DELETE FROM orders")
    assert bad["isError"] and "read-only" in bad["content"][0]["text"]
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601

    # stdio framing: one JSON message per line
    out = io.StringIO()
    server.serve(io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\nnot json\n'), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 9, "result": {}} and replies[1]["error"]["code"] == -32700
