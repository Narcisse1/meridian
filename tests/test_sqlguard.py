from __future__ import annotations

import pytest

from meridian.db import TABLES
from meridian.sqlguard import UnsafeQueryError, check_sql


def ok(sql: str, **kw):
    return check_sql(sql, TABLES, **kw)


def rejected(sql: str) -> str:
    with pytest.raises(UnsafeQueryError) as exc:
        check_sql(sql, TABLES)
    return str(exc.value)


# --- allowed ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM transactions",
        "select r.name, sum(t.amount) as revenue from transactions t join regions r on r.id = t.region_id "
        "where t.status = 'succeeded' group by 1 order by 2 desc",
        "WITH daily AS (SELECT created_at::date AS day, count(*) AS n FROM transactions GROUP BY 1) "
        "SELECT * FROM daily ORDER BY day",
        "SELECT plan FROM customers UNION ALL SELECT plan FROM subscriptions",
        "SELECT date_trunc('week', created_at) AS week, avg((status = 'failed')::int) FROM transactions GROUP BY 1",
        "SELECT d::date FROM generate_series(DATE '2026-11-01', DATE '2026-11-07', interval '1 day') AS d",
        "SELECT * FROM public.regions;",
        "SELECT name FROM regions WHERE code IN (SELECT 'WAF')",
    ],
)
def test_read_queries_are_allowed(sql):
    checked = ok(sql)
    assert checked.sql.startswith("SELECT * FROM (") and checked.sql.endswith("LIMIT 500")


def test_row_limit_is_always_applied_outside():
    checked = ok("SELECT * FROM transactions LIMIT 100000", row_limit=50)
    assert checked.sql.endswith("AS meridian_q LIMIT 50")
    assert checked.tables == {"transactions"}


def test_cte_names_are_not_mistaken_for_tables():
    assert ok("WITH x AS (SELECT 1 AS a) SELECT a FROM x").tables == frozenset()


# --- rejected ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,needle",
    [
        ("DELETE FROM transactions", "only SELECT"),
        ("UPDATE customers SET plan = 'scale'", "only SELECT"),
        ("INSERT INTO regions (code, name) VALUES ('X', 'Y')", "only SELECT"),
        ("DROP TABLE transactions", "only SELECT"),
        ("TRUNCATE transactions", ""),
        ("CREATE TABLE x AS SELECT * FROM transactions", ""),
        ("ALTER TABLE regions ADD COLUMN x int", ""),
        ("GRANT ALL ON transactions TO public", ""),
        ("SELECT 1; DROP TABLE regions", "single statement"),
        ("SELECT * FROM regions; SELECT * FROM customers", "single statement"),
        ("WITH gone AS (DELETE FROM transactions RETURNING *) SELECT count(*) FROM gone", "DELETE"),
        ("SELECT * INTO backup FROM transactions", "INTO"),
        ("SELECT * FROM regions FOR UPDATE", "lock"),
        ("SELECT pg_sleep(30)", "pg_sleep"),
        ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
        ("SELECT current_setting('server_version')", "current_setting"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT * FROM dblink('host=evil', 'select 1') AS t(a int)", "dblink"),
        ("SELECT * FROM pg_catalog.pg_authid", "schema"),
        ("SELECT * FROM information_schema.tables", "schema"),
        ("SELECT * FROM pg_user", "not available"),
        ("SELECT * FROM secrets", "not available"),
        ("COPY transactions TO '/tmp/x.csv'", ""),
        ("SET statement_timeout = 0", ""),
        ("", "empty"),
        ("   ;  ", "empty"),
        ("SELEC broken FROM", ""),
    ],
)
def test_unsafe_queries_are_rejected(sql, needle):
    message = rejected(sql)
    assert needle.lower() in message.lower()


def test_error_lists_available_tables_for_repair():
    message = rejected("SELECT * FROM payments")
    assert "customers" in message and "transactions" in message
