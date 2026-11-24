from __future__ import annotations

from conftest import DATA_THROUGH, FakeDB, FakeLLM, result

from meridian.analyst import Analyst, AnswerSummary, QueryPlan, unverified_numbers
from meridian.db import QueryError
from meridian.llm import LLMError

GOOD_SQL = (
    "SELECT r.name AS region, round(100.0 * avg((t.status = 'failed')::int), 2) AS failed_rate_pct "
    "FROM transactions t JOIN regions r ON r.id = t.region_id "
    "WHERE t.created_at >= DATE '2026-11-17' GROUP BY 1 ORDER BY 2 DESC"
)
ROWS = result(
    ["region", "failed_rate_pct"], [["West Africa", 15.92], ["East Africa", 3.41], ["Europe", 3.12]]
)


def plan(sql: str, chart: str = "bar") -> QueryPlan:
    return QueryPlan(
        sql=sql,
        explanation="Failed share by region over the last 7 days.",
        chart=chart,
        x="region",
        y="failed_rate_pct",
        title="Failed rate by region",
    )


def summary(text: str) -> AnswerSummary:
    return AnswerSummary(answer=text, caveats=[])


def test_happy_path_returns_answer_chart_and_rows():
    db = FakeDB([DATA_THROUGH, ("meridian_q", ROWS)])
    llm = FakeLLM(
        {
            QueryPlan: lambda s, p: plan(GOOD_SQL),
            AnswerSummary: lambda s, p: summary(
                "West Africa had the highest failed rate at 15.92%."
            ),
        }
    )
    out = Analyst(db, llm).ask("Which region had the highest failed transaction rate last week?")
    assert out.ok and out.rows[0] == ["West Africa", 15.92]
    assert out.answer.startswith("West Africa") and out.unverified_numbers == []
    assert out.chart == {
        "type": "bar",
        "x": "region",
        "y": "failed_rate_pct",
        "title": "Failed rate by region",
    }
    # the model saw the data cut-off date and the business definitions
    system = llm.calls[0][1]
    assert (
        "complete through 2026-11-23" in system and "Revenue = SUM(transactions.amount)" in system
    )
    # what ran was the guarded, limited query
    assert db.queries[-1].startswith("SELECT * FROM (") and db.queries[-1].endswith("LIMIT 500")


def test_unsafe_sql_is_rejected_and_repaired():
    plans = iter([plan("DELETE FROM transactions"), plan(GOOD_SQL)])
    db = FakeDB([DATA_THROUGH, ("meridian_q", ROWS)])
    llm = FakeLLM(
        {
            QueryPlan: lambda s, p: next(plans),
            AnswerSummary: lambda s, p: summary("West Africa: 15.92%."),
        }
    )
    out = Analyst(db, llm).ask("q?")
    assert out.ok and len(out.attempts) == 1 and "only SELECT" in out.attempts[0].error
    retry_prompt = [p for name, _, p in llm.calls if name == "QueryPlan"][1]
    assert "DELETE FROM transactions" in retry_prompt and "only SELECT" in retry_prompt
    assert not any("DELETE" in q for q in db.queries)  # never reached the database


def test_database_errors_are_fed_back():
    plans = iter([plan("SELECT nonexistent_column FROM transactions"), plan(GOOD_SQL)])
    db = FakeDB(
        [
            DATA_THROUGH,
            ("nonexistent_column", QueryError('column "nonexistent_column" does not exist')),
            ("meridian_q", ROWS),
        ]
    )
    llm = FakeLLM({QueryPlan: lambda s, p: next(plans), AnswerSummary: lambda s, p: summary("ok")})
    out = Analyst(db, llm).ask("q?")
    assert out.ok and "does not exist" in out.attempts[0].error


def test_gives_up_after_three_attempts():
    db = FakeDB([DATA_THROUGH])
    llm = FakeLLM({QueryPlan: lambda s, p: plan("DROP TABLE regions")})
    out = Analyst(db, llm).ask("q?")
    assert not out.ok and len(out.attempts) == 3 and "could not produce a valid query" in out.answer


def test_unanswerable_question():
    db = FakeDB([DATA_THROUGH])
    llm = FakeLLM(
        {
            QueryPlan: lambda s, p: QueryPlan(
                sql="",
                explanation="There is no marketing spend data.",
                chart="table",
                x=None,
                y=None,
                title="",
            )
        }
    )
    out = Analyst(db, llm).ask("What was our ROAS?")
    assert not out.ok and out.answer == "There is no marketing spend data." and out.attempts == []


def test_summary_failure_falls_back_to_plain_answer():
    db = FakeDB([DATA_THROUGH, ("meridian_q", ROWS)])
    llm = FakeLLM(
        {QueryPlan: lambda s, p: plan(GOOD_SQL), AnswerSummary: lambda s, p: LLMError("quota")}
    )
    out = Analyst(db, llm).ask("q?")
    assert out.ok and out.answer == "The query returned 3 row(s); see the table."


def test_unverified_numbers():
    assert (
        unverified_numbers("West Africa led at 15.92%, ahead of East Africa (3.41%).", ROWS) == []
    )
    assert unverified_numbers("West Africa failed rate was 15.9%.", ROWS) == []  # rounding is fine
    assert unverified_numbers("Failures rose 48% week over week.", ROWS) == ["48"]
    assert unverified_numbers("On 2026-11-23 there were 3 regions above 3%.", ROWS) == []
    share = result(["share"], [[0.4123]])
    assert (
        unverified_numbers("That is 41.23% of the total.", share) == []
    )  # fraction shown as percent
