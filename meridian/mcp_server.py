"""MCP server: use Meridian from Claude Code, Claude Desktop, Cursor, VS Code Copilot, Windsurf
or any MCP client, against any PostgreSQL database.

    meridian mcp            # stdio transport
    uvx --from git+https://github.com/Narcisse1/meridian meridian mcp

Tools
  list_tables      tables and views, with descriptions and row estimates
  describe_table   columns, types, comments (sensitive columns are hidden) + metric definitions
  sample_rows      a few example rows, to see real values and formats before writing SQL
  run_query        one read-only SELECT, validated by the SQL guard; returns JSON rows
  ask              plain-English question answered end to end (needs GEMINI_API_KEY or
                   ANTHROPIC_API_KEY on the server; otherwise the calling agent writes the SQL)
  daily_digest     executive digest with anomaly detection (bundled demo schema)

Every query path goes through the same SQL guard and read-only executor as the web app:
one SELECT only, allow-listed tables, no restricted columns, no dangerous functions, a row cap,
a READ ONLY transaction with a timeout, and (recommended) a SELECT-only database role.
"""

from __future__ import annotations

import json

from .analyst import Analyst, semantic_layer
from .db import PostgresDatabase, QueryError, guard_scope, is_demo
from .digest import build_digest
from .llm import make_llm, provider_ready, resolve_provider
from .sqlguard import UnsafeQueryError, check_sql

MAX_MCP_ROWS = 200

INSTRUCTIONS = """\
Meridian gives read-only, guarded SQL access to a PostgreSQL database.
Workflow: list_tables -> describe_table (for each table you need) -> optionally sample_rows ->
run_query with one SELECT. Aggregate in SQL rather than pulling raw rows. If run_query returns
'Rejected: ...', fix the query as the message says; never try to bypass the guard. Restricted
columns hold sensitive data and are not available by design. Cite the SQL you ran in answers."""


def server_llm():
    """The server's own model, or None when no API key is configured."""
    try:
        if not provider_ready(resolve_provider()):
            return None
        return make_llm()
    except Exception:
        return None


def _server_class():
    """MCP Python SDK 2.x renamed FastMCP to MCPServer; support both."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def build_server(db=None):
    from mcp.types import ToolAnnotations

    server = _server_class()("meridian", instructions=INSTRUCTIONS)
    database = db or PostgresDatabase()
    # Every tool only reads: lets MCP clients auto-approve calls safely.
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def guarded(sql: str, limit: int) -> str:
        try:
            checked = check_sql(sql, row_limit=limit, **guard_scope(database))
            result = database.query(checked.sql, max_rows=limit)
        except (UnsafeQueryError, QueryError) as exc:
            return f"Rejected: {exc}"
        rows = [dict(zip(result.columns, r, strict=False)) for r in result.rows]
        return json.dumps(
            {"columns": result.columns, "rows": rows, "row_count": len(rows),
             "truncated": result.truncated}, default=str,
        )  # fmt: skip

    def hidden_for(table: str) -> list[str]:
        return sorted(getattr(database, "hidden_columns", {}).get(table, set()))

    @server.tool(annotations=read_only)
    def list_tables() -> str:
        """List the tables and views available for analysis, with descriptions and approximate
        row counts. Call this first."""
        tables = database.schema()
        if not tables:
            return f"No tables found in schema {getattr(database, 'schema_name', 'public')!r}."
        return "\n".join(
            f"{t.name} (~{t.row_estimate:,} rows){': ' + t.comment if t.comment else ''}"
            for t in tables
        )

    @server.tool(annotations=read_only)
    def describe_table(name: str) -> str:
        """Columns, types and descriptions for one table, plus any business metric definitions.
        Restricted (sensitive) columns are not listed and cannot be queried."""
        tables = database.schema()
        for t in tables:
            if t.name == name.lower().split(".")[-1]:
                cols = "\n".join(
                    f"- {c.name} ({c.type}){': ' + c.comment if c.comment else ''}"
                    for c in t.columns
                )
                hidden = hidden_for(t.name)
                note = (
                    f"\nRestricted columns (not available): {', '.join(hidden)}" if hidden else ""
                )
                return f"{t.name}: {t.comment}\n{cols}{note}\n\n{semantic_layer(tables)}".strip()
        return f"Unknown table {name!r}. Available: {', '.join(t.name for t in tables)}"

    @server.tool(annotations=read_only)
    def sample_rows(table: str, limit: int = 5) -> str:
        """Return up to 20 example rows from a table (visible columns only), to check real values,
        codes and formats before writing a query."""
        tables = {t.name: t for t in database.schema()}
        t = tables.get(table.lower().split(".")[-1])
        if t is None:
            return f"Unknown table {table!r}. Available: {', '.join(tables)}"
        columns = ", ".join(f'"{c.name}"' for c in t.columns)
        return guarded(f'SELECT {columns} FROM "{t.name}"', max(1, min(int(limit), 20)))

    @server.tool(annotations=read_only)
    def run_query(sql: str) -> str:
        """Run one read-only PostgreSQL SELECT (CTEs allowed) and return JSON rows (max 200).
        Data-changing statements, other schemas, restricted columns and unsafe functions are
        rejected with a message explaining why."""
        return guarded(sql, MAX_MCP_ROWS)

    @server.tool(annotations=read_only)
    def ask(question: str) -> str:
        """Answer a business question in plain English: Meridian writes, validates and runs the
        SQL with its own model and returns the answer, the SQL and the rows. Requires an LLM key
        on the server; if none is set, use describe_table + run_query instead."""
        fallback = (
            "Use list_tables, describe_table and run_query to answer the question with your "
            "own SQL."
        )
        llm = server_llm()
        if llm is None:
            return f"ask is unavailable: no GEMINI_API_KEY or ANTHROPIC_API_KEY is set. {fallback}"
        try:
            result = Analyst(database, llm).ask(question)
        except Exception as exc:  # model or quota errors: hand the work back to the agent
            return f"ask failed ({type(exc).__name__}: {exc}). {fallback}"
        return json.dumps(
            {"answer": result.answer, "sql": result.sql, "columns": result.columns,
             "rows": result.rows[:50], "caveats": result.caveats,
             "unverified_numbers": result.unverified_numbers}, default=str,
        )  # fmt: skip

    @server.tool(annotations=read_only)
    def daily_digest() -> str:
        """Executive digest: key metrics vs. their recent baseline, anomalies and a short brief
        (Markdown). Available for the bundled demo schema."""
        if not is_demo(database.schema()):
            return (
                "daily_digest is built for the bundled demo schema. For this database, use "
                "run_query to compare the latest day with the previous weeks."
            )
        return build_digest(database, server_llm()).markdown()  # template brief without a key

    return server


def main() -> None:
    build_server().run()
