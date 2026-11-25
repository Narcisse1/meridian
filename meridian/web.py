"""HTTP API + single-page UI.

    meridian web                  # http://127.0.0.1:8000

POST /api/ask           {"question": "..."}   -> answer, SQL, chart spec, rows
POST /api/digest/run                          -> today's digest (n8n calls this every morning)
GET  /api/digest/latest | /api/history | /api/schema | /api/health

Set MERIDIAN_ACCESS_TOKEN on a public deployment: the owner sends it as X-Access-Token.
Visitors without it are guests (MERIDIAN_GUEST_ACCESS=off turns that off): they can ask a few
questions (MERIDIAN_GUEST_QUESTIONS_PER_VISITOR, default 5; MERIDIAN_GUEST_QUESTIONS_PER_DAY in
total, default 50) and read the latest digest, history and schema. Running the digest is owner-only.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .analyst import Analyst, semantic_layer
from .db import Database, PostgresDatabase, QueryError
from .digest import build_digest
from .llm import LLMError, StructuredLLM, make_llm, provider_ready, resolve_provider
from .state import State

STATIC = Path(__file__).resolve().parent / "static"

EXAMPLE_QUESTIONS = [
    "Which region had the highest failed transaction rate last week?",
    "Daily revenue for the last 30 days",
    "How many sign-ups did we get yesterday by acquisition channel?",
    "What are the top cancellation reasons in the last 14 days?",
    "Revenue by payment method this month",
    "Which plan has the highest churn rate over the last 30 days?",
]


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


class RateLimiter:
    """Global sliding-window cap on model-backed requests (protects API credits)."""

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit, self.window = limit, window_s
        self.hits: deque[float] = deque()
        self.lock = threading.Lock()

    def check(self) -> None:
        now = time.time()
        with self.lock:
            while self.hits and now - self.hits[0] > self.window:
                self.hits.popleft()
            if len(self.hits) >= self.limit:
                raise HTTPException(
                    429, "Too many questions right now. Try again in a few minutes."
                )
            self.hits.append(now)


Access = Literal["owner", "guest"]


class GuestQuota:
    """Counts guest questions per visitor and in total, per UTC day."""

    def __init__(self, per_visitor: int, per_day: int) -> None:
        self.per_visitor, self.per_day = per_visitor, per_day
        self._day = ""
        self._by_visitor: dict[str, int] = {}
        self._total = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._day:
            self._day, self._by_visitor, self._total = today, {}, 0

    def remaining(self, visitor: str) -> int:
        with self._lock:
            self._roll()
            left_visitor = self.per_visitor - self._by_visitor.get(visitor, 0)
            return max(0, min(left_visitor, self.per_day - self._total))

    def take(self, visitor: str) -> None:
        with self._lock:
            self._roll()
            if self._total >= self.per_day:
                raise HTTPException(
                    429, "Today's guest questions are used up. Please come back tomorrow."
                )
            if self._by_visitor.get(visitor, 0) >= self.per_visitor:
                raise HTTPException(
                    429,
                    f"Guests can ask {self.per_visitor} questions a day. Come back tomorrow, "
                    "or browse the Daily digest and Data dictionary pages.",
                )
            self._by_visitor[visitor] = self._by_visitor.get(visitor, 0) + 1
            self._total += 1

    def refund(self, visitor: str) -> None:
        """Give a question back when it failed for reasons outside the guest's control."""
        with self._lock:
            if self._by_visitor.get(visitor, 0) > 0:
                self._by_visitor[visitor] -= 1
                self._total = max(0, self._total - 1)


def visitor_id(request: Request) -> str:
    # Render and most hosts put the client address first in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(
    *,
    db: Database | None = None,
    llm_factory: Callable[[], StructuredLLM] | None = None,
    state: State | None = None,
    ask_limit: int = 30,
) -> FastAPI:
    app = FastAPI(title="Meridian", version="0.1.0")
    state = state or State(_env("MERIDIAN_STATE_DB") or "./data/meridian-state.db")
    new_llm = llm_factory or make_llm
    limiter = RateLimiter(ask_limit, 600)
    holder: dict[str, Database] = {}

    def get_db() -> Database:
        if db is not None:
            return db
        if "db" not in holder:
            try:
                holder["db"] = PostgresDatabase()
            except QueryError as exc:
                raise HTTPException(503, str(exc)) from exc
        return holder["db"]

    quota = GuestQuota(
        int(_env("MERIDIAN_GUEST_QUESTIONS_PER_VISITOR") or 5),
        int(_env("MERIDIAN_GUEST_QUESTIONS_PER_DAY") or 50),
    )

    def guests_allowed() -> bool:
        return (_env("MERIDIAN_GUEST_ACCESS") or "on").lower() not in ("off", "false", "0", "no")

    def access(
        x_access_token: str | None = Header(default=None), token: str | None = Query(default=None)
    ) -> Access:
        expected = _env("MERIDIAN_ACCESS_TOKEN")
        given = x_access_token or token or ""
        if not expected:
            return "owner"  # private/local install: no token configured
        if given:
            if secrets.compare_digest(given, expected):
                return "owner"
            raise HTTPException(401, "Wrong access token")
        if guests_allowed():
            return "guest"
        raise HTTPException(401, "Missing access token")

    def check_token(level: Access = Depends(access)) -> None:  # noqa: B008
        if level != "owner":
            raise HTTPException(401, "This action needs the owner's access token")

    now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: E731

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        provider = resolve_provider()
        token_set = bool(_env("MERIDIAN_ACCESS_TOKEN"))
        out: dict[str, Any] = {
            "llm_provider": provider,
            "llm_ready": provider_ready(provider) or llm_factory is not None,
            "token_required": token_set and not guests_allowed(),
            "guest_access": token_set and guests_allowed(),
            "guest_questions_left": quota.remaining(visitor_id(request)) if token_set else None,
            "database": False,
            "data_through": None,
        }
        try:
            rows = get_db().query("SELECT max(created_at)::date FROM transactions", max_rows=1).rows
            out["database"], out["data_through"] = True, str(rows[0][0])[:10] if rows else None
        except (HTTPException, QueryError):
            pass
        return out

    @app.get("/api/examples")
    def examples() -> list[str]:
        return EXAMPLE_QUESTIONS

    @app.post("/api/ask")
    def ask(req: AskRequest, request: Request, level: Access = Depends(access)) -> dict[str, Any]:  # noqa: B008
        limiter.check()
        visitor = visitor_id(request)
        if level == "guest":
            quota.take(visitor)
        try:
            result = Analyst(get_db(), new_llm()).ask(req.question).to_dict()
        except (QueryError, LLMError, HTTPException) as exc:
            if level == "guest":
                quota.refund(visitor)
            if isinstance(exc, QueryError):
                raise HTTPException(503, f"Database unavailable: {exc}") from exc
            if isinstance(exc, LLMError):
                raise HTTPException(502, f"AI model unavailable: {exc}") from exc
            raise
        state.add_question(now(), result)
        if level == "guest":
            result["guest_questions_left"] = quota.remaining(visitor)
        return result

    @app.get("/api/history", dependencies=[Depends(access)])
    def history(limit: int = 30) -> list[dict[str, Any]]:
        return [
            {k: q.get(k) for k in ("asked_at", "question", "answer", "ok", "sql", "elapsed_ms")}
            for q in state.questions(min(limit, 100))
        ]

    @app.post("/api/digest/run", dependencies=[Depends(check_token)])
    def run_digest() -> dict[str, Any]:
        try:
            llm = new_llm()
        except Exception:  # no model configured: the template brief still works
            llm = None
        try:
            digest = build_digest(get_db(), llm).to_dict()
        except QueryError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}") from exc
        state.add_digest(now(), digest)
        return {"created_at": now(), **digest}

    @app.get("/api/digest/latest", dependencies=[Depends(access)])
    def latest_digest() -> dict[str, Any]:
        digest = state.latest_digest()
        if digest is None:
            raise HTTPException(404, "No digest yet. Run one first.")
        return digest

    @app.get("/api/schema")
    def schema() -> dict[str, Any]:
        tables = get_db().schema()
        return {
            "tables": [
                {"name": t.name, "comment": t.comment, "rows": t.row_estimate,
                 "columns": [{"name": c.name, "type": c.type, "comment": c.comment} for c in t.columns]}
                for t in tables
            ],
            "definitions": [line[2:] for line in semantic_layer(tables).splitlines() if line.startswith("- ")],
        }  # fmt: skip

    return app


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port)
