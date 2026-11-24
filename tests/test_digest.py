from __future__ import annotations

import datetime as dt

from conftest import DATA_THROUGH, FakeDB, FakeLLM, result

from meridian.digest import METRICS, DigestBrief, assess, build_digest, robust_z, weekday_adjusted
from meridian.llm import LLMError

AS_OF = dt.date(2026, 11, 23)  # a Monday


def series(base: float, last: float, weekend: float = 1.0) -> tuple[list[dt.date], list[float]]:
    days = [AS_OF - dt.timedelta(days=28 - i) for i in range(29)]
    values = [
        base * (weekend if d.weekday() >= 5 else 1) * (1 + 0.02 * ((i % 5) - 2))
        for i, d in enumerate(days)
    ]
    values[-1] = last
    return days, values


def test_robust_z():
    assert abs(robust_z(10, [10, 10.2, 9.8, 10.1, 9.9])) < 1
    assert robust_z(20, [10, 10.2, 9.8, 10.1, 9.9]) > 10
    assert robust_z(5, []) == 0.0
    assert robust_z(12, [10, 10, 10]) > 0  # zero MAD handled


def test_weekday_adjustment_removes_weekend_dips():
    days, values = series(1000, 1000, weekend=0.6)
    adjusted = weekday_adjusted(days, values)
    weekend = [v for d, v in zip(days, adjusted, strict=True) if d.weekday() >= 5]
    assert min(weekend) > 900


def test_assess_flags_material_spikes_only():
    revenue = next(m for m in METRICS if m.key == "revenue")
    days, values = series(80000, 80500, weekend=0.7)
    normal = assess(revenue, days, values)
    assert not normal.anomaly and abs(normal.change_pct) < 3

    days, values = series(80000, 52000, weekend=0.7)
    drop = assess(revenue, days, values)
    assert drop.anomaly and drop.direction == "down" and drop.favourable is False
    assert drop.change_pct < -30

    failed = next(m for m in METRICS if m.key == "failed_rate")
    days, values = series(3.4, 6.1)
    spike = assess(failed, days, values)
    assert spike.anomaly and spike.direction == "up" and spike.favourable is False


def digest_db() -> FakeDB:
    days = [AS_OF - dt.timedelta(days=28 - i) for i in range(29)]
    daily = []
    for i, d in enumerate(days):
        wobble = 1 + 0.02 * ((i % 5) - 2)
        weekend = 0.75 if d.weekday() >= 5 else 1
        daily.append(
            [
                d.isoformat(),
                80000 * weekend * wobble,
                1500 * weekend * wobble,
                3.4 * wobble,
                55 * weekend * wobble,
                12 * wobble,
            ]
        )
    daily[-1] = [
        AS_OF.isoformat(),
        81000,
        1520,
        6.08,
        101,
        41,
    ]  # failed rate, sign-ups, churn spike
    return FakeDB(
        [
            DATA_THROUGH,
            (
                "WITH days AS",
                result(
                    ["day", "revenue", "transactions", "failed_rate", "signups", "cancellations"],
                    daily,
                ),
            ),
            (
                "JOIN regions r ON r.id = t.region_id",
                result(
                    ["name", "today", "baseline", "n"],
                    [["West Africa", 14.32, 3.1, 470], ["Europe", 3.0, 3.2, 250]],
                ),
            ),
            (
                "failure_reason, count(*)",
                result(
                    ["reason", "failures"], [["issuer_unavailable", 61], ["insufficient_funds", 18]]
                ),
            ),
        ]
    )


def brief(text: str) -> DigestBrief:
    return DigestBrief(
        headline=text, bullets=["Failed rate 6.08% vs 3.4% typical.", "Sign-ups 101."], watch=[]
    )


def test_build_digest_detects_planted_anomalies_with_model_brief():
    llm = FakeLLM(
        {DigestBrief: lambda s, p: brief("West Africa failures drove the failed rate to 6.08%.")}
    )
    d = build_digest(digest_db(), llm)
    flagged = {m.key for m in d.anomalies}
    assert {"failed_rate", "signups", "cancellations"} <= flagged and "revenue" not in flagged
    assert d.brief_source == "model" and d.headline.startswith("West Africa")
    assert (
        d.regions[0].region == "West Africa"
        and d.top_failure_reasons[0]["reason"] == "issuer_unavailable"
    )
    facts = llm.calls[0][2]
    assert "issuer_unavailable" in facts and "2026-11-23" in facts
    md = d.markdown()
    assert (
        "*Daily brief for 2026-11-23*" in md
        and "Failed transaction rate | 6.08%" in md
        and "⚠" in md
    )


def test_invented_numbers_fall_back_to_template():
    llm = FakeLLM({DigestBrief: lambda s, p: brief("Failures cost us $250,000 in lost revenue.")})
    d = build_digest(digest_db(), llm)
    assert d.brief_source == "template" and d.unverified_numbers
    assert "outside their normal range" in d.headline


def test_model_failure_and_no_model_still_produce_a_digest():
    for llm in (FakeLLM({DigestBrief: lambda s, p: LLMError("quota")}), None):
        d = build_digest(digest_db(), llm)
        assert d.brief_source == "template" and d.bullets
        assert any("West Africa" in w for w in d.watch)
