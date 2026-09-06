# Data access and accounting

## Read-only access

Use Python sqlite3 with `Path(db).resolve().as_uri() + "?mode=ro"`, `uri=True`, `PRAGMA query_only=ON` and one explicit read transaction. Close promptly. Do not call tracker.db.connect/init: they configure WAL or migrate. Do not call ingestion, rebuild, snapshot creation or consumption endpoints during this audit. Do not copy a live SQLite file without its WAL; use SQLite backup to an agreed temporary destination if a coherent copy is needed. Do not set immutable on an active database.

Inspect sqlite_master and PRAGMA table_info before queries. Unsupported legacy schemas require reporting limitations, not migration. Parameterize project and date values. Use half-open intervals [start, end), normalized to UTC from the stated timezone.

## Current normalized schema

- sessions: source-qualified id, cwd, tool, session_kind, originator/source, timestamps; model is only the last model. Session totals are lifetime values.
- messages: session_id, ts, model, reasoning_effort, input_tokens, cache_read, cache_write_5m/cache_write_1h, output_tokens, reasoning_tokens, usage_status, cost_status, est_cost_usd, source_file/source_line, agent_id/agent_type.
- mcp_calls: session_id, ts, server/tool_name, call_id, completed, is_error, result_chars, est_result_tokens, media_count, source_file/source_line.
- ingest_runs: finished_at, error; newest successful completion indicates import freshness, not guaranteed log completeness.

In this repaired database input_tokens is already normalized fresh input (Codex cache was subtracted by the parser). Fresh = input_tokens + cache_write_5m + cache_write_1h; cached = cache_read; total = fresh + cached + output_tokens. Never subtract cache_read again. cache_write_input_tokens is an overlapping pricing diagnostic, not an additive category. reasoning_tokens is included in output. Raw provider fields can have different semantics; use reconciled parser records rather than blindly summing raw events.

Aggregate messages by session and time, preserving per-message model/cost. Sum non-null costs only as a known subtotal alongside unpriced steps/tokens. All-unpriced cost is unknown, not zero. Do not reprice history silently. Inspect usage_status uncertainty separately. Never join messages directly to all MCP rows before summing (fanout multiplies consumption); aggregate independently.

The helper provides session totals, top models/MCP tools, source kinds, baseline and quality indicators. It intentionally returns no conversation text or source paths. For selected session IDs query DISTINCT source_file and bounded source_line ranges with parameters. Check identity and source availability before archive reads.

## Discussions and prompts

Use explicit parent links from supported metadata when grouping Codex children; see the installed tracker/discussions.py and docs/DISCUSSIONS.md. Claude messages can already include child agents in one session: distinguish agent_id/agent_type and never add those child logs again. Project cwd aliases/worktrees require confirmed membership. Report unmatched children and aliases, not speculative links.

The /api/discussions date filter selects discussions but preserves lifetime totals: do not use its totals as period consumption. /api/discussion-previews only exposes first prompts, not full prompt histories.

messages does not store user prompt text or universal turn IDs. For selected Codex logs tracker/prompt_usage.py read_turns exposes boundary=explicit/approximate and unmatched rows. It reparses whole selected files and may recompute prices using current rates: use it cautiously, disclose differences, prefer stored message costs joined by source_file/source_line. Retain bounded archive scans and fail explicitly on truncation. Multiple user steering messages can share a turn; do not charge its full usage to each message. Use existing first_prompts.py cleaning rules where relevant, without assuming first-prompt extraction enumerates all prompts. Do not copy cached priorConversation prompts into the current session's human prompt list.

For other sources, inspect the supported parser and message identities; where turn attribution cannot be established, rank sessions or measured intervals only. Never fabricate prompt-level granularity.

## Snapshots

usage-snapshots.sqlite3 stores JSON bodies in snapshots(sid, body) and reports(sid, body). Inspect only selected bodies/fields, not SELECT * dumps. These are baselines and latest reports, not an exhaustive immutable prompt history. Same-session intervals can overlap; deduplicate SID and do not sum overlaps or add reports to messages. Newly logged deltas can include delayed records and exclude the final answer emitted afterward. Cross-database reads need not share one instant: state their respective cutoffs.

## Interpretation

MCP result character estimates exclude media and do not equal billed LLM usage. Error counts are recorded errors, not proof every failure was captured. Compare call frequency/output/retries with observed subsequent context before proposing savings. Missing archives permit aggregate findings but not invented examples. Redact sensitive snippets before reporting; archive text entering the agent context is not guaranteed to stay on the machine.
