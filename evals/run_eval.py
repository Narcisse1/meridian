"""Execution-accuracy eval for natural-language-to-SQL.

    python evals/run_eval.py          # needs DATABASE_URL and an LLM key; uses real model calls

For each case the model's query and the gold query are both executed; the case passes when the
result sets match (column names ignored, numbers within 0.5% / 0.01). Reports accuracy, repair
rate and cost, and saves a JSON report under evals/results/.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meridian.analyst import Analyst  # noqa: E402
from meridian.db import PostgresDatabase  # noqa: E402
from meridian.llm import make_llm  # noqa: E402


def norm(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2)
    return str(v).strip().lower() if v is not None else None


def close(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=0.005, abs_tol=0.01)
    return a == b


def rows_match(got: list[list], want: list[list]) -> bool:
    g = sorted([[norm(v) for v in r] for r in got], key=str)
    w = sorted([[norm(v) for v in r] for r in want], key=str)
    if len(g) != len(w):
        return False
    for gr, wr in zip(g, w, strict=True):
        # the model may add extra columns (e.g. counts next to a label): require gold values present
        if not all(any(close(wv, gv) for gv in gr) for wv in wr):
            return False
    return True


def main() -> int:
    load_dotenv()
    cases = yaml.safe_load((ROOT / "evals" / "nl2sql_cases.yaml").read_text(encoding="utf-8"))[
        "cases"
    ]
    db, llm = PostgresDatabase(), make_llm()
    analyst = Analyst(db, llm)
    rows, passed = [], 0
    for case in cases:
        started = time.time()
        out = analyst.ask(case["question"])
        if case["compare"] == "unanswerable":
            ok = not out.ok
        elif not out.ok:
            ok = False
        else:
            gold = db.query(case["gold"]).rows
            ok = (
                (bool(out.rows) and bool(gold) and close(norm(out.rows[0][0]), norm(gold[0][0])))
                if case["compare"] == "first_cell"
                else rows_match(out.rows, gold)
            )
        passed += ok
        rows.append(
            {
                "id": case["id"],
                "ok": ok,
                "repairs": len(out.attempts),
                "sql": out.sql,
                "seconds": round(time.time() - started, 1),
            }
        )
        print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<40} repairs={len(out.attempts)}")
    summary = {
        "cases": len(cases),
        "passed": passed,
        "accuracy": round(passed / len(cases), 3),
        "repaired": sum(r["repairs"] > 0 for r in rows),
        "usage": llm.usage.summary(),
    }
    print(json.dumps(summary, indent=2))
    out_dir = ROOT / "evals" / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"nl2sql-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
