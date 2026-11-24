"""Static validation of model-written SQL before it reaches the database.

Defence in depth: this guard is the first layer. The query also runs in a READ ONLY transaction
with a statement timeout, as a database role that only has SELECT grants. Any one layer failing
is not enough to change data.

Rules enforced here:
  * exactly one statement, and it is a query (SELECT / WITH ... SELECT / UNION of selects)
  * no data-changing or schema-changing constructs anywhere in the tree (including CTEs)
  * only allow-listed tables (CTE names are fine)
  * no dangerous functions (sleep, file access, settings, remote connections, ...)
  * a row limit is always applied to the outer query
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DIALECT = "postgres"
DEFAULT_ROW_LIMIT = 500

# Expression types that must never appear anywhere in a query tree.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    t
    for t in (
        getattr(exp, name, None)
        for name in (
            "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
            "TruncateTable", "Command", "Grant", "Revoke", "Copy", "Into", "Set", "Lock",
            "Transaction", "Commit", "Rollback", "Use", "Pragma", "Kill", "LoadData", "Analyze",
        )
    )
    if t is not None
)  # fmt: skip

# Functions that read files, sleep, change settings, reach other servers or inspect secrets.
FORBIDDEN_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get", "lo_put", "set_config",
    "current_setting", "dblink", "dblink_exec", "dblink_connect", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile", "query_to_xml",
    "pg_advisory_lock", "txid_current", "nextval", "setval", "pg_notify",
}  # fmt: skip


class UnsafeQueryError(ValueError):
    """The SQL was rejected; the message is safe to show to the model and the user."""


@dataclass(frozen=True)
class CheckedQuery:
    sql: str  # normalised SQL with the row limit applied
    tables: frozenset[str]


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    name = getattr(node, "sql_name", None)
    return (name() if callable(name) else type(node).__name__).lower()


def check_sql(
    sql: str, allowed_tables: Iterable[str], *, row_limit: int = DEFAULT_ROW_LIMIT
) -> CheckedQuery:
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise UnsafeQueryError("empty query")
    try:
        statements = [s for s in sqlglot.parse(text, read=DIALECT) if s is not None]
    except ParseError as exc:
        raise UnsafeQueryError(f"could not parse SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise UnsafeQueryError("only a single statement is allowed")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise UnsafeQueryError(f"only SELECT queries are allowed (got {tree.key.upper()})")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQueryError(f"{node.key.upper()} is not allowed in a read-only query")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise UnsafeQueryError("SELECT INTO is not allowed")
        if isinstance(node, exp.Lock) or (isinstance(node, exp.Select) and node.args.get("locks")):
            raise UnsafeQueryError("row locks (FOR UPDATE/SHARE) are not allowed")
        if isinstance(node, exp.Func) and _function_name(node) in FORBIDDEN_FUNCTIONS:
            raise UnsafeQueryError(f"function {_function_name(node)}() is not allowed")

    allowed = {t.lower() for t in allowed_tables}
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    used: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if not name:  # e.g. table functions such as generate_series(...)
            continue
        schema = (table.db or "").lower()
        if schema and schema != "public":
            raise UnsafeQueryError(f"schema {schema!r} is not allowed")
        if name in cte_names:
            continue
        if name not in allowed:
            raise UnsafeQueryError(
                f"table {name!r} is not available; use one of: {', '.join(sorted(allowed))}"
            )
        used.add(name)

    inner = tree.sql(dialect=DIALECT)
    limited = f"SELECT * FROM ({inner}) AS meridian_q LIMIT {int(row_limit)}"
    return CheckedQuery(sql=limited, tables=frozenset(used))
