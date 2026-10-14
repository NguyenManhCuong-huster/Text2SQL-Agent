import json

import pytest
from fastapi.testclient import TestClient

from app import auth, config
from app.main import app
from tests.conftest import sql_reply


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def sse_events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "Text2SQL Studio" in r.text
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200


def test_sources_and_schema(client):
    sources = client.get("/api/sources").json()
    assert sources[0]["id"] == "shop" and sources[0]["builtin"]
    schema = client.get("/api/sources/shop/schema").json()
    assert {t["name"] for t in schema["tables"]} >= {"orders", "customers", "products"}
    assert client.get("/api/sources/missing/schema").status_code == 404


def test_execute_endpoint(client):
    assert client.post("/api/execute", json={"source_id": "shop", "sql": "SELECT 1 AS x"}).json()["rows"] == [[1]]
    r = client.post("/api/execute", json={"source_id": "shop", "sql": "DROP TABLE orders"})
    assert r.status_code == 400 and "read-only" in r.json()["detail"]


def test_ask_records_history(client, fake_llm):
    fake_llm(sql_reply("SELECT COUNT(*) AS n FROM customers"))
    out = client.post("/api/ask", json={"source_id": "shop", "question": "How many customers?"}).json()
    assert out["status"] == "ok" and out["result"]["rows"] == [[500]]
    hist = client.get("/api/history", params={"source_id": "shop"}).json()
    item = next(h for h in hist if h["id"] == out["id"])
    assert item["question"] == "How many customers?" and item["row_count"] == 1 and item["cost"] == pytest.approx(0.0001)
    assert client.patch(f"/api/history/{item['id']}", json={"starred": True}).status_code == 200
    assert any(h["id"] == item["id"] for h in client.get("/api/history", params={"starred": "true"}).json())


def test_ask_stream(client, fake_llm):
    fake_llm(sql_reply("SELECT nope"), sql_reply("SELECT 2 AS two"))
    r = client.post("/api/ask/stream", json={"source_id": "shop", "question": "two?"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r)
    types = [e["type"] for e in events]
    assert types[0] == "step" and types[-1] == "result" and types.count("sql") == 2
    assert events[-1]["result"]["rows"] == [[2]] and events[-1]["id"] > 0


def test_ask_stream_llm_error(client, monkeypatch):
    from app import llm

    async def boom(*a, **k):
        raise llm.LLMError("OpenRouter error 402: insufficient credits")
    monkeypatch.setattr(llm, "chat", boom)
    events = sse_events(client.post("/api/ask/stream", json={"source_id": "shop", "question": "x"}))
    assert events[-1] == {"type": "error", "message": "OpenRouter error 402: insufficient credits"}


def test_ask_validation(client):
    assert client.post("/api/ask", json={"source_id": "shop", "question": ""}).status_code == 422
    assert client.post("/api/ask", json={"source_id": "shop", "question": "x" * 3000}).status_code == 422


def test_upload_csv_and_delete(client):
    files = [("files", ("people.csv", b"name,age\nAn,30\nBinh,25\n", "text/csv"))]
    sid = client.post("/api/sources", data={"name": "People"}, files=files).json()["id"]
    assert client.post("/api/execute", json={"source_id": sid, "sql": "SELECT SUM(age) FROM people"}).json()["rows"] == [[55]]
    assert client.delete(f"/api/sources/{sid}").json() == {"ok": True}
    assert client.delete("/api/sources/shop").status_code == 403


def test_upload_rejects_bad_files(client):
    r = client.post("/api/sources", files=[("files", ("x.exe", b"MZ...", "application/octet-stream"))])
    assert r.status_code == 400
    r = client.post("/api/sources", files=[("files", ("x.db", b"not sqlite", "application/octet-stream"))])
    assert r.status_code == 400


def test_password_protection(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "s3cret")
    client.cookies.clear()
    assert client.get("/api/auth").json() == {"required": True, "authenticated": False}
    assert client.get("/api/sources").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200  # login screen is served by the SPA
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200
    assert client.get("/api/sources").status_code == 200
    client.cookies.set(auth.COOKIE, "9999999999.forged")
    assert client.get("/api/sources").status_code == 401
    client.cookies.clear()


def test_rate_limit(client, monkeypatch, fake_llm):
    monkeypatch.setattr(auth, "llm_limiter", auth.RateLimiter(2))
    fake_llm(sql_reply("SELECT 1"), sql_reply("SELECT 1"))
    body = {"source_id": "shop", "question": "q"}
    assert client.post("/api/ask", json=body).status_code == 200
    assert client.post("/api/ask", json=body).status_code == 200
    r = client.post("/api/ask", json=body)
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_request_id_and_ready(client):
    r = client.get("/api/sources", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = client.get("/api/sources", headers={"X-Request-ID": "bad id!"})
    assert len(r.headers["x-request-id"]) == 16
    ready = client.get("/api/ready").json()
    assert ready["status"] == "ok" and ready["checks"]["sample_db"] == "ok"


def test_api_key_auth(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEYS", ["k-123", "k-456"])
    client.cookies.clear()
    assert client.get("/api/sources").status_code == 401
    assert client.get("/api/sources", headers={"Authorization": "Bearer k-456"}).status_code == 200
    assert client.get("/api/sources", headers={"X-API-Key": "k-123"}).status_code == 200
    assert client.get("/api/sources", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/auth", headers={"X-API-Key": "k-123"}).json()["authenticated"]
    # keys alone: password login is not available
    assert client.post("/api/login", json={"password": ""}).status_code == 401


def test_audit_log_records_every_statement(client, fake_llm):
    fake_llm(sql_reply("SELECT nope FROM orders"), sql_reply("SELECT 42 AS answer"))
    client.post("/api/ask/stream", json={"source_id": "shop", "question": "audit me"}, headers={"X-Request-ID": "aud-1"})
    client.post("/api/execute", json={"source_id": "shop", "sql": "DELETE FROM orders"}, headers={"X-Request-ID": "aud-2"})
    rows = client.get("/api/audit", params={"source_id": "shop"}).json()
    ask_rows = [r for r in rows if r["request_id"] == "aud-1"]
    assert [r["status"] for r in reversed(ask_rows)] == ["error", "ok"] and ask_rows[0]["kind"] == "ask"
    blocked = next(r for r in rows if r["request_id"] == "aud-2")
    assert blocked["kind"] == "execute" and blocked["status"] == "error" and "DELETE" in blocked["sql"]
    assert all(r["status"] == "error" for r in client.get("/api/audit", params={"status": "error"}).json())


def test_execute_rate_limit(client, monkeypatch):
    monkeypatch.setattr(auth, "exec_limiter", auth.RateLimiter(1))
    body = {"source_id": "shop", "sql": "SELECT 1"}
    assert client.post("/api/execute", json=body).status_code == 200
    assert client.post("/api/execute", json=body).status_code == 429


def test_daily_budget(client, monkeypatch, fake_llm):
    from app import history
    monkeypatch.setattr(config, "DAILY_BUDGET_USD", 0.5)
    history.add_llm_usage("m", 1, 1, 0.6)
    try:
        r = client.post("/api/ask", json={"source_id": "shop", "question": "q"})
        assert r.status_code == 429 and "budget" in r.json()["detail"]
        assert client.post("/api/execute", json={"source_id": "shop", "sql": "SELECT 1"}).status_code == 200
    finally:
        with history._conn() as c:
            c.execute("DELETE FROM llm_usage WHERE cost = 0.6")
