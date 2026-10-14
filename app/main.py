"""Text2SQL Studio - FastAPI monolith serving both the JSON API and the web UI."""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, Field

from . import auth, config, connectors, datasources, history, llm, pipeline, sample_data

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
log = logging.getLogger("text2sql")
STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_API = {"/api/health", "/api/auth", "/api/login", "/api/logout"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    history.init()
    sample_data.ensure_sample()
    if not config.OPENROUTER_KEY:
        log.warning("OPENROUTER_KEY is not set - question answering will fail.")
    if not auth.enabled():
        log.warning("APP_PASSWORD is not set - anyone who can reach this server can use it.")
    yield
    await llm.close()


app = FastAPI(title="Text2SQL Studio", version="1.1.0", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API:
        if auth.enabled() and not auth.valid_token(request.cookies.get(auth.COOKIE)):
            return JSONResponse(status_code=401, content={"detail": "Login required."})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.exception_handler(llm.LLMError)
async def llm_error_handler(_, exc: llm.LLMError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(datasources.QueryError)
async def source_error_handler(_, exc: datasources.QueryError):
    # Only reached when not handled locally, e.g. an external database is unreachable while reading its schema.
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request) -> None:
    ok, retry = auth.llm_limiter.allow(_client_ip(request))
    if not ok:
        raise HTTPException(429, f"Too many requests - try again in {retry}s.", headers={"Retry-After": str(retry)})


def _model(model: str | None) -> str:
    return (model or config.DEFAULT_MODEL).strip()


def _check_source(source_id: str) -> None:
    if not datasources.exists(source_id):
        raise HTTPException(404, f"Data source '{source_id}' not found.")


# ---------------------------------------------------------------- meta & auth

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/auth")
def auth_status(request: Request):
    return {"required": auth.enabled(),
            "authenticated": not auth.enabled() or auth.valid_token(request.cookies.get(auth.COOKIE))}


class LoginRequest(BaseModel):
    password: str = Field(max_length=500)


@app.post("/api/login")
def login(req: LoginRequest, request: Request, response: Response):
    if not auth.enabled():
        return {"ok": True}
    ok, retry = auth.login_limiter.allow(_client_ip(request))
    if not ok:
        raise HTTPException(429, f"Too many login attempts - try again in {retry}s.")
    if not auth.check_password(req.password):
        raise HTTPException(401, "Wrong password.")
    response.set_cookie(auth.COOKIE, auth.issue_token(), max_age=auth.SESSION_TTL_S, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/config")
def get_config():
    return {
        "default_model": config.DEFAULT_MODEL,
        "models": config.AVAILABLE_MODELS,
        "key_configured": bool(config.OPENROUTER_KEY),
        "max_rows": config.MAX_ROWS,
        "query_timeout_s": config.QUERY_TIMEOUT_S,
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "auth": auth.enabled(),
        "dialects": {k: {"label": v["label"], "port": v["port"]} for k, v in connectors.DIALECTS.items()},
    }


# ---------------------------------------------------------------- sources

@app.get("/api/sources")
def list_sources():
    return datasources.list_sources()


@app.get("/api/sources/{source_id}/schema")
def get_schema(source_id: str, refresh: bool = False):
    _check_source(source_id)
    try:
        return datasources.get_schema(source_id, refresh=refresh)
    except datasources.QueryError as e:  # external database unreachable
        raise HTTPException(502, str(e))


class ConnectionRequest(BaseModel):
    name: str = Field("", max_length=48)
    dialect: Literal["postgres", "mysql"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=128)
    password: str = Field("", max_length=512)
    db_schema: str = Field("", max_length=128, pattern=r"^[A-Za-z0-9_]*$")

    def info(self) -> connectors.ConnInfo:
        return connectors.ConnInfo(dialect=self.dialect, host=self.host.strip(), port=self.port,
                                   database=self.database.strip(), user=self.user.strip(),
                                   password=self.password, db_schema=self.db_schema.strip())


@app.post("/api/connections/test")
def test_connection(req: ConnectionRequest):
    try:
        meta = connectors.introspect(req.info())
    except connectors.ConnectorError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "version": meta["version"], "tables": len(meta["tables"])}


@app.post("/api/connections")
def add_connection(req: ConnectionRequest):
    try:
        source_id = datasources.add_connection(req.name.strip(), req.info())
    except connectors.ConnectorError as e:
        raise HTTPException(400, str(e))
    return {"id": source_id}


@app.get("/api/sources/{source_id}/suggestions")
async def suggestions(source_id: str, request: Request, model: str | None = None, refresh: bool = False):
    _check_source(source_id)
    if source_id == datasources.SAMPLE_ID and not refresh:
        return sample_data.SUGGESTIONS
    if not refresh:
        cached = history.get_suggestions(source_id)
        if cached:
            return cached
    _rate_limit(request)
    items = await pipeline.suggest(source_id, _model(model))
    if items:
        history.save_suggestions(source_id, items)
    return items


@app.post("/api/sources")
async def upload_source(name: str = Form(""), files: list[UploadFile] = File(...)):
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    payload = []
    for f in files:
        data = await f.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"{f.filename} exceeds {config.MAX_UPLOAD_MB} MB.")
        payload.append((f.filename or "upload", data))
    if not payload:
        raise HTTPException(400, "No files uploaded.")
    name = name.strip() or Path(payload[0][0]).stem
    exts = {Path(fn).suffix.lower() for fn, _ in payload}
    try:
        if exts <= {".csv", ".tsv", ".txt"}:
            source_id = datasources.import_csv_files(name, payload)
        elif len(payload) == 1 and exts <= {".sqlite", ".sqlite3", ".db"}:
            source_id = datasources.import_sqlite_file(name, payload[0][1])
        else:
            raise ValueError("Upload one .sqlite/.db file, or one or more .csv files.")
    except (ValueError, UnicodeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # malformed CSV etc.
        log.exception("upload failed")
        raise HTTPException(400, f"Import failed: {e}")
    return {"id": source_id}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    _check_source(source_id)
    try:
        datasources.delete_source(source_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    history.drop_source(source_id)
    return {"ok": True}


# ---------------------------------------------------------------- querying

class Turn(BaseModel):
    question: str = Field(max_length=2000)
    sql: str = Field(max_length=20000)


class AskRequest(BaseModel):
    source_id: str
    question: str = Field(min_length=1, max_length=2000)
    model: str | None = Field(None, max_length=200)
    context: list[Turn] = Field(default_factory=list, max_length=10)


def _record(req: AskRequest, model: str, out: dict) -> int:
    return history.add(
        source_id=req.source_id, question=req.question.strip(), sql=out["sql"],
        explanation=out["explanation"], model=model, status=out["status"], error=out["error"],
        row_count=out["result"]["row_count"] if out["result"] else None,
        attempts=len(out["attempts"]) + 1, duration_ms=out["duration_ms"], tokens=out["tokens"],
        cost=out.get("cost"),
    )


@app.post("/api/ask")
async def ask(req: AskRequest, request: Request):
    _check_source(req.source_id)
    _rate_limit(request)
    model = _model(req.model)
    out = await pipeline.ask(req.source_id, req.question.strip(), model, [t.model_dump() for t in req.context])
    out["id"] = _record(req, model, out)
    return out


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, request: Request):
    """Server-sent events: step / sql progress events, then a final 'result' (or 'error')."""
    _check_source(req.source_id)
    _rate_limit(request)
    model = _model(req.model)
    context = [t.model_dump() for t in req.context]

    async def events():
        try:
            async for event in pipeline.ask_events(req.source_id, req.question.strip(), model, context):
                if event["type"] == "result":
                    event["id"] = _record(req, model, event)
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except (llm.LLMError, datasources.QueryError) as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        except Exception:
            log.exception("ask_stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Internal server error.'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ExecuteRequest(BaseModel):
    source_id: str
    sql: str = Field(min_length=1, max_length=20000)


@app.post("/api/execute")
def execute(req: ExecuteRequest):
    _check_source(req.source_id)
    try:
        return datasources.execute(req.source_id, req.sql)
    except datasources.QueryError as e:
        raise HTTPException(400, str(e))


class AnswerRequest(BaseModel):
    question: str = Field(max_length=2000)
    sql: str = Field(max_length=20000)
    columns: list[str]
    rows: list[list] = Field(max_length=1000)
    row_count: int
    truncated: bool = False
    model: str | None = Field(None, max_length=200)


@app.post("/api/answer")
async def answer(req: AnswerRequest, request: Request):
    _rate_limit(request)
    text = await pipeline.answer(req.question, req.sql, req.columns, req.rows[:40],
                                 req.row_count, req.truncated, _model(req.model))
    return {"answer": text}


# ---------------------------------------------------------------- history

@app.get("/api/history")
def get_history(source_id: str | None = None, starred: bool = False, limit: int = 100):
    return history.list_(source_id, max(1, min(limit, 500)), starred)


class StarRequest(BaseModel):
    starred: bool


@app.patch("/api/history/{item_id}")
def star_history(item_id: int, req: StarRequest):
    history.set_starred(item_id, req.starred)
    return {"ok": True}


@app.delete("/api/history/{item_id}")
def delete_history(item_id: int):
    history.delete(item_id=item_id)
    return {"ok": True}


@app.delete("/api/history")
def clear_history(source_id: str):
    history.delete(source_id=source_id)
    return {"ok": True}


# ---------------------------------------------------------------- UI

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
