from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from meridian import http
from meridian.db import Column, QueryError, QueryResult, Table
from meridian.llm import UsageMeter

SCHEMA = [
    Table(
        "regions",
        "Sales regions.",
        [Column("id", "integer"), Column("code", "text"), Column("name", "text")],
        6,
    ),
    Table(
        "customers",
        "Merchant accounts.",
        [
            Column("id", "integer"),
            Column("region_id", "integer"),
            Column("signed_up_at", "timestamp with time zone"),
            Column("plan", "text"),
            Column("channel", "text"),
        ],
        6000,
    ),
    Table(
        "subscriptions",
        "Subscriptions.",
        [Column("id", "integer"), Column("canceled_at", "timestamp with time zone")],
        4000,
    ),
    Table(
        "transactions",
        "Payments.",
        [
            Column("id", "bigint"),
            Column("region_id", "integer"),
            Column("created_at", "timestamp with time zone"),
            Column("amount", "numeric(12,2)"),
            Column("status", "text"),
        ],
        120000,
    ),
]


class FakeLLM:
    """Routes structured() calls to handlers by schema; a handler may return an Exception."""

    def __init__(self, handlers: dict[type, Callable[[str, str], Any]]) -> None:
        self.handlers = handlers
        self.calls: list[tuple[str, str, str]] = []
        self.usage = UsageMeter()

    def structured(self, *, system, prompt, schema):
        self.calls.append((schema.__name__, system, prompt))
        out = self.handlers[schema](system, prompt)
        if isinstance(out, Exception):
            raise out
        return out


class FakeDB:
    """Answers queries by the first matching substring rule; records every SQL it receives."""

    def __init__(self, rules: list[tuple[str, QueryResult | Exception]]) -> None:
        self.rules = rules
        self.queries: list[str] = []

    def query(self, sql: str, *, max_rows: int = 500) -> QueryResult:
        self.queries.append(sql)
        for needle, outcome in self.rules:
            if needle in sql:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise QueryError(f"no fake result for: {sql[:80]}")

    def schema(self) -> list[Table]:
        return SCHEMA


def result(columns: list[str], rows: list[list[Any]]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, elapsed_ms=3)


DATA_THROUGH = ("max(created_at)::date", result(["max"], [["2026-11-23"]]))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http, "_sleep", lambda _s: None)
