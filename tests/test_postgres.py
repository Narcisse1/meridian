"""Integration tests against a real PostgreSQL (CI starts one; locally set TEST_DATABASE_URL).

They load db/schema.sql + db/seed.sql, then check that read-only execution really blocks writes
and that the digest finds the anomalies planted in the seed data.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian.db import PostgresDatabase, QueryError
from meridian.digest import build_digest
from meridian.sqlguard import check_sql

URL = os.environ.get("TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parent.parent
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")]


@pytest.fixture(scope="module")
def db():
    import psycopg

    with psycopg.connect(URL, autocommit=True) as conn:
        for name in ("schema.sql", "seed.sql"):
            conn.execute((ROOT / "db" / name).read_text(encoding="utf-8"))
    return PostgresDatabase(URL)


def test_schema_introspection_includes_comments(db):
    tables = {t.name: t for t in db.schema()}
    assert set(tables) == {"regions", "customers", "subscriptions", "transactions"}
    status = next(c for c in tables["transactions"].columns if c.name == "status")
    assert "succeeded" in status.comment


def test_writes_are_blocked_even_if_the_guard_is_bypassed(db):
    with pytest.raises(QueryError, match="read-only"):
        db.query("INSERT INTO regions (code, name) VALUES ('ZZZ', 'Nowhere')")
    with pytest.raises(QueryError):
        db.query("WITH x AS (DELETE FROM regions RETURNING *) SELECT count(*) FROM x")
    assert db.query("SELECT count(*) FROM regions").rows == [[6]]


def test_guarded_query_runs(db):
    checked = check_sql(
        "SELECT r.name, count(*) AS n FROM transactions t JOIN regions r ON r.id = t.region_id GROUP BY 1",
        ["regions", "transactions"],
    )
    out = db.query(checked.sql)
    assert len(out.rows) == 6 and all(isinstance(r[1], int) for r in out.rows)


def test_digest_finds_planted_anomalies(db):
    digest = build_digest(db, llm=None)
    flagged = {m.key for m in digest.anomalies}
    assert "failed_rate" in flagged and "signups" in flagged
    assert digest.regions[0].region == "West Africa"
    assert digest.top_failure_reasons[0]["reason"] == "issuer_unavailable"


def test_hidden_columns_and_allow_list_on_a_real_database():
    from meridian.db import guard_scope
    from meridian.sqlguard import UnsafeQueryError

    db = PostgresDatabase(
        URL, tables=("customers", "regions"), hidden_columns={"customers": {"channel"}}
    )
    tables = {t.name: t for t in db.schema()}
    assert list(tables) == ["customers", "regions"]
    assert "channel" not in {c.name for c in tables["customers"].columns}
    scope = guard_scope(db)
    with pytest.raises(UnsafeQueryError, match="restricted"):
        check_sql("SELECT channel, count(*) FROM customers GROUP BY 1", **scope)
    with pytest.raises(UnsafeQueryError, match="not available"):
        check_sql("SELECT count(*) FROM transactions", **scope)
    ok = check_sql("SELECT plan, count(*) AS n FROM customers GROUP BY 1", **scope)
    assert len(db.query(ok.sql).rows) >= 2


def test_mcp_tools_on_a_real_database():
    import asyncio
    import json

    from meridian.mcp_server import build_server

    server = build_server(PostgresDatabase(URL, hidden_columns={"customers": {"channel"}}))

    def call(tool, **args):
        out = asyncio.run(server.call_tool(tool, args))
        out = getattr(out, "content", out)
        blocks = out[0] if isinstance(out, tuple) else out
        return "".join(getattr(b, "text", "") for b in blocks)

    assert "transactions (~" in call("list_tables")
    sample = json.loads(call("sample_rows", table="customers", limit=3))
    assert sample["row_count"] == 3 and "channel" not in sample["columns"]
    rows = json.loads(call("run_query", sql="SELECT count(*) AS n FROM regions"))["rows"]
    assert rows == [{"n": 6}]
