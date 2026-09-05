# token-tracker

Local dashboard for Claude Code and Codex (including JetBrains integration logs) token usage and cost. Walks the
JSONL logs each tool already writes, normalizes them into SQLite, and serves a small
web UI with totals, daily charts, per-model / per-project / per-session breakdowns, and
MCP-server usage.

Nothing leaves your machine.

![token-tracker dashboard](docs/dashboard.png)

## Changes and improvements — 2026-09-05

This release includes the Codex accounting repair, partial-cost dashboard, 

### ADD SKILL allowing the agent to give the token consumption after each prompt

A portable [prompt-usage skill](skills/prompt-usage/SKILL.md) for snapshot-based usage reporting, with stop/resume controls and non-blocking error handling.

### Changes 

| Area | Previous behavior | Corrected behavior |
|------|-------------------|--------------------| 
| Token accounting | Repeated events could count the same consumption again. | Reconciles cumulative counters with last-step usage; unchanged totals add no consumption. |
| Incremental ingestion | Counter state and pending MCP results could be lost between imports. | Replays the imported prefix to restore state; unchanged files add no usage or duplicate calls. Resets, resumes, compaction and missing fields are handled explicitly. |
| Token categories | Overlapping counters could inflate estimates. | Fresh input excludes cached input; reasoning is already included in output. Cache writes remain a subset of fresh input. |
| MCP coverage | Newer and namespaced call formats were missed. | Supports legacy/custom calls and outputs, namespaced calls, `mcp_tool_call_end` and completed/failed `item_completed` MCP records. Counts execution evidence and deduplicates by source file and call ID. |
| MCP result sizes | Payload size could be mistaken for billed tokens. | Tracks text size, media counts and reported tool/transport errors separately. Text tokens are explicitly estimated as characters ÷ 4; image/audio/base64 payloads are excluded from that estimate. |
| Model pricing | Unknown models, including GPT-5.6 Sol, could silently inherit default rates. | Uses verified explicit model rates and aliases. Unknown prices preserve tokens and remain unpriced. |
| Session attribution | Generic source labels could misidentify JetBrains; final-model attribution could misprice earlier steps. | Preserves originator/source, distinguishes user sessions, guardian reviews and subagents, and retains model/reasoning effort per usage step. |
| Dashboard costs | Any unpriced usage could make the main total unavailable. | Shows the known API-equivalent subtotal with “Partial estimate · N unpriced steps excluded.” A wholly unpriced selection still shows “Unpriced.” |
| Small screens | Summary cards and pricing notes were clipped. | Cards use two columns on narrow screens and pricing notes wrap. |

### How to read the cost display

**Known API-equivalent cost** is the subtotal of usage that can be priced. It is
not a complete total when the partial-estimate label is present, and it is not an
invoice or a measure of subscription quota consumption. The cost breakdown and
cost per active hour use that same known subtotal for a partial selection.

Unpriced review steps still contribute token counts. `codex-auto-review` remains
unpriced because no reliable public rate was established during the repair.
Uncertain usage records can also remain unpriced even when a model has a rate.
No fallback rate is invented to remove the label. API consumers receive strict
unknown totals separately from `pricing_coverage` and `known_cost_breakdown`.

### Validation and existing databases

The repair was validated with **23 regression tests**, plus full-versus-incremental
comparisons on **three real sessions** using temporary databases. Checks covered
duplicates, resets, compaction, cross-import MCP results, errors/media, unknown
pricing and synthetic mixed-model sessions. Re-ingesting unchanged files added
zero usage or calls. Display checks covered partial, fully priced and wholly
unpriced totals; existing small-text/contrast warnings remain in the UI.

For an existing legacy database, prepare and validate a separate candidate first: startup deliberately
rejects incompatible schemas instead of silently rebuilding live data. See
[the rebuild procedure and limitations](docs/CODEX_REPAIR.md). Source logs are
read as data; historical commands are never executed.

## Agent snapshots: start, work, measure

Snapshots now support **Codex and Claude Code main sessions**. Supply a
source-qualified `session_id`: `codex:<session UUID>` or `claude:<session ID>`.
The server advertises `supported_snapshot_sources` in `/api/health`; unsupported
sources return HTTP 422 rather than being interpreted as Codex. Bare IDs still
mean Codex for compatibility. Claude's separate subagent logs are excluded, and
repeated streamed usage records are reconciled by message identity; decreasing
counters or conflicting identities fail explicitly instead of inventing a delta.

The skill is independent of the client: use any available HTTP tool or curl.
RTK is optional and no Codex-only environment variable is universally required.
Use trusted session metadata from the integration; an agent without a matching
server log adapter cannot measure itself merely by issuing these API calls.

The recommended agent workflow now needs **no turn ID**:

1. Before task execution, POST the agent's session ID to `/api/usage-snapshots` and save the returned `sid`.
2. After the work, POST `/api/usage-snapshots/{sid}/consumption` to refresh logs and receive the delta.

Snapshots isolate the supplied session, excluding other agent sessions. They persist
across restarts in `usage-snapshots.sqlite3`; `tokens.db` is unchanged. Repeated checks
use the original baseline. Warnings compare against prior reported snapshot intervals.
Results measure newly logged usage, so delayed records may cross checkpoint boundaries
and the final check cannot include the answer generated afterward.
See [the agent usage guide](docs/AGENT_USAGE.md) for requests, JetBrains access and limits.

## Prompt-usage skill: controls and failure handling

The portable skill is included at [skills/prompt-usage/SKILL.md](skills/prompt-usage/SKILL.md).
Copy the entire `skills/prompt-usage` folder into your agent's supported skills
directory, or use its persistent-instruction mechanism if it does not discover
SKILL.md files. For a standard Codex installation, use `$CODEX_HOME/skills` when
configured, otherwise `~/.codex/skills`. Review an existing copy before replacing
it. Reload the agent's skill discovery or start a new session, then invoke
`$prompt-usage` in Codex or the equivalent mechanism in your agent. The skill calls the local tracker API directly and contains no
personal installation path.

The `prompt-usage` skill instructs the agent to use the two-request snapshot
workflow and append a usage summary to its final answer. It calls the HTTP API
directly; no helper-script location or personal installation path is required.
To require tracking for each prompt, add this to the agent's persistent instructions:

```text
For every prompt, use $prompt-usage while tracking is enabled.
Create a snapshot before task work, preserve its SID, and report its delta before
the final answer. Honor the user's stop, resume and skip instructions.
```

A skill is an instruction-based workflow, not an executable pre/post hook.
Automatic skill discovery alone does not guarantee every-prompt execution.

### User controls

| Request | Effect |
|---------|--------|
| “Stop measuring token usage” | Disable tracking immediately for this conversation. Abandon the active SID; make no further tracking or diagnostic requests and omit usage footers. |
| “Resume measuring token usage” | Resume from the next prompt with a fresh SID; do not create a late snapshot for the resume request. |
| “Skip tracking for this prompt” | Skip that prompt, abandoning its SID if necessary, and retain the previous tracking preference afterward. |

Equivalent natural wording is accepted. These controls override the mandatory
workflow. The agent preserves the preference across context compaction; they do
not change server settings or disable tracking in other conversations. Steering
messages during ongoing work retain the same SID unless tracking is stopped or
skipped.

After the first tracked prompt of a session, the agent explains the stop, resume
and skip controls once. If the skill is enabled midway through a session, the
notice appears after its first tracked prompt. No reminders are shown while
tracking is disabled.

### Tracking errors do not block the task

If snapshot creation or consumption reporting fails, the agent continues the
user's task and reports **“Usage unavailable”** with the reason at the end. It
never invents zero usage, guesses another session, or creates a late replacement
snapshot to represent the whole task. Without a valid initial SID, the final
consumption request is skipped. Requests use bounded timeouts and do not trigger
retry loops or automatic server restarts.

When server access fails, the agent also suggests: **“If you prefer to continue
without usage checks, say ‘Stop measuring token usage’.”** If the first-prompt
notice is due, it combines the messages rather than repeating them. A failure
does not disable tracking automatically; the user chooses.

When the server responds, recovery guidance is discoverable through
`GET /api/health` → `documentation_url` → `GET /api/docs/operations`.
When unreachable, the agent directs the user to this README for the appropriate
[console or service restart procedure](#server-operations). Sandbox restrictions
and missing session identifiers are distinguished from a stopped server; restarting
a healthy server will not fix those issues.

## Per-prompt usage and warnings for agents

Agents can now request **total consumption for a specific Codex turn** over local
HTTP, including fresh/cached input, output, reasoning, estimated-cost coverage and
MCP calls/errors. `GET /api/prompts` discovers identifiers;
`GET /api/prompt-usage?session_id=…&turn_id=…` returns the report and warning.

The default warning compares against at least 20 completed turns from the same
project, model set, agent type and reasoning effort over 30 days. It flags a metric
only above both its 95th percentile and twice its median. Thresholds are configurable
per request; insufficient history and uncertain boundaries are reported explicitly.

The index refreshes from local logs without changing the database. Reports cover
one session's turn, excluding separate guardian/subagent sessions. A checkpoint
before the final answer reports **usage so far**, not the yet-to-be-generated answer.
No automatic polling or notifications are installed. See [agent setup, examples and
limitations](docs/AGENT_USAGE.md) for copyable requests and an agent instruction.

## Data sources

| Tool   | Path                                                | Per-message tokens                                                                 |
|--------|-----------------------------------------------------|------------------------------------------------------------------------------------|
| Claude | `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`     | `message.usage.{input,output,cache_read_input,cache_creation_input}_tokens`        |
| Codex  | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`      | `event_msg:token_count` → reconciled `total_token_usage` and `last_token_usage` |

For Codex, `cached_input_tokens` is a subset of `input_tokens`. We subtract to separate "fresh"
input from cached input so the two tools line up on the same axes.

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                   # create .venv + install deps
make ingest                               # first run
make server                               # http://127.0.0.1:8732
```

## Periodic ingest (macOS launchd)

```sh
make agent                                # ingests every 5 minutes
make logs                                 # tail ~/Library/Logs/token-tracker.log
make down                                 # uninstall the agent
```

`make up` loads the launchd agent and then starts the server in one step.
The raw scripts (`scripts/run-server.sh`, `scripts/install-launchagent.sh`,
`scripts/uninstall-launchagent.sh`) still work if you'd rather skip make.

The installer drops a plist at `~/Library/LaunchAgents/com.user.token-tracker.plist`
and registers it with `launchctl load -w` — the standard macOS path that EDR/MDM
tools expect.

The web UI also has a **re-ingest** button that triggers `POST /api/reingest` if you don't
want to wait.

### Note for humans and AI coding agents

If you want this server or the ingest job to run in the background, **use the supplied
installer above.** Do not use `launchctl submit` to register it ad-hoc. `launchctl submit`
is a legacy interface that registers a launchd job without writing a plist to disk; many
EDR products (Microsoft Defender for Endpoint, etc.) flag it as suspicious because
file-less persistence is a known malware pattern. The installer in this repo uses the
plist + `launchctl load` flow that EDR tools recognize as legitimate.

For one-off foreground runs, just invoke `./scripts/run-server.sh` directly.

## Server operations

This guidance is also available at `GET /api/docs/operations`, linked by the
`documentation_url` field in `GET /api/health`. The documentation endpoint does
not need a database connection. It describes supported launch methods; it does
not detect which method is currently in use.

### Console-launched server

Stop the server with Ctrl+C in the console that launched it. From the Token Tracker
project directory, run `make server` (or `./scripts/run-server.sh`) again. Keep that
console open. Verify `GET /api/health` responds successfully after restart.

### Service-managed server

If you configured the web server as a service, restart it through the service
manager and service definition used for that installation. This project does not
supply a web-server service definition, so there is no universal service name or
restart command. Check that service's configuration and logs; do not start a
second console server on the same port.

### Periodic ingestion is separate

The supplied `make agent` / `make down` commands manage the scheduled ingestion
job, not the web server. `make up` loads that ingestion job and then launches the
web server in the foreground. Restarting ingestion alone does not restore an
unavailable HTTP server.

### Access failures

A sandbox or permission restriction can prevent an agent from reaching a healthy
localhost server. Restarting the server does not resolve that restriction; use
the integration's permitted localhost execution route. If the server is unreachable,
online documentation is unavailable too: consult this README using your existing
installation or checkout. Do not guess installation paths, service names, or kill
unidentified port listeners.

## What it tracks

- **Totals & breakdowns**: input / output / cache-read / cache-write / reasoning tokens, cost $.
- **Time series**: daily token & cost charts.
- **Filters**: tool, model, project (cwd), date range — apply across the whole page.
- **MCP servers**: executed call counts, text-result size estimates, separate media counts, and errors; these estimates are not billed usage.
- **Per session**: open any row to see the full message timeline and MCP breakdown.

## Pricing

Edit `prices.json` to update USD-per-1M-token rates. Costs are computed at ingest time. After changing rates, explicitly use the
**recompute prices** button to update stored estimates. Only exact model IDs and explicitly listed aliases are used. Unknown/unverified Codex models retain their tokens with NULL cost. Prices are current standard API-equivalent estimates, not bills or subscription quota usage. Recompute is explicit. See [Codex repair and rebuild](docs/CODEX_REPAIR.md) before using an existing database.

## Layout

```
tracker/         core package
  db.py          schema + sqlite helpers
  parse_claude.py per-file parser (incremental, byte-offset resume)
  parse_codex.py Codex usage and structured MCP parsing
  codex_usage.py cumulative-counter reconciliation
  prompt_usage.py read-only turn index and configurable anomaly comparisons
  rebuild_codex.py prepares a separate database candidate and validation report
  ingest.py      walks both sources, upserts rows
  pricing.py     model → $/1M lookup
  api.py         FastAPI app (/api/stats, /api/mcp, /api/sessions, /api/reingest)
web/             single-page UI (vanilla JS + Chart.js)
scripts/         run-server.sh, run-ingest.sh, launchd plist + install
tests/           accounting, MCP, pricing and rebuild regression checks
prices.json      editable rate card
tokens.db        sqlite (generated; gitignored)
```

## How tokens are attributed

- **Claude**: every `type:"assistant"` line carries a `message.usage` block — we store one
  `messages` row per assistant turn, then sum into `sessions`.
- **Codex**: every `event_msg:token_count` event reports `last_token_usage` (per-turn delta)
  and `total_token_usage` (cumulative). We reconcile both, skip unchanged cumulative events, detect observed counter resets, and replay prefix state across incremental imports. Compaction/resume alone does not reset counters. Ambiguous last-only/partial usage is flagged and unpriced.
- **MCP**: Codex calls are counted from structured results/completions, including
  namespaced calls, legacy/custom outputs, `mcp_tool_call_end`, and completed/failed
  `item_completed:McpToolCall` records (type spelling: `McpToolCall`). Generated calls
  without execution evidence and names mentioned in code are not counted. Multiple
  representations share one call ID per source file. Text characters / 4 is a size
  estimate only; media blocks are counted separately. Exact per-tool billing is unavailable.
- **Classification**: originator overrides generic source labels for the entrypoint;
  user/guardian/subagent kinds are separate. Model and effort are retained per usage
  row, and each MCP call retains the model at invocation when available.


## License

Apache License 2.0 — see [LICENSE](LICENSE).
