"""Read-only PostgreSQL access: query execution and schema introspection.

Works with any PostgreSQL database. Configuration (environment variables):

    DATABASE_URL              connection string (use a read-only role, see db/readonly_role.sql)
    MERIDIAN_SCHEMA           schema to expose (default: public)
    MERIDIAN_TABLES           comma-separated allow-list (default: every table/view in the schema)
    MERIDIAN_HIDDEN_COLUMNS   sensitive columns never shown or queried,
                              e.g. "customers.email,customers.phone,users.password_hash"
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# The bundled demo database (db/schema.sql). Used for the demo's metric definitions and digest.
TABLES = ("regions", "customers", "subscriptions", "transactions")
DEMO_TABLES = TABLES
STATEMENT_TIMEOUT_MS = 8000
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _identifier(name: str, what: str) -> str:
    name = name.strip()
    if not _IDENT.match(name):
        raise QueryError(f"invalid {what} name {name!r}")
    return name


def parse_hidden_columns(spec: str | None) -> dict[str, set[str]]:
    """'customers.email, users.phone' -> {'customers': {'email'}, 'users': {'phone'}}."""
    hidden: dict[str, set[str]] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        if "." not in item:
            raise QueryError(f"MERIDIAN_HIDDEN_COLUMNS entry {item!r} must be table.column")
        table, column = (part.strip().lower() for part in item.rsplit(".", 1))
        hidden.setdefault(_identifier(table, "table"), set()).add(_identifier(column, "column"))
    return hidden


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

    def __init__(
        self,
        url: str | None = None,
        *,
        tables: tuple[str, ...] | None = None,
        schema_name: str | None = None,
        hidden_columns: Mapping[str, set[str]] | None = None,
    ) -> None:
        self.url = url or os.environ.get("DATABASE_URL", "")
        if not self.url:
            raise QueryError("DATABASE_URL is not set")
        if tables is None and os.environ.get("MERIDIAN_TABLES", "").strip():
            tables = tuple(t for t in os.environ["MERIDIAN_TABLES"].split(",") if t.strip())
        # None = discover every table and view in the schema
        self.tables = tuple(_identifier(t, "table").lower() for t in tables) if tables else None
        self.schema_name = _identifier(
            schema_name or os.environ.get("MERIDIAN_SCHEMA") or "public", "schema"
        )
        self.hidden_columns = (
            dict(hidden_columns)
            if hidden_columns is not None
            else parse_hidden_columns(os.environ.get("MERIDIAN_HIDDEN_COLUMNS"))
        )
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
        only = (
            "AND c.relname IN (" + ", ".join(f"'{t}'" for t in self.tables) + ")"
            if self.tables
            else ""
        )
        cols = self.query(
            f"""
            SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                   coalesce(col_description(c.oid, a.attnum), ''),
                   coalesce(obj_description(c.oid, 'pg_class'), ''),
                   greatest(c.reltuples, 0)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = '{self.schema_name}'
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind IN ('r', 'v', 'm', 'p') {only}
            ORDER BY c.relname, a.attnum
            """,
            max_rows=20000,
        )
        tables: dict[str, Table] = {}
        for rel, col, typ, col_comment, tbl_comment, estimate in cols.rows:
            table = tables.setdefault(
                rel, Table(name=rel, comment=tbl_comment, row_estimate=int(estimate))
            )
            if col.lower() in self.hidden_columns.get(rel.lower(), set()):
                continue  # sensitive: never shown to the model
            table.columns.append(Column(name=col, type=typ, comment=col_comment))
        order = self.tables or tuple(sorted(tables))
        ordered = [tables[t] for t in order if t in tables]
        self._schema_cache = (time.time(), ordered)
        return ordered


def guard_scope(db: Database) -> dict[str, Any]:
    """Arguments for sqlguard.check_sql that match what this database exposes."""
    return {
        "allowed_tables": [t.name for t in db.schema()],
        "schema": getattr(db, "schema_name", "public"),
        "hidden_columns": getattr(db, "hidden_columns", {}),
    }


def is_demo(tables: list[Table]) -> bool:
    return set(DEMO_TABLES) <= {t.name for t in tables}


def describe_schema(tables: list[Table]) -> str:
    """Compact text description of the schema for the model's system prompt."""
    lines = []
    for t in tables:
        lines.append(f"TABLE {t.name} (~{t.row_estimate:,} rows) -- {t.comment}".rstrip(" -"))
        for c in t.columns:
            note = f"  -- {c.comment}" if c.comment else ""
            lines.append(f"  {c.name} {c.type}{note}")
    return "\n".join(lines)
