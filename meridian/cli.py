"""Command line.

meridian web [--port 8000]
meridian ask "Which region had the highest failed transaction rate last week?"
meridian digest [--as-of 2026-11-23]
meridian mcp
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="meridian", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_web = sub.add_parser("web", help="run the web app")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8000)
    p_ask = sub.add_parser("ask", help="answer one question")
    p_ask.add_argument("question")
    p_digest = sub.add_parser("digest", help="print the daily digest as Markdown")
    p_digest.add_argument("--as-of", type=dt.date.fromisoformat, default=None)
    sub.add_parser("mcp", help="run the MCP server (stdio)")
    args = parser.parse_args(argv)

    if args.command == "web":
        from .web import serve

        serve(args.host, args.port)
    elif args.command == "ask":
        from .analyst import Analyst
        from .db import PostgresDatabase
        from .llm import make_llm

        result = Analyst(PostgresDatabase(), make_llm()).ask(args.question)
        print(result.answer, "\n")
        print(result.sql or "(no query)")
        print(
            json.dumps({"columns": result.columns, "rows": result.rows[:20]}, default=str, indent=1)
        )
    elif args.command == "digest":
        from .db import PostgresDatabase
        from .digest import build_digest
        from .llm import make_llm

        try:
            llm = make_llm()
        except Exception:
            llm = None
        print(build_digest(PostgresDatabase(), llm, args.as_of).markdown())
    elif args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
