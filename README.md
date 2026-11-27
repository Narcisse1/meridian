<div align="center">

# Meridian

### Your AI data analyst for PostgreSQL: plain-English questions in, safe read-only SQL and answers out.

[![Live demo](https://img.shields.io/badge/Live_demo-open_the_app-1f4e8c?style=for-the-badge)](https://meridian-analyst.onrender.com)
[![Install in your agent](https://img.shields.io/badge/Install-MCP_+_agent_skill-D97757?style=for-the-badge)](#use-it-in-your-ai-agent-mcp-server--skill)
[![Get it on Agensi](https://img.shields.io/badge/Agensi-free_agent_skill-14213d?style=for-the-badge)](https://www.agensi.io/skills/meridian-ask-postgres-anything-in-plain-english)

</div>

![How Meridian works: question, guarded SQL, answer](docs/images/how-it-works.svg)

**Meridian lets anyone on a team ask the company database a question and get a trustworthy
answer in seconds, without SQL skills and without putting the data at risk.** It writes the
query, proves it is read-only before running it, checks every number in the answer against the
results, and shows the SQL so anyone can verify it. Every weekday morning it also posts a brief
to Slack with the key metrics and anything unusual.

## Why it matters

| Without Meridian | With Meridian |
|---|---|
| Questions wait days in an analyst's queue | Answer, chart and SQL in seconds |
| Giving everyone database access is a security risk | Four independent safety layers: the AI can only read, and only what you allow |
| Customer emails and phone numbers leak into AI prompts | Restricted columns are invisible to the model and refused by the guard |
| AI answers sound confident but may invent numbers | Every figure is checked against the result rows; untraceable numbers are flagged |
| Morning metrics are assembled by hand, or not at all | n8n posts a brief at 07:00 with anomalies flagged by robust statistics |
| Every AI tool needs its own database integration | One MCP server + skill works in Claude Code, Cursor, Copilot, Windsurf and Claude Desktop |

**Live demo:** https://meridian-analyst.onrender.com (free tier: the first load after idle can
take about a minute)

[![CI](https://github.com/Narcisse1/meridian/actions/workflows/ci.yml/badge.svg)](https://github.com/Narcisse1/meridian/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-read--only-4169E1?logo=postgresql&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-server-000000)
![Agent skill](https://img.shields.io/badge/Agent_skill-Claude_Code_%7C_Cursor_%7C_Copilot-D97757)
![n8n](https://img.shields.io/badge/n8n-daily_digest-EA4B71?logo=n8n&logoColor=white)
![Gemini](https://img.shields.io/badge/Google_Gemini-8E75B2?logo=googlegemini&logoColor=white)
![Claude](https://img.shields.io/badge/Anthropic_Claude-191919?logo=anthropic&logoColor=white)

> "Which region had the highest failed transaction rate last week?"
> → *West Africa, at 15.9%, roughly five times the other regions.* Bar chart, table, and the
> exact read-only SQL that produced it.

## Built with

| Tool | Role in Meridian |
|---|---|
| **PostgreSQL** (hosted on **Neon**) | The data being analysed; a SELECT-only role with read-only transactions by default |
| **sqlglot** | Parses every model-written query into a syntax tree so the guard can prove it only reads |
| **Google Gemini** / **Anthropic Claude** | Writes the SQL plan and the summary through structured outputs; switch with `LLM_PROVIDER` |
| **MCP** (Model Context Protocol) | Exposes the guarded tools to any AI agent; tools are annotated read-only |
| **Agent skill** (`SKILL.md`) | Teaches agents the workflow and how to recover from rejected queries |
| **n8n** + **Slack** | Weekday 07:00 digest: alert channel when anomalies are found, routine channel otherwise |
| **FastAPI** + vanilla JS | Web app: Ask, Daily digest with KPI sparklines, Data dictionary, History |
| **Docker**, **Render**, **GitHub Actions** | Deployment, plus CI that runs the tests against a real PostgreSQL service |

## What this project demonstrates

- **Safe AI over production data:** AST-level SQL validation, column-level privacy, read-only
  execution and least-privilege roles, each tested independently.
- **Self-correcting agent loop:** guard and database errors go back to the model, which repairs
  its query (up to 3 attempts).
- **Hallucination control:** numbers in answers and briefs are verified against the data; the
  digest falls back to a deterministic template if the model's brief can't be verified.
- **Statistics, not vibes:** anomalies use a weekday-adjusted median/MAD robust z-score.
- **Agent ecosystem ready:** an MCP server and packaged skill that work on any PostgreSQL schema.
- **Engineering quality:** 80+ tests (unit, API, MCP protocol, real PostgreSQL in CI) and an
  NL→SQL evaluation set measuring execution accuracy.

## The problem

Operational questions ("why did failed payments jump?", "how many sign-ups yesterday, by
channel?") usually wait in an analyst's queue, while giving everyone database access is unsafe.
And the numbers leaders need every morning are assembled by hand, or not at all.

## What it does

| | |
|---|---|
| **Ask** | Natural language → validated, read-only SQL → answer, chart, table, CSV |
| **Daily digest** | Revenue, transactions, failed-payment rate, sign-ups and churn vs their normal range; anomalies flagged; a short brief written from the computed facts |
| **Automation** | An n8n workflow (included) runs the digest every weekday at 07:00 and posts to Slack: an alert channel if anything is anomalous, a routine channel otherwise |
| **MCP server + agent skill** | Any AI coding agent (Claude Code, Cursor, Copilot, Windsurf, Claude Desktop) gets the same guarded tools on **any PostgreSQL database**: `list_tables`, `describe_table`, `sample_rows`, `run_query`, `ask`, `daily_digest`. A packaged skill teaches the agent when and how to use them |
| **Sensitive data stays hidden** | Mark columns such as emails or phone numbers as restricted: they disappear from the schema the model sees, and any query touching them is refused |

### Screenshots (live app)

**Ask:** a plain-English question, the answer, a chart, the result table and the exact SQL that
produced it.

![Meridian answering two questions with a chart, a table and the SQL](docs/images/ask.png)

**Data dictionary:** the tables and the business definitions the analyst must follow, so
"revenue" or "churn rate" always means the same thing.

![Meridian data dictionary with business definitions](docs/images/data-dictionary.png)

### The daily digest, automated end to end

Every weekday at 07:00 (Africa/Douala) an **n8n** workflow wakes the app (free hosting sleeps
when idle), runs the digest, and branches: if any metric is outside its normal range it posts
an alert to Slack, otherwise a routine summary. The workflow is in
[`n8n/daily-digest.json`](n8n/daily-digest.json).

![n8n workflow: schedule, wake up, run digest, branch on anomalies, post to Slack](docs/images/n8n-workflow.png)

**What lands in Slack.** Every figure is computed with fixed SQL and compared with the same
weekday over the previous 28 days. The AI may only word the brief from those computed facts;
any number it writes that is not among them is rejected, and a template brief is used instead.

![Slack alert: 3 metrics need attention, with the morning brief](docs/images/slack-alert.png)

**The same digest in the app,** with sparklines, the regional breakdown and the top failure
reasons, so the person reading the alert can see what changed and ask follow-up questions.

![Meridian daily digest page with metric cards, anomalies and regional breakdown](docs/images/daily-digest.png)

```mermaid
flowchart LR
  Q[Question] --> P[LLM: query plan<br/>structured output]
  P --> G{SQL guard}
  G -- rejected --> P
  G -- ok --> X[(Postgres<br/>read-only txn,<br/>SELECT-only role)]
  X -- error --> P
  X --> S[LLM: summary]
  S --> V{Numbers found<br/>in results?}
  V --> A[Answer + chart + SQL]
  N[n8n 07:00] --> D[Digest API]
  D --> M[Fixed metric SQL] --> Z[Robust z-score<br/>weekday-adjusted]
  Z --> B[LLM brief<br/>from facts only] --> SL[Slack]
```

## Use it in your AI agent (MCP server + skill)

Meridian works with **any PostgreSQL database**, not only the demo. Your agent writes the SQL;
Meridian makes sure it can only read, and only what you allow. No LLM key is needed for this.

```bash
claude mcp add meridian \
  -e DATABASE_URL="postgresql://meridian_reader:PASSWORD@HOST/db?sslmode=require" \
  -e MERIDIAN_HIDDEN_COLUMNS="customers.email,customers.phone" \
  -- uvx --from git+https://github.com/Narcisse1/meridian meridian mcp
```

Then add the skill: get it free on
[Agensi](https://www.agensi.io/skills/meridian-ask-postgres-anything-in-plain-english), where it
passed their security review, or copy [`skill/meridian-postgres-analyst/`](skill/meridian-postgres-analyst)
to `~/.claude/skills/`. It tells the agent the workflow (map the schema → check real values →
one aggregated query → cite the SQL), how to recover from each kind of rejected query, and
proven query patterns (period comparisons, cohorts, funnels, anomaly checks). Setup for Cursor,
VS Code/Copilot, Windsurf and Claude Desktop is in
[`references/setup.md`](skill/meridian-postgres-analyst/references/setup.md).

| Setting | Meaning |
|---|---|
| `MERIDIAN_SCHEMA` | Schema to expose (default `public`) |
| `MERIDIAN_TABLES` | Allow-list of tables (default: every table and view in the schema) |
| `MERIDIAN_HIDDEN_COLUMNS` | Sensitive columns, never shown or queried (`table.column,...`) |
| `MERIDIAN_DEFINITIONS` | Your business definitions in Markdown ([example](docs/definitions.example.md)) |
| `MERIDIAN_DATE_COLUMN` | `table.column` that anchors "last week" on the latest data |

All tools are annotated read-only, so clients can approve them safely.

## Safety: four independent layers

1. **SQL guard** (`meridian/sqlguard.py`, sqlglot AST): one statement only; must be a query;
   no INSERT/UPDATE/DELETE/DDL anywhere in the tree (including data-modifying CTEs); no
   `SELECT INTO` or row locks; no dangerous functions (`pg_sleep`, `pg_read_file`, `dblink`,
   `set_config`, ...); only allow-listed tables in the configured schema (no system catalogs);
   a row limit is always applied. 38 tests cover these cases.
2. **Column-level privacy**: restricted columns are removed from the schema the model sees, and
   any query that names them, or uses `SELECT *` on their tables, is refused. `COUNT(*)` still
   works.
3. **Read-only execution**: every query runs in `SET TRANSACTION READ ONLY` with a statement
   timeout.
4. **Least-privilege role**: the app connects as `meridian_reader`, which only has SELECT grants
   and `default_transaction_read_only = on`. An integration test proves writes fail even when
   the guard is bypassed.

Errors from the guard and the database are fed back to the model, which repairs its query (up to
3 attempts).

## Reliability and honesty

- **Semantic layer:** metric definitions (revenue, failed rate, churn, active MRR) are given to the
  model, so the same question always means the same calculation.
- **Number grounding:** every figure in a generated answer or brief is checked against the
  result set / computed facts. Answers flag untraceable numbers; the digest refuses to publish a
  model brief containing them and falls back to a deterministic template.
- **The digest never depends on the model:** metrics and anomalies are computed with fixed SQL
  and statistics (median/MAD robust z-score, weekday-adjusted so weekends are not "anomalies").
  If the model is down, the template brief still goes out.
- **Provider-agnostic:** Anthropic Claude or Google Gemini behind one interface (`LLM_PROVIDER`),
  with refusal fallbacks, model auto-discovery, quota rotation and typed errors.

## Demo data

`db/seed.sql` generates 120 days of data for **Lumio Pay**, a fictional African payments company
(6 regions, ~6,000 merchants, ~120,000 transactions) with trend, weekly seasonality and noise,
plus three planted incidents the digest should catch:

- West Africa mobile-money payments fail far more often in the last 6 days (`issuer_unavailable`)
- sign-ups jump ~70% on the latest day
- growth-plan cancellations rise after a price change

## Run it

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env          # DATABASE_URL (read-only role) + GEMINI_API_KEY or ANTHROPIC_API_KEY

psql "$OWNER_URL" -f db/schema.sql -f db/seed.sql -f db/readonly_role.sql

meridian web                  # http://127.0.0.1:8000
meridian ask "Which region had the highest failed transaction rate last week?"
meridian digest               # prints today's brief as Markdown
meridian mcp                  # MCP server over stdio
```

**MCP clients:** see [Use it in your AI agent](#use-it-in-your-ai-agent-mcp-server--skill).

**n8n:** import `n8n/daily-digest.json`, set the URL to your deployment, create a Header Auth
credential (`X-Access-Token`) and a Slack credential, then activate.

**Deploy:** the `Dockerfile` serves the app on `$PORT` (Render, Railway, Fly.io). Set
`DATABASE_URL`, an LLM key and `MERIDIAN_ACCESS_TOKEN`.

**Access model (public demo):** anyone can try the live site without signing in. Guests get
5 questions per visitor per day (50 in total), and can read the latest digest, history and data
dictionary. The owner's token (`X-Access-Token`) removes the limit and is required to run the
digest. Tune it with `MERIDIAN_GUEST_QUESTIONS_PER_VISITOR` and `MERIDIAN_GUEST_QUESTIONS_PER_DAY`,
or set `MERIDIAN_GUEST_ACCESS=off` to require the token for everything. Either way, every query
runs as a SELECT-only database role.

## Tests and evaluation

```bash
pytest -q                     # unit + API tests; Postgres integration tests run when
                              # TEST_DATABASE_URL is set (CI starts a Postgres service)
python evals/run_eval.py      # execution accuracy on evals/nl2sql_cases.yaml (real model calls)
```

The eval runs the model's query and a reference query and compares results, reporting accuracy,
how often the repair loop was needed, and cost.

## Project layout

```
meridian/
  sqlguard.py     AST-based read-only validator
  db.py           read-only executor, schema introspection (column comments = data dictionary)
  analyst.py      NL→SQL with repair loop, semantic layer, grounded summaries
  digest.py       metrics, anomaly detection, grounded brief with template fallback
  llm.py          Claude / Gemini provider layer
  web.py          FastAPI app; static/index.html is the UI
  mcp_server.py   MCP tools (read-only annotated; MCP SDK 1.x and 2.x)
  state.py        question and digest history (SQLite)
skill/            installable agent skill: SKILL.md + setup and query-pattern references
docs/             example business-definitions file
db/               schema, seed, read-only role
n8n/              scheduled digest workflow
evals/            NL→SQL eval set and runner
tests/            unit, API and Postgres integration tests
```

Photography: Christina @ wocintechchat.com via [Unsplash](https://unsplash.com).
