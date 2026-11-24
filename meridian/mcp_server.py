"""MCP server: lets Claude Desktop, Claude Code or any MCP client use Meridian as a tool.

    meridian mcp            # stdio transport

Tools: list_tables, describe_table, run_query (guarded, read-only), ask (natural language),
daily_digest. Every query path goes through the same SQL guard and read-only executor as the
web app, so an MCP client cannot change data either.
"""

from __future__ import annotations

import json

from .analyst import SEMANTIC_LAYER, Analyst
from .db import TABLES, PostgresDatabase
from .digest import build_digest
from .llm import make_llm
from .sqlguard import UnsafeQueryError, check_sql


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("meridian")
    db = PostgresDatabase()

    @server.tool()
    def list_tables() -> str:
        """List the tables available for analysis, with descriptions and approximate row counts."""
        return "\n".join(f"{t.name} (~{t.row_estimate:,} rows): {t.comment}" for t in db.schema())

    @server.tool()
    def describe_table(name: str) -> str:
        """Columns, types and descriptions for one table, plus the business metric definitions."""
        for t in db.schema():
            if t.name == name.lower():
                cols = "\n".join(f"- {c.name} ({c.type}): {c.comment}" for c in t.columns)
                return f"{t.name}: {t.comment}\n{cols}\n\n{SEMANTIC_LAYER}"
        return f"Unknown table {name!r}. Available: {', '.join(TABLES)}"

    @server.tool()
    def run_query(sql: str) -> str:
        """Run one read-only PostgreSQL SELECT query (max 200 rows). Returns JSON rows."""
        try:
            checked = check_sql(sql, TABLES, row_limit=200)
        except UnsafeQueryError as exc:
            return f"Rejected: {exc}"
        result = db.query(checked.sql, max_rows=200)
        rows = [dict(zip(result.columns, r, strict=False)) for r in result.rows]
        return json.dumps({"rows": rows, "truncated": result.truncated}, default=str)

    @server.tool()
    def ask(question: str) -> str:
        """Answer a business question in plain English: writes, validates and runs the SQL."""
        result = Analyst(db, make_llm()).ask(question)
        return json.dumps(
            {"answer": result.answer, "sql": result.sql, "columns": result.columns,
             "rows": result.rows[:50], "caveats": result.caveats}, default=str,
        )  # fmt: skip

    @server.tool()
    def daily_digest() -> str:
        """Today's executive digest: key metrics, anomalies and a short brief (Markdown)."""
        try:
            llm = make_llm()
        except Exception:
            llm = None
        return build_digest(db, llm).markdown()

    return server


def main() -> None:
    build_server().run()
