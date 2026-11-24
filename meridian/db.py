"""Read-only PostgreSQL access: query execution and schema introspection."""

from __future__ import annotations

import datetime as dt
import decimal
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

TABLES = ("regions", "customers", "subscriptions", "transactions")
STATEMENT_TIMEOUT_MS = 8000


class QueryError(RuntimeError):
    """The database rejected or failed the query; message is safe to show the model."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: int
    truncated: bool = False


@dataclass
class Column:
    name: str
    type: str
    comment: str = ""


@dataclass
class Table:
    name: str
    comment: str = ""
    columns: list[Column] = field(default_factory=list)
    row_estimate: int = 0


class Database(Protocol):
    def query(self, sql: str, *, max_rows: int = 500) -> QueryResult: ...

    def schema(self) -> list[Table]: ...


def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return value


class PostgresDatabase:
    """Every query runs in its own READ ONLY transaction with a statement timeout.

    Connect as a role that only has SELECT grants (db/readonly_role.sql) for a third layer.
    """

    def __init__(self, url: str | None = None, *, tables: tuple[str, ...] = TABLES) -> None:
        self.url = url or os.environ.get("DATABASE_URL", "")
        if not self.url:
            raise QueryError("DATABASE_URL is not set")
        self.tables = tables
        self._schema_cache: tuple[float, list[Table]] | None = None

    def _connect(self):
        import psycopg

        return psycopg.connect(self.url, autocommit=False, connect_timeout=15)

    def query(self, sql: str, *, max_rows: int = 500) -> QueryResult:
        import psycopg

        started = time.perf_counter()
        try:
            with self._connect() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
                with conn.cursor() as cur:
                    cur.execute(sql)
                    columns = [d.name for d in cur.description or []]
                    fetched = cur.fetchmany(max_rows + 1)
                conn.rollback()  # nothing to keep: read-only by construction
        except psycopg.errors.QueryCanceled as exc:
            raise QueryError(
                f"query exceeded the {STATEMENT_TIMEOUT_MS // 1000}s time limit"
            ) from exc
        except psycopg.Error as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise QueryError(first) from exc
        rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
        return QueryResult(
            columns=columns,
            rows=rows,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            truncated=len(fetched) > max_rows,
        )

    def schema(self) -> list[Table]:
        if self._schema_cache and time.time() - self._schema_cache[0] < 600:
            return self._schema_cache[1]
        names = ", ".join(f"'{t}'" for t in self.tables)
        cols = self.query(
            f"""
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   coalesce(col_description(c.oid, a.attnum), ''),
                   coalesce(obj_description(c.oid, 'pg_class'), ''),
                   greatest(c.reltuples, 0)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relname IN ({names})
            ORDER BY c.relname, a.attnum
            """,
            max_rows=2000,
        )
        tables: dict[str, Table] = {}
        for rel, col, typ, col_comment, tbl_comment, estimate in cols.rows:
            table = tables.setdefault(
                rel, Table(name=rel, comment=tbl_comment, row_estimate=int(estimate))
            )
            table.columns.append(Column(name=col, type=typ, comment=col_comment))
        ordered = [tables[t] for t in self.tables if t in tables]
        self._schema_cache = (time.time(), ordered)
        return ordered


def describe_schema(tables: list[Table]) -> str:
    """Compact text description of the schema for the model's system prompt."""
    lines = []
    for t in tables:
        lines.append(f"TABLE {t.name} (~{t.row_estimate:,} rows) -- {t.comment}".rstrip(" -"))
        for c in t.columns:
            note = f"  -- {c.comment}" if c.comment else ""
            lines.append(f"  {c.name} {c.type}{note}")
    return "\n".join(lines)
