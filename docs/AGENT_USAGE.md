# Agent usage: snapshot before work, delta afterward

## Recommended two-request workflow

1. **Before other task tools or commands**, send `POST /api/usage-snapshots` with
   your verified session ID. The server refreshes that session's logs, saves a
   checkpoint and returns `sid`. Keep this SID for the entire task.
2. **After finishing the work**, send `POST /api/usage-snapshots/{sid}/consumption`.
   The server refreshes the logs again and returns only newly logged consumption
   in that session since the checkpoint. No turn ID is required.

Create the snapshot (use `CODEX_THREAD_ID`, or fall back to `CODEX_SESSION_ID`):

```sh
rtk proxy curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"${CODEX_THREAD_ID:-${CODEX_SESSION_ID:?No session ID available}}\"}" \
  http://127.0.0.1:8732/api/usage-snapshots
```

The JSON response includes `sid`. Substitute it in the second request:

```sh
rtk proxy curl --fail --silent --show-error -X POST \
  http://127.0.0.1:8732/api/usage-snapshots/RETURNED_SID/consumption
```

Snapshot creation returns HTTP 201. Consumption returns HTTP 200. Neither SID nor
turn ID is automatically exported to the agent's environment. Save the returned
SID explicitly; it survives a server restart. Reusing it returns the refreshed
delta from the original baseline, not from the last check. A repeated report
updates one baseline-history entry rather than creating duplicates.

The snapshot is bound to the supplied session. **Other sessions, including separate
subagents and guardian reviews, are excluded.** Work sharing that same session
cannot be separated; do not reuse a SID across unrelated prompts. Snapshot deltas
are interval totals, while the older prompt endpoints below remain available for
whole-turn analysis.

Warnings compare against previously reported snapshot intervals with matching
project, model set, agent type and reasoning effort. The defaults remain 30 days,
20 samples, and consumption above both the 95th percentile and twice the median.
The same `days`, `min_samples`, `percentile` and `multiplier` query options apply.
History starts with this feature; the initial response will normally report
`insufficient_history`. Overlapping intervals are excluded from comparison.

Checkpoints and aggregate reports are stored in the gitignored local
`usage-snapshots.sqlite3`, separately from `tokens.db`. No live database rebuild is
needed. Source-prefix hashes protect against rewritten/truncated history: HTTP 409
means attribution is no longer safe. HTTP 503 means retry the same request once
logs are stable/accessible. A missing session log or unknown SID returns 404.
These records are retained until the local checkpoint database is removed; removing
it invalidates all SIDs and their warning history.

### Measurement limits and JetBrains access

This measures **newly logged usage**, not an exact wall-clock billing boundary.
The agent already needs some model processing to issue its first snapshot call.
Records delayed by the producer may appear after the checkpoint even if processing
began earlier. A final check cannot include the final answer generated afterward.
Label the result “usage since snapshot, as currently logged”; request the same SID
later if you need to include late/final usage, before unrelated work in that session.
MCP results for known pre-snapshot invocations are excluded from new-call counts.
Prices are evaluated at report time and remain API-equivalent estimates.

Check `CODEX_THREAD_ID` and `CODEX_SESSION_ID` in the agent execution environment;
when both are present, they must match. `CODEX_TURN_ID` is not required.
Some JetBrains environments restrict localhost access inside the sandbox. Use the integration's approved
execution route for localhost requests. If identity is missing or conflicting,
or access is unavailable, report the limitation instead of guessing.

The server must run on the same machine. No settings, skills, automatic polling
or MCP server are installed by this feature.

## Find and request a turn

### Where the identifiers come from

`YOUR_SESSION_UUID` and `YOUR_TURN_ID` below are placeholders, **not predefined
agent variables**. A session identifies the conversation; a turn identifies one
prompt execution inside it.

Session-variable availability depends on the integration. Check the agent's
command environment for `CODEX_THREAD_ID` and `CODEX_SESSION_ID`; do not assume
`CODEX_TURN_ID` exists. Availability in JetBrains must be checked locally.

When available, use `CODEX_THREAD_ID`, falling back to `CODEX_SESSION_ID`. This
first request deliberately omits `turn_id` and fails before making the request
if neither session variable is populated:

```sh
rtk proxy curl --fail --silent --show-error --get \
  http://127.0.0.1:8732/api/prompt-usage \
  --data-urlencode "session_id=${CODEX_THREAD_ID:-${CODEX_SESSION_ID:?No session ID available}}"
```

The tracker reads the turn ID from structured local log records and returns it
as `turn_id` in the JSON response. Save that returned ID for later checkpoints
of the same prompt. It is **not automatically exported as an environment variable**.
If the current turn has not reached the logs yet, the latest result can still be
the previous turn: check its start time and status before treating it as current,
and retry after logs flush if necessary.

If neither session variable exists, or they disagree, obtain a verified session
ID from the integration before reporting consumption. Do not guess from the most
recent project entry. Opening `/api/prompt-usage` without `session_id` returns a
validation error because this server cannot identify the caller automatically.

### Requests with explicit identifiers

Discover identifiers for a known session (the `codex:` prefix is optional):

```sh
rtk proxy curl --fail --silent --show-error --get \
  http://127.0.0.1:8732/api/prompts \
  --data-urlencode 'session_id=YOUR_SESSION_UUID' --data-urlencode 'limit=10'
```

Alternatively filter `/api/prompts` by `project` using its exact absolute directory.
This lists candidates; it does **not** identify which concurrent session belongs to
the caller. Verify the session ID before reporting usage. Prompt text is never
returned. Discovery includes turn IDs, timestamps, models, session kinds and usage.

Request a specific turn:

```sh
rtk proxy curl --fail --silent --show-error --get \
  http://127.0.0.1:8732/api/prompt-usage \
  --data-urlencode 'session_id=YOUR_SESSION_UUID' \
  --data-urlencode 'turn_id=YOUR_TURN_ID'
```

Omitting `turn_id` selects the latest logged turn **within the supplied session**.
Use the returned ID for subsequent requests so a new prompt cannot change the
meaning of a checkpoint. Unknown or conflicting turns return 404; retry after logs
flush or inspect discovery. Invalid options return 422.

## Report semantics

- `tokens.total` = fresh input + cached input + output, summed across the prompt's
  model steps. This measures repeated context processing too, not just the words
  typed in the prompt. `tokens.input` includes cached input.
- `tokens.reasoning_in_output` is a subset of output; never add it to the total.
- `pricing` supplies the known API-equivalent estimate and priced/unpriced step
  coverage. A wholly unpriced turn has a null estimate, not zero. These are current
  API equivalents, not subscription quota usage or actual billing.
- `mcp` counts completed calls and errors, attributed to the originating call's
  turn when available, even if its result arrives in a later turn.
- `status` is `running`, `completed`, `aborted` or `unknown_end`. `boundary` is
  `explicit` or `approximate`; older user-message/context boundaries are inferred.
- `freshness` reports refresh time, unreadable/changing files, conflicting turns
  and unattributed usage steps. It is the time logs were checked, not a guarantee
  the producer has flushed its final usage. An unavailable file is omitted for
  that refresh rather than served with stale attribution.
- Scope is **one session's turn**. Separate guardian/subagent sessions are not
  rolled into the parent. Steering messages inside an explicit running turn stay
  in that turn. Claude prompt indexing is not implemented.

## Unusually high usage

The default baseline consists of completed turns in the preceding 30 days with
the same project, agent type, model set and reasoning-effort set. It excludes the
target, later completions, approximate boundaries and uncertain usage records.

With at least 20 comparable turns, each metric warns only when it exceeds **both
the 95th percentile and twice the median**. Metrics are fresh input, output, total
tokens and API-equivalent cost. Cost comparisons require fully priced turns on
both sides; token comparisons still work for unpriced models. The comparison is
statistical, not a judgment about whether the task warranted the work.

Optional query parameters:

| Parameter | Default | Allowed values |
|-----------|---------|----------------|
| `days` | 30 | 1–365 |
| `min_samples` | 20 | 2–1000 |
| `percentile` | 95 | 50–99.9 |
| `multiplier` | 2 | 1–100 |

`warning.status` is `high`, `normal`, `insufficient_history` or `uncertain_usage`.
The response includes cohort size, per-metric median, percentile, threshold and
current value. No history means no “normal” claim. This is an on-request warning;
it does not push notifications, stop the agent or impose a budget.

## Instruction to give an agent

> Before executing other task tools or commands, create a Token Tracker snapshot
> with POST /api/usage-snapshots and your verified CODEX_THREAD_ID (fallback:
> CODEX_SESSION_ID). Keep the returned sid. Do not guess a session or require a
> turn ID. After finishing the work, POST /api/usage-snapshots/{sid}/consumption.
> Report the returned token delta, known estimated cost, unpriced coverage, MCP
> counts/errors and any high-usage warning. Say “usage since snapshot, as currently
> logged”; separate subagent sessions and the not-yet-generated final answer are
> excluded. Reuse the same SID for retries. If identity or localhost access is
> unavailable, say that measurement could not be established. Use the integration's
> approved execution route; do not change settings or poll after every tool call.

After completion, a later request can retrieve the final logged usage. Even a
completion event can precede a final token update; allow logs to settle. The
agent's own report request/response can add usage, so these checkpoints are not
zero-cost instrumentation.

## Implementation and validation

`tracker/prompt_usage.py` maintains a disposable in-memory index keyed by file
identity, size and timestamps. Changed files are reparsed with the existing
cumulative-counter/MCP parser; unchanged files reuse aggregate metadata. Price
changes invalidate the index. No prompt text or tool output is retained in it,
and neither endpoint writes to `tokens.db`. Historical commands are never run.

Regression checks cover per-turn sums, duplicate events, incremental refresh,
partial lines, compaction, resets, mixed models, unknown pricing, late MCP results,
inferred boundaries, copied/conflicting files, changing-file races, cohort
selection and the HTTP endpoints. Run from the project directory:

```sh
rtk proxy .venv/bin/python -m unittest discover -s tests -q
```
