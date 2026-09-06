# Audit report template

Write in the user's language. Replace fields with evidence; omit unsupported precision.

## Audit identity and scope

Audit ID; project membership; [start,end) and timezone; comparison dates; sources; ingestion cutoff; read-only method. Quality: supported / exploratory / insufficient, with reasons. Session count, attributable prompt count (or unknown), active days, archive coverage, pricing coverage, and share examined.

## Findings at a glance

Tokens: fresh / cached / output (reasoning is included). Known API estimate and unpriced share. Main/child/review contribution where attributable. Model and MCP differences. Distinguish volume changes from efficiency changes.

## Largest contributors

| Rank | Discussion/session and date | Period tokens | Share | Fresh/cache/output | Known cost and coverage | Attribution |
|---|---|---|---|---|---|---|

For identified prompts, add turn ID/boundary confidence and a short redacted excerpt. Keep lifetime totals in a separate labeled column if useful. Describe sample selection and comparable cheaper work.

## OBS-001 — Observation

Evidence IDs, timestamps and source/event references. Short authentic excerpt or explicitly labeled paraphrase. Measurements and comparison. Measured fact versus probable cause; alternative explanation and missing evidence.

## Optimization selection

| Reference | Priority | Proposed change | Expected reduction and scope | Confidence | Tradeoff/dependencies |
|---|---|---|---|---|---|

### OPT-001 — Change

- Related observations: OBS references.
- Exact target: relative file and section/line, skill, MCP or workflow.
- Proposed edit: concrete replacement text/diff or precise behavioral change.
- Why it should work: mechanism tied to evidence.
- Expected reduction: affected-work range; project share and weighted range; assumptions. Unknown where not defensible. Tokens and cost distinguished.
- Confidence and counterexamples.
- Quality/latency/security/maintenance tradeoffs.
- Dependencies, incompatible alternatives and overlapping savings.
- Validation: comparable task, baseline and success/quality criteria.
- Rollback.

## Limits and next step

Explain missing logs, approximate boundaries, partial costs and insufficiency. Ask which OPT references to implement, all compatible changes or none; await selection. Do not add independent savings ranges when they overlap.

## After selected implementation

For each OPT: applied files, checks, unresolved limits, rollback. A subsequent measurement records predicted versus observed changes, workload comparability and effective/inconclusive/adverse/insufficient-data outcome. No unrequested recurring monitoring.
