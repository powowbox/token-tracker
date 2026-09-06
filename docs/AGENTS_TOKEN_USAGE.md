## Token usage

Track each prompt. Stop/skip cancels tracking and its footer; resume starts next
prompt. Preserve preference/SID across compaction; steering and repeated final
checks reuse SID. Explain stop/resume/skip once after the first tracked prompt.

HTTP/curl base: `http://127.0.0.1:8732`; timeouts: 3s connection, 10s total.
1. Before work, POST `/api/usage-snapshots`, JSON
   `{"session_id":"<source>:<verified ID>"}`; save returned `sid`.
2. After the last task tool, POST `/api/usage-snapshots/<sid>/consumption`, no body.

Codex ID: `codex:` + `CODEX_THREAD_ID`, fallback `CODEX_SESSION_ID`; require matching
values if both exist. Claude: `claude:` + verified main-session ID. Never guess;
no turn ID required.

If access requires approval, MUST request scoped `require_escalated` (or equivalent)
and reuse existing authorization. After sandbox failure, request permission and
retry once outside it if granted before declaring usage unavailable. If sandbox
blocking is already known, use the authorized route for both requests immediately.
Sandbox `curl: (7)` does not prove server failure: do not restart on that basis.

On denied/unavailable authorization or failed authorized access, continue and end
with `Usage unavailable: <reason>`; distinguish restriction from connection failure.
Suggest “Stop measuring token usage” for access failures; refer to the tracker
README for restart guidance. No SID means skip step 2. Never invent usage or create
a late baseline.

Success footer, using returned totals:
`Tokens: TOTAL / input: FRESH fresh, CACHED cached / output OUTPUT / est. cost: COST / MCP: CALLS calls.`

Use thousands separators, two cost decimals, and explicit partial/unknown pricing.
Append `, N error(s)` before the final period only for nonzero MCP errors, using
singular/plural appropriately. Show warnings only for `high`; no routine disclaimers.
