# Codex accounting repair and activation

This guide describes the Codex accounting changes and the explicit database
rebuild procedure. Do not replace a running database without a reviewed backup
and activation plan.
The old database is deliberately rejected by the new initializer rather than
silently migrating it at server startup. Existing unrelated code/configuration
and Claude rows are preserved by the preparation procedure.

## What changed

- Cumulative counters are replayed over the already-read prefix. Unchanged
  counters do not emit usage; observed counter decreases start a new baseline.
  Compaction and resume alone do not reset the baseline. Missing totals use
  flagged last-only counts, which are subtracted from the next cumulative delta.
  Missing/partial/ambiguous usage remains visible but unpriced. A first event
  with inherited totals uses its reported last step, not inherited consumption.
- Fresh input is total input minus cached input. Reasoning is a subset of output.
  Codex cache writes are retained separately as a subset of fresh input; pricing
  applies the write premium without adding those tokens twice.
- Namespaced, legacy/custom output, `mcp_tool_call_end`, and completed/failed
  `item_completed/McpToolCall` records establish MCP execution. Call IDs deduplicate
  representations; pending calls/models survive import boundaries by prefix replay.
  Text characters exclude media/base64 fields; estimated text tokens are characters/4.
- Pricing uses exact IDs and explicit aliases only. Verified rates and source URLs
  are in prices.json. Unpriced steps produce NULL costs and an explicit coverage
  count. Aggregates remain unknown if any included step is unpriced; a separately
  labelled known subtotal remains available. MCP lifecycle dollar attribution was
  removed because model changes/replays cannot be billed precisely from these logs.
- Originator, source, session kind and reasoning effort are preserved. JetBrains
  originators take precedence over generic `source="vscode"`. Per-step model pricing
  never uses the session's final model for earlier usage.

## Reproducible checks

From the active project directory, use the existing tracker environment (no installs):

```sh
rtk proxy .venv/bin/python -m unittest discover -s tests -v
```

Prepare a new candidate at a new path; the command refuses an existing output or
source/output equality. The source is read-only; SQLite's backup API includes any
committed WAL records. Its backup is retained beside the candidate.

```sh
candidate_dir="$(mktemp -d)"
rtk proxy .venv/bin/python -m tracker.rebuild_codex --source tokens.db --output "$candidate_dir/candidate.db"
rtk proxy .venv/bin/python -m tests.validate_real_sessions "$candidate_dir/candidate.db.report.json"
```

Preparation copies non-Codex rows and ingestion history; reconstructs Codex from
the exact byte prefixes already imported in the snapshot; checks prefix hashes
before/after parsing; validates SQLite integrity; and writes a readiness report.
Missing or changed source prefixes fail closed. Newer log appends are left for the
next normal import. Reusing an output requires choosing a new filename, not deleting
the last known-good backup. The candidate contains private local metadata and must
not be committed or uploaded.

## Live activation — requires the user's approval

1. Stop the existing tracker server and ingestion job using their current launcher.
   Do not change Codex/IDE/RTK configuration or register a new background job.
2. Re-run preparation above at a new candidate path while writers are stopped. This
   creates a fresh, consistent backup of the current live database and its state.
3. Confirm the report and unchanged-source checks; retain both the source backup
   and a copy of the current source files for rollback. Compare the reviewed patch
   against the current checkout and abort if another process changed touched files.
4. With all DB connections closed, checkpoint any live WAL, verify the live DB still
   matches the fresh snapshot, and activate the candidate on the same filesystem
   using an atomic replacement. Do not leave old WAL/SHM sidecars beside the new DB.
5. Apply the reviewed code changes, preserving unrelated edits; restart the server
   and existing ingestion job. Inspect `/api/stats?tool=codex` and `/api/mcp?tool=codex`.
   Re-ingest once for log appends since the snapshot, then verify an unchanged pass
   adds zero rows/calls.
6. Rollback if needed: stop writers, restore the saved source files and SQLite backup
   together, remove only stopped/stale candidate WAL sidecars, and restart normally.

Activation remains separate from the preparation utility, so there is no
accidental live-write flag.

## Limits

- This is current standard API-equivalent pricing, not historical invoices, actual
  subscription limits, fast/flex/regional surcharges or all service/tool fees.
- `codex-auto-review` remains unpriced: no reliable public rate was established.
  Other Codex IDs without a verified explicit rate also remain unpriced.
- Partial/last-only counters cannot establish exact consumption with certainty;
  usage_status and diagnostics expose those cases. Tests cover mixed-model sessions;
  the captured real dataset has no sessions switching models.
- Prefix replay costs CPU proportional to file size on changed files. Unchanged
  files are skipped. Logs are read as data only; no historical code is executed.
- Historical log truncation/rewrites require an explicit rebuild. Media counts do
  not estimate image/audio tokens. Text-size estimates cannot be added to LLM usage.
- The existing FastAPI startup deprecation warning is unrelated and unchanged.

## Verified OpenAI sources (2026-09-05)

- https://developers.openai.com/api/docs/models/gpt-5.6-sol
- https://developers.openai.com/api/docs/models/gpt-5.6-luna
- https://developers.openai.com/api/docs/models/gpt-6-astra
- https://developers.openai.com/api/docs/models/gpt-5.4-mini
