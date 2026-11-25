# Installing the Meridian MCP server

Meridian runs locally next to your agent and connects to PostgreSQL with a connection string
you provide. Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python and Meridian on
first run). No API key is needed for the SQL tools.

## 1. Create a read-only database role (recommended)

Meridian already refuses anything but `SELECT` and runs every query in a read-only transaction.
A role that only has `SELECT` grants makes that a third, database-enforced layer:

```sql
CREATE ROLE meridian_reader LOGIN PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE your_db TO meridian_reader;
GRANT USAGE ON SCHEMA public TO meridian_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO meridian_reader;
ALTER ROLE meridian_reader SET default_transaction_read_only = on;
ALTER ROLE meridian_reader SET statement_timeout = '10s';
```

Connection string: `postgresql://meridian_reader:PASSWORD@HOST:5432/your_db?sslmode=require`

## 2. Configure

| Variable | Required | Meaning |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL connection string (prefer the read-only role) |
| `MERIDIAN_SCHEMA` | no | Schema to expose (default `public`) |
| `MERIDIAN_TABLES` | no | Comma-separated allow-list (default: all tables and views in the schema) |
| `MERIDIAN_HIDDEN_COLUMNS` | no | Sensitive columns that are never shown or queried, e.g. `customers.email,customers.phone,users.password_hash` |
| `MERIDIAN_DEFINITIONS` | no | Path to a Markdown file of business definitions (how revenue, active user... are calculated) |
| `MERIDIAN_DATE_COLUMN` | no | `table.column` that shows how fresh the data is (anchors "last week") |
| `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` | no | Only for the `ask` tool (Meridian's own model) |

## 3. Add it to your agent

The command is the same everywhere:

```
uvx --from git+https://github.com/Narcisse1/meridian meridian mcp
```

**Claude Code**

```bash
claude mcp add meridian \
  -e DATABASE_URL="postgresql://meridian_reader:PASSWORD@HOST/db?sslmode=require" \
  -e MERIDIAN_HIDDEN_COLUMNS="customers.email,customers.phone" \
  -- uvx --from git+https://github.com/Narcisse1/meridian meridian mcp
```

Then copy this skill folder to `~/.claude/skills/meridian-postgres-analyst/` (all projects) or
`.claude/skills/meridian-postgres-analyst/` (one project).

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`.cursor/mcp.json`),
**Windsurf** (`~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "meridian": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Narcisse1/meridian", "meridian", "mcp"],
      "env": {
        "DATABASE_URL": "postgresql://meridian_reader:PASSWORD@HOST/db?sslmode=require",
        "MERIDIAN_HIDDEN_COLUMNS": "customers.email,customers.phone"
      }
    }
  }
}
```

**VS Code / GitHub Copilot** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "meridian": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Narcisse1/meridian", "meridian", "mcp"],
      "env": { "DATABASE_URL": "${input:meridianDatabaseUrl}" }
    }
  },
  "inputs": [
    { "id": "meridianDatabaseUrl", "type": "promptString", "description": "PostgreSQL URL (read-only role)", "password": true }
  ]
}
```

For agents that read `AGENTS.md` instead of skills, paste the *Workflow* and *Fixing rejected
queries* sections of `SKILL.md` into it.

## 4. Check it works

Ask your agent: "List the tables in my database and tell me what each one is for." It should
call `list_tables` and `describe_table`. Then try a question such as "What were our top 5
products by revenue last month?".

## Troubleshooting

- `DATABASE_URL is not set`: the `env` block is missing or the agent was not restarted.
- `connection timeout expired`: host/port unreachable from your machine, or SSL required
  (add `?sslmode=require`).
- `No tables found in schema 'public'`: set `MERIDIAN_SCHEMA`, or grant `USAGE`/`SELECT` to the role.
- `uvx: command not found`: install uv (`pip install uv`, or see docs.astral.sh/uv).
