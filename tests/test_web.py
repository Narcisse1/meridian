from __future__ import annotations

from conftest import FakeLLM
from fastapi.testclient import TestClient
from test_analyst import GOOD_SQL, plan, summary
from test_digest import brief, digest_db

from meridian.analyst import AnswerSummary, QueryPlan
from meridian.digest import DigestBrief
from meridian.state import State
from meridian.web import create_app

ROWS_RULE = ("meridian_q", None)


def fake_llm():
    return FakeLLM(
        {
            QueryPlan: lambda s, p: plan(GOOD_SQL),
            AnswerSummary: lambda s, p: summary(
                "West Africa had the highest failed rate at 14.32%."
            ),
            DigestBrief: lambda s, p: brief("Failed rate jumped to 6.08%."),
        }
    )


def client(**kw) -> TestClient:
    db = digest_db()
    from test_analyst import ROWS

    db.rules.insert(0, ("meridian_q", ROWS))
    return TestClient(create_app(db=db, llm_factory=fake_llm, state=State(), **kw))


def test_index_health_examples_schema():
    c = client()
    assert "Meridian" in c.get("/").text
    health = c.get("/api/health").json()
    assert (
        health["database"] is True
        and health["data_through"] == "2026-11-23"
        and health["llm_ready"]
    )
    assert len(c.get("/api/examples").json()) >= 4
    schema = c.get("/api/schema").json()
    assert [t["name"] for t in schema["tables"]] == [
        "regions",
        "customers",
        "subscriptions",
        "transactions",
    ]
    assert any(d.startswith("Revenue") for d in schema["definitions"])


def test_ask_and_history():
    c = client()
    r = c.post("/api/ask", json={"question": "Which region had the highest failed rate last week?"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["rows"][0][0] == "West Africa"
    [h] = c.get("/api/history").json()
    assert h["question"].startswith("Which region") and h["sql"] == GOOD_SQL
    assert c.post("/api/ask", json={"question": "x"}).status_code == 422


def test_digest_run_and_latest():
    c = client()
    assert c.get("/api/digest/latest").status_code == 404
    d = c.post("/api/digest/run").json()
    assert (
        d["as_of"] == "2026-11-23"
        and "failed_rate" in d["anomalies"]
        and d["markdown"].startswith("*Daily brief")
    )
    assert c.get("/api/digest/latest").json()["headline"] == d["headline"]


def test_access_token(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ACCESS_TOKEN", "s3cret")
    c = client()
    assert c.post("/api/digest/run").status_code == 401
    assert c.post("/api/digest/run", headers={"X-Access-Token": "s3cret"}).status_code == 200
    assert c.post("/api/digest/run?token=s3cret").status_code == 200
    assert c.get("/api/health").status_code == 200  # health stays public
    assert c.post("/api/digest/run", headers={"X-Access-Token": "nope"}).status_code == 401


Q = {"question": "Which region had the highest failed rate last week?"}


def test_guests_can_ask_within_quota(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ACCESS_TOKEN", "s3cret")
    monkeypatch.setenv("MERIDIAN_GUEST_QUESTIONS_PER_VISITOR", "2")
    c = client()
    health = c.get("/api/health").json()
    assert health["guest_access"] and not health["token_required"]
    assert health["guest_questions_left"] == 2
    r = c.post("/api/ask", json=Q)
    assert r.status_code == 200 and r.json()["guest_questions_left"] == 1
    assert c.post("/api/ask", json=Q).status_code == 200
    blocked = c.post("/api/ask", json=Q)
    assert blocked.status_code == 429 and "2 questions" in blocked.json()["detail"]
    # The owner is never limited by the guest quota.
    owner = c.post("/api/ask", json=Q, headers={"X-Access-Token": "s3cret"})
    assert owner.status_code == 200 and "guest_questions_left" not in owner.json()
    # Another visitor has their own allowance.
    other = c.post("/api/ask", json=Q, headers={"X-Forwarded-For": "203.0.113.9"})
    assert other.status_code == 200


def test_guests_read_but_cannot_run_digest(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ACCESS_TOKEN", "s3cret")
    c = client()
    assert c.post("/api/digest/run").status_code == 401
    assert c.get("/api/digest/latest").status_code == 404
    c.post("/api/digest/run", headers={"X-Access-Token": "s3cret"})
    assert c.get("/api/digest/latest").status_code == 200
    assert c.get("/api/history").status_code == 200


def test_guest_quota_refunded_when_model_fails(monkeypatch):
    from meridian.llm import LLMError

    monkeypatch.setenv("MERIDIAN_ACCESS_TOKEN", "s3cret")
    monkeypatch.setenv("MERIDIAN_GUEST_QUESTIONS_PER_VISITOR", "1")

    def broken():
        raise LLMError("quota exhausted")

    c = TestClient(create_app(db=digest_db(), llm_factory=broken, state=State()))
    assert c.post("/api/ask", json=Q).status_code == 502
    assert c.get("/api/health").json()["guest_questions_left"] == 1


def test_guest_access_off(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ACCESS_TOKEN", "s3cret")
    monkeypatch.setenv("MERIDIAN_GUEST_ACCESS", "off")
    c = client()
    assert c.get("/api/health").json()["token_required"] is True
    assert c.post("/api/ask", json=Q).status_code == 401
    assert c.get("/api/history").status_code == 401


def test_rate_limit():
    c = client(ask_limit=2)
    for _ in range(2):
        assert c.post("/api/ask", json={"question": "revenue?"}).status_code == 200
    assert c.post("/api/ask", json={"question": "revenue?"}).status_code == 429
