"""Meridian on a database other than the demo: discovery scope, sensitive columns, MCP tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FakeDB, result

from meridian.analyst import SEMANTIC_LAYER, analyst_system, semantic_layer
from meridian.db import Column, QueryError, Table, guard_scope, is_demo, parse_hidden_columns
from meridian.sqlguard import UnsafeQueryError, check_sql

SHOP = [
    Table(
        "orders",
        "One row per order.",
        [Column("id", "bigint"), Column("customer_id", "bigint"), Column("total", "numeric")],
        5000,
    ),
    Table("customers", "Shop customers.", [Column("id", "bigint"), Column("country", "text")], 900),
]
HIDDEN = {"customers": {"email", "phone"}}


class ShopDB(FakeDB):
    schema_name = "shop"
    hidden_columns = HIDDEN

    def schema(self):
        return SHOP


def check(sql: str):
    return check_sql(sql, ["orders", "customers"], schema="shop", hidden_columns=HIDDEN)


# --- sensitive columns ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT email FROM customers",
        "SELECT c.phone, o.total FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT * FROM customers",
        "SELECT c.* FROM customers c",
        "SELECT count(*) FROM customers WHERE email LIKE '%@gmail.com'",
        "WITH x AS (SELECT email FROM customers) SELECT count(*) FROM x",
        "SELECT lower(EMAIL) FROM customers",
    ],
)
def test_restricted_columns_are_refused(sql):
    with pytest.raises(UnsafeQueryError, match="restricted|SELECT \\*"):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT country, count(*) FROM customers GROUP BY 1",
        "SELECT * FROM orders",  # no restricted columns on orders
        "SELECT o.total, c.country FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT count(*) FROM shop.customers",
    ],
)
def test_other_columns_still_work(sql):
    assert check(sql).sql.endswith("LIMIT 500")


def test_schema_scope():
    with pytest.raises(UnsafeQueryError, match="schema 'public'"):
        check("SELECT * FROM public.orders")
    with pytest.raises(UnsafeQueryError, match="not available"):
        check("SELECT * FROM payroll")


def test_parse_hidden_columns():
    assert parse_hidden_columns(" customers.email, Customers.Phone ,users.password_hash") == {
        "customers": {"email", "phone"},
        "users": {"password_hash"},
    }
    assert parse_hidden_columns("") == {}
    with pytest.raises(QueryError, match="table.column"):
        parse_hidden_columns("email")
    with pytest.raises(QueryError, match="invalid"):
        parse_hidden_columns("customers.email; drop table x")


# --- prompts and definitions ----------------------------------------------------------------


def test_definitions_follow_the_database(tmp_path, monkeypatch):
    monkeypatch.delenv("MERIDIAN_DEFINITIONS", raising=False)
    assert not is_demo(SHOP) and semantic_layer(SHOP) == ""
    from conftest import SCHEMA

    assert semantic_layer(SCHEMA) == SEMANTIC_LAYER
    defs = tmp_path / "definitions.md"
    defs.write_text("- Revenue = SUM(orders.total)", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_DEFINITIONS", str(defs))
    assert "Revenue = SUM(orders.total)" in semantic_layer(SHOP)


def test_prompt_without_a_date_column_uses_current_date():
    system = analyst_system("TABLE orders", "unknown", "", "shop")
    assert "CURRENT_DATE" in system and "complete through" not in system
    assert "schema shop" in system


def test_guard_scope_reads_the_database():
    scope = guard_scope(ShopDB([]))
    assert scope == {
        "allowed_tables": ["orders", "customers"],
        "schema": "shop",
        "hidden_columns": HIDDEN,
    }


# --- MCP tools ------------------------------------------------------------------------------


def call(server, tool: str, **args) -> str:
    out = asyncio.run(server.call_tool(tool, args))
    if hasattr(out, "content"):  # mcp 2.x CallToolResult
        out = out.content
    blocks = out[0] if isinstance(out, tuple) else out
    return "".join(getattr(b, "text", "") for b in blocks)


@pytest.fixture
def server():
    from meridian.mcp_server import build_server

    db = ShopDB(
        [
            ("meridian_q", result(["country", "orders"], [["CM", 41], ["NG", 17]])),
        ]
    )
    srv = build_server(db)
    srv._test_db = db
    return srv


def test_mcp_lists_and_describes_without_sensitive_columns(server):
    assert call(server, "list_tables").splitlines()[0] == "orders (~5,000 rows): One row per order."
    described = call(server, "describe_table", name="customers")
    assert (
        "- country (text)" in described
        and "Restricted columns (not available): email, phone" in described
    )
    assert "Unknown table" in call(server, "describe_table", name="payroll")


def test_mcp_run_query_is_guarded(server):
    ok = json.loads(
        call(
            server, "run_query", sql="SELECT country, count(*) AS orders FROM customers GROUP BY 1"
        )
    )
    assert ok["rows"][0] == {"country": "CM", "orders": 41} and ok["row_count"] == 2
    assert call(server, "run_query", sql="DELETE FROM orders").startswith("Rejected: only SELECT")
    assert "restricted" in call(server, "run_query", sql="SELECT email FROM customers")
    assert not any("DELETE" in q or "email" in q for q in server._test_db.queries)


def test_mcp_sample_rows_selects_only_visible_columns(server):
    call(server, "sample_rows", table="customers", limit=50)
    sent = server._test_db.queries[-1]
    assert '"country"' in sent and "email" not in sent and sent.endswith("LIMIT 20")


def test_mcp_ask_and_digest_degrade_gracefully(server, monkeypatch):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert "use list_tables" in call(server, "ask", question="How many orders per country?").lower()
    assert "demo schema" in call(server, "daily_digest")


# --- the packaged agent skill ---------------------------------------------------------------

SKILL_DIR = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "skill"
    / "meridian-postgres-analyst"
)


def test_skill_frontmatter_is_valid():
    import re

    import yaml

    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---")[1])
    assert meta["name"] == SKILL_DIR.name and re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    assert 50 < len(meta["description"]) <= 1024
    for link in re.findall(r"\]\((references/[^)]+)\)", text):
        assert (SKILL_DIR / link).is_file(), link


def test_every_query_pattern_in_the_skill_passes_the_guard():
    import re

    patterns = (SKILL_DIR / "references" / "query-patterns.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```sql\n(.*?)```", patterns, flags=re.S)
    assert len(blocks) >= 6
    for sql in blocks:
        check_sql(sql, ["orders", "customers", "payments"], hidden_columns=HIDDEN)
