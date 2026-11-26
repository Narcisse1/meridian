"""Daily executive digest: deterministic metrics, statistical anomalies, grounded brief.

The numbers never come from the model. Metrics are computed with fixed SQL, anomalies with a
robust, weekday-adjusted z-score, and the model only turns those facts into a short brief. Every
number in the brief is checked against the facts; if the model fails or invents figures, a
template brief is used instead, so the scheduled digest always goes out.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .analyst import unverified_numbers
from .db import Database, QueryResult
from .llm import LLMError, StructuredLLM

WINDOW_DAYS = 28
Z_THRESHOLD = 2.5
MIN_CHANGE = 0.10  # ignore statistically odd but immaterial moves (<10%)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str  # usd | count | pct
    good: str  # up | down


METRICS = [
    Metric("revenue", "Revenue", "usd", "up"),
    Metric("transactions", "Transactions", "count", "up"),
    Metric("failed_rate", "Failed transaction rate", "pct", "down"),
    Metric("signups", "Sign-ups", "count", "up"),
    Metric("cancellations", "Cancellations (churn)", "count", "down"),
]


@dataclass
class MetricReport:
    key: str
    label: str
    unit: str
    value: float
    baseline: float
    change_pct: float | None
    z: float
    anomaly: bool
    direction: str  # up | down | flat
    favourable: bool | None
    series: list[float] = field(default_factory=list)


@dataclass
class RegionRow:
    region: str
    failed_rate_today: float
    failed_rate_baseline: float
    transactions_today: int


@dataclass
class Digest:
    as_of: str
    headline: str
    bullets: list[str]
    watch: list[str]
    metrics: list[MetricReport]
    regions: list[RegionRow]
    top_failure_reasons: list[dict[str, Any]]
    brief_source: str  # model | template
    unverified_numbers: list[str] = field(default_factory=list)

    @property
    def anomalies(self) -> list[MetricReport]:
        return [m for m in self.metrics if m.anomaly]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["anomalies"] = [m.key for m in self.anomalies]
        data["markdown"] = self.markdown()
        data["slack"] = self.markdown(table=False)
        return data

    def markdown(self, *, table: bool = True) -> str:
        """The brief as Markdown. Slack's mrkdwn has no tables, so `table=False` drops the
        metric table (the bullets already carry every metric with its change)."""
        lines = [f"*Daily brief for {self.as_of}*", "", f"*{self.headline}*", ""]
        lines += [f"• {b}" for b in self.bullets]
        if self.watch:
            lines += ["", "*Watch:*"] + [f"• {w}" for w in self.watch]
        if not table:
            return "\n".join(lines)
        lines += ["", "| Metric | Value | vs 28-day typical |", "|---|---|---|"]
        for m in self.metrics:
            change = "n/a" if m.change_pct is None else f"{m.change_pct:+.1f}%"
            flag = " ⚠" if m.anomaly else ""
            lines.append(f"| {m.label} | {fmt(m.value, m.unit)} | {change}{flag} |")
        return "\n".join(lines)


def fmt(value: float, unit: str) -> str:
    if unit == "usd":
        return f"${value:,.0f}"
    if unit == "pct":
        return f"{value:.2f}%"
    return f"{value:,.0f}"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def robust_z(value: float, history: list[float]) -> float:
    """Modified z-score (median / MAD); stable with outliers in the baseline window."""
    if not history:
        return 0.0
    median = statistics.median(history)
    mad = statistics.median([abs(h - median) for h in history])
    if mad == 0:
        spread = statistics.pstdev(history) or (abs(median) * 0.05) or 1.0
        return (value - median) / spread
    return 0.6745 * (value - median) / mad


def weekday_adjusted(days: list[dt.date], values: list[float]) -> list[float]:
    """Divide out the typical weekday level so weekends are not flagged as anomalies."""
    overall = statistics.median(values) if values else 0
    if not overall:
        return values
    by_dow: dict[int, list[float]] = {}
    for d, v in zip(days, values, strict=True):
        by_dow.setdefault(d.weekday(), []).append(v)
    factors = {k: (statistics.median(v) / overall) or 1.0 for k, v in by_dow.items()}
    return [v / factors.get(d.weekday(), 1.0) for d, v in zip(days, values, strict=True)]


def assess(metric: Metric, days: list[dt.date], values: list[float]) -> MetricReport:
    """`values` ends with the day being reported; the earlier ones are the baseline window."""
    today, history = values[-1], values[:-1]
    seasonal = metric.key in {"revenue", "transactions", "signups"}
    adjusted = weekday_adjusted(days, values) if seasonal and len(values) >= 14 else values
    z = robust_z(adjusted[-1], adjusted[:-1])
    baseline = statistics.median(history) if history else 0.0
    if seasonal and history:  # the typical value for this weekday
        same_dow = [
            v for d, v in zip(days[:-1], history, strict=True) if d.weekday() == days[-1].weekday()
        ]
        baseline = statistics.median(same_dow) if same_dow else baseline
    change = None if not baseline else (today - baseline) / baseline * 100
    direction = "flat" if change is None or abs(change) < 1 else ("up" if change > 0 else "down")
    anomaly = abs(z) >= Z_THRESHOLD and (change is None or abs(change) >= MIN_CHANGE * 100)
    favourable = None if direction == "flat" else (direction == metric.good)
    return MetricReport(
        key=metric.key, label=metric.label, unit=metric.unit, value=round(today, 2),
        baseline=round(baseline, 2), change_pct=None if change is None else round(change, 1),
        z=round(z, 2), anomaly=anomaly, direction=direction, favourable=favourable,
        series=[round(v, 2) for v in values],
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _daily_sql(start: dt.date, end: dt.date) -> str:
    s, e = start.isoformat(), end.isoformat()
    return f"""
    WITH days AS (SELECT generate_series(DATE '{s}', DATE '{e}', interval '1 day')::date AS day),
    tx AS (
      SELECT created_at::date AS day,
             sum(amount) FILTER (WHERE status = 'succeeded') AS revenue,
             count(*) AS transactions,
             100.0 * avg((status = 'failed')::int) AS failed_rate
      FROM transactions
      WHERE created_at >= DATE '{s}' AND created_at < DATE '{e}' + 1 GROUP BY 1),
    su AS (SELECT signed_up_at::date AS day, count(*) AS signups FROM customers
           WHERE signed_up_at >= DATE '{s}' AND signed_up_at < DATE '{e}' + 1 GROUP BY 1),
    ch AS (SELECT canceled_at::date AS day, count(*) AS cancellations FROM subscriptions
           WHERE canceled_at >= DATE '{s}' AND canceled_at < DATE '{e}' + 1 GROUP BY 1)
    SELECT d.day, coalesce(tx.revenue, 0), coalesce(tx.transactions, 0),
           coalesce(tx.failed_rate, 0), coalesce(su.signups, 0), coalesce(ch.cancellations, 0)
    FROM days d
    LEFT JOIN tx ON tx.day = d.day LEFT JOIN su ON su.day = d.day LEFT JOIN ch ON ch.day = d.day
    ORDER BY d.day"""


def _region_sql(as_of: dt.date) -> str:
    d = as_of.isoformat()
    return f"""
    SELECT r.name,
           round(100.0 * avg((t.status = 'failed')::int) FILTER (WHERE t.created_at::date = DATE '{d}'), 2),
           round(100.0 * avg((t.status = 'failed')::int) FILTER (WHERE t.created_at::date < DATE '{d}'), 2),
           count(*) FILTER (WHERE t.created_at::date = DATE '{d}')
    FROM transactions t JOIN regions r ON r.id = t.region_id
    WHERE t.created_at >= DATE '{d}' - {WINDOW_DAYS} AND t.created_at < DATE '{d}' + 1
    GROUP BY r.name ORDER BY 2 DESC NULLS LAST"""


def _reasons_sql(as_of: dt.date) -> str:
    d = as_of.isoformat()
    return f"""
    SELECT failure_reason, count(*) AS failures FROM transactions
    WHERE status = 'failed' AND created_at >= DATE '{d}' AND created_at < DATE '{d}' + 1
    GROUP BY 1 ORDER BY 2 DESC LIMIT 5"""


def latest_complete_day(db: Database) -> dt.date:
    rows = db.query("SELECT max(created_at)::date FROM transactions", max_rows=1).rows
    if not rows or rows[0][0] is None:
        raise ValueError("no transactions in the database")
    return dt.date.fromisoformat(str(rows[0][0])[:10])


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------


class DigestBrief(BaseModel):
    headline: str = Field(description="One sentence: the single most important thing today")
    bullets: list[str] = Field(description="3-5 short bullets, most important first")
    watch: list[str] = Field(description="0-2 items to keep an eye on, if any")


BRIEF_SYSTEM = """You write the morning metrics brief for a payments company's leadership team.
You receive facts that were already computed. Write a headline, 3-5 bullets and up to 2 watch items.
- Lead with anomalies (flagged metrics), and say which way they moved and versus what baseline.
- Use only numbers present in the facts (rounding is fine). Never compute new figures.
- Point to the likely driver when the facts show one (e.g. a region or failure reason).
- Plain, specific language. No greetings, no filler, no emoji."""


def template_brief(metrics: list[MetricReport], regions: list[RegionRow]) -> DigestBrief:
    flagged = [m for m in metrics if m.anomaly]

    def line(m: MetricReport) -> str:
        change = (
            ""
            if m.change_pct is None
            else f" ({m.change_pct:+.1f}% vs typical {fmt(m.baseline, m.unit)})"
        )
        return f"{m.label}: {fmt(m.value, m.unit)}{change}"

    headline = (
        f"{len(flagged)} metric(s) moved outside their normal range: "
        + ", ".join(m.label.lower() for m in flagged)
        if flagged
        else "All tracked metrics are within their normal range."
    )
    bullets = [line(m) for m in (flagged + [m for m in metrics if not m.anomaly])][:5]
    worst = regions[0] if regions else None
    watch = []
    if (
        worst
        and worst.failed_rate_today
        and worst.failed_rate_today > 2 * max(worst.failed_rate_baseline, 0.1)
    ):
        watch.append(
            f"{worst.region}: failed rate {worst.failed_rate_today:.2f}% vs {worst.failed_rate_baseline:.2f}% typical"
        )
    return DigestBrief(headline=headline, bullets=bullets, watch=watch)  # fmt: skip


def build_digest(db: Database, llm: StructuredLLM | None, as_of: dt.date | None = None) -> Digest:
    as_of = as_of or latest_complete_day(db)
    start = as_of - dt.timedelta(days=WINDOW_DAYS)
    daily = db.query(_daily_sql(start, as_of), max_rows=WINDOW_DAYS + 2)
    days = [dt.date.fromisoformat(str(r[0])[:10]) for r in daily.rows]
    columns = {m.key: [float(r[i + 1]) for r in daily.rows] for i, m in enumerate(METRICS)}
    metrics = [assess(m, days, columns[m.key]) for m in METRICS]

    regions = [
        RegionRow(region=r[0], failed_rate_today=float(r[1] or 0), failed_rate_baseline=float(r[2] or 0),
                  transactions_today=int(r[3] or 0))
        for r in db.query(_region_sql(as_of), max_rows=50).rows
    ]  # fmt: skip
    reasons = [{"reason": r[0], "failures": int(r[1])} for r in db.query(_reasons_sql(as_of)).rows]

    facts = {
        "as_of": as_of.isoformat(),
        "baseline": f"median of the previous {WINDOW_DAYS} days (same weekday for seasonal metrics)",
        "baseline_window_days": WINDOW_DAYS,
        "metrics": [{k: v for k, v in asdict(m).items() if k != "series"} for m in metrics],
        "failed_rate_by_region_today_vs_28d": [asdict(r) for r in regions],
        "top_failure_reasons_today": reasons,
    }

    brief, source, flagged = None, "template", []
    if llm is not None:
        try:
            brief = llm.structured(
                system=BRIEF_SYSTEM, prompt=json.dumps(facts, default=str), schema=DigestBrief
            )
            fact_rows = QueryResult(
                columns=["v"], rows=[[v] for v in _numbers(facts)], elapsed_ms=0
            )
            text = " ".join([brief.headline, *brief.bullets, *brief.watch])
            flagged = unverified_numbers(text, fact_rows)
            source = "model"
            if flagged:  # the model introduced figures we cannot trace: do not publish them
                brief, source = None, "template"
        except LLMError:
            brief = None
    if brief is None:
        brief = template_brief(metrics, regions)

    return Digest(
        as_of=as_of.isoformat(), headline=brief.headline, bullets=brief.bullets, watch=brief.watch,
        metrics=metrics, regions=regions, top_failure_reasons=reasons, brief_source=source,
        unverified_numbers=flagged,
    )  # fmt: skip


def _numbers(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _numbers(v)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        try:
            out.append(float(obj))
        except ValueError:
            pass
    return out
