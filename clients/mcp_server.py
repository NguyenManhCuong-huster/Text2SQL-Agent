"""MCP server for Text2SQL Studio: lets Claude Desktop, Claude Code and other MCP clients query your databases.

It speaks MCP over stdio (JSON-RPC 2.0, one message per line) and forwards every call to a running Text2SQL
Studio over HTTP, so authentication, read-only enforcement, hidden columns, rate limits, budget and the audit
log all still apply. Standard library only.

    T2S_URL=http://localhost:8000 T2S_API_KEY=... python clients/mcp_server.py

Claude Desktop (claude_desktop_config.json) / Claude Code (.mcp.json):
    {"mcpServers": {"text2sql": {"command": "python", "args": ["/path/to/clients/mcp_server.py"],
                                 "env": {"T2S_URL": "http://localhost:8000", "T2S_API_KEY": "..."}}}}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text2sql_client import Text2SQL, Text2SQLError, format_table  # noqa: E402

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "text2sql-studio", "version": "1.2.0"}

TOOLS = [
    {
        "name": "list_sources",
        "description": "List the databases (data sources) that can be queried, with their ids and engines.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_schema",
        "description": "Tables, columns (with primary/foreign keys) and row counts of a data source.",
        "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}}, "required": ["source_id"]},
    },
    {
        "name": "ask",
        "description": "Answer a question about a data source in natural language (English, Vietnamese, ...). "
                       "Text2SQL Studio writes the SQL, runs it read-only, repairs it if it fails and returns the "
                       "SQL, its assumptions and the result rows. Prefer this over writing SQL yourself.",
        "inputSchema": {"type": "object", "properties": {
            "source_id": {"type": "string"},
            "question": {"type": "string"},
            "cross_check": {"type": "boolean", "description": "vote across 3 candidate queries (slower, ~3x cost)"},
        }, "required": ["source_id", "question"]},
    },
    {
        "name": "run_sql",
        "description": "Run one read-only SELECT query on a data source and return the rows (max 1000).",
        "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "sql": {"type": "string"}},
                        "required": ["source_id", "sql"]},
    },
    {
        "name": "mark_answer",
        "description": "Record whether an answer from `ask` (by its history_id) was correct. Correct answers are "
                       "reused as examples for similar questions; pass corrected_sql if you fixed the query.",
        "inputSchema": {"type": "object", "properties": {
            "history_id": {"type": "integer"}, "correct": {"type": "boolean"}, "corrected_sql": {"type": "string"},
        }, "required": ["history_id", "correct"]},
    },
]


class Server:
    def __init__(self, client: Text2SQL):
        self.client = client

    # ------------------------------------------------------------ tools
    def call_tool(self, name: str, args: dict) -> str:
        c = self.client
        if name == "list_sources":
            return "\n".join(f"- {s['id']} ({s.get('kind', 'sqlite')}): {s['name']}" for s in c.sources())
        if name == "get_schema":
            lines = []
            for t in c.schema(args["source_id"])["tables"]:
                cols = ", ".join(f"{col['name']} {col['type']}".strip() + (" PK" if col.get("pk") else "")
                                 + (f" -> {col['fk']}" if col.get("fk") else "") for col in t["columns"])
                lines.append(f"{t['name']} ({t.get('row_count')} rows): {cols}")
            return "\n".join(lines)
        if name == "ask":
            out = c.ask(args["source_id"], args["question"], candidates=3 if args.get("cross_check") else None)
            parts = []
            if out.get("explanation"):
                parts.append(out["explanation"])
            if out.get("sql"):
                parts.append(f"```sql\n{out['sql']}\n```")
            if out["status"] == "ok":
                r = out["result"]
                parts.append(format_table(r["columns"], r["rows"], limit=100))
                parts.append(f"{r['row_count']} rows{' (truncated)' if r['truncated'] else ''}; history_id={out['id']}")
            elif out["status"] == "unanswerable":
                parts.append("The question cannot be answered from this database.")
            else:
                parts.append(f"Query failed: {out.get('error')}")
            return "\n\n".join(parts)
        if name == "run_sql":
            r = c.execute(args["source_id"], args["sql"])
            return format_table(r["columns"], r["rows"], limit=200) + f"\n\n{r['row_count']} rows" + \
                (" (truncated)" if r["truncated"] else "")
        if name == "mark_answer":
            c.feedback(int(args["history_id"]), bool(args["correct"]), args.get("corrected_sql"))
            return "Recorded."
        raise KeyError(name)

    # ------------------------------------------------------------ JSON-RPC
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification (e.g. notifications/initialized): no reply
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return _error(mid, -32602, f"Unknown tool: {name}")
                try:
                    text, is_error = self.call_tool(name, params.get("arguments") or {}), False
                except Text2SQLError as e:
                    text, is_error = f"Error {e.status}: {e.detail}", True
                except (OSError, KeyError, ValueError) as e:
                    text, is_error = f"Error: {e}", True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return _error(mid, -32601, f"Method not found: {method}")
        except Exception as e:  # noqa: BLE001 - never crash the stdio loop
            return _error(mid, -32603, str(e))
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply = _error(None, -32700, "Parse error")
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else _error(None, -32600, "Invalid request")
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    Server(Text2SQL(os.getenv("T2S_URL", "http://localhost:8000"), os.getenv("T2S_API_KEY"))).serve()
