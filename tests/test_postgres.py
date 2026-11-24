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
