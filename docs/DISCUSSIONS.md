# Discussion leaderboard

## What is counted

The leaderboard measures **complete discussions**, using the imported message rows
in `tokens.db`. It never sums snapshot reports from `usage-snapshots.sqlite3`, never
reprices messages, and never triggers ingestion when opened.

Total tokens = fresh input + cached input + output. Claude cache writes belong to
fresh input; Codex cache-write subsets are not added again. Reasoning is already
included in output. Each model's stored per-message estimate is preserved, including
mixed-model discussions. No model-family fallback pricing is introduced.

The date, source, project, model, agent and entrypoint filters select discussions
having a matching imported usage record; totals then include their full imported
lifetime within the chosen main-only/include-children scope. A model filter therefore
does not remove other models from a matching discussion. Multiple filters must match
the same record. The separate **Rank** column is computed by total tokens descending within the filtered
selection, with stable ID tie-breaking. It does not change when sorting or paging;
filters, scope or new data can change it. Discussion titles have a separate column.
Click a column header to sort, and click again to reverse the direction. Activity uses
the last recorded activity; title sorting ignores case. The order selector also works
on mobile. Sorts apply to the full result before pagination, with stable ID tie-breaking;
missing values (including unpriced costs) sort last in either direction. Discussions without any matching usage are not listed.

## Titles and relationships

Codex titles come from `session_index.jsonl` (`thread_name`), then the newest supported
`state_*.sqlite` schema's `threads.name`. The default metadata directory is
`CODEX_HOME` when set, otherwise the user's `.codex` directory. These stores are
opened read-only. `threads.title` and `first_user_message` are never selected.
Fallback titles contain only the project basename and session start date.
Other supported sources currently use that fallback.

Only explicit Codex relationships are accepted:

- `session_meta.payload.parent_thread_id` in an imported log's bounded header;
- `source.subagent.thread_spawn.parent_thread_id`;
- `thread_spawn_edges.parent_thread_id` / `child_thread_id` in the supported local state database.

Repeated representations of the same link collapse to one relationship. Descendants
are included once. Missing parents, conflicting parents and cycles are quarantined
in the **unattached agents** view, with a reason. Cycles and sessions leading into
them are not rolled up. A guardian/subagent without a known parent stays unattached.
No relationship is guessed from a title, project, timestamp, nickname or prompt.

Claude subagent rows already share their parent session ID. They contribute once,
and the main-only option excludes rows carrying subagent identity/type. A child
count uses distinct agent IDs, not message counts. Sessions retain their original
classification and originator; `source="vscode"` is never used here to relabel JetBrains.

Metadata reads are cached: titles and state edges refresh at most every five seconds;
log headers reuse file signatures. Missing metadata degrades to fallback names and
verified links only. Metadata can change independently of usage ingestion.

## Costs and freshness

- `complete`: all selected imported steps have stored prices.
- `partial`: a known subtotal is available, but some steps remain unpriced.
- `unavailable`: no selected step has a stored price; cost is `null`, not zero.

A real stored zero price remains valid. Detail responses include priced/unpriced step
counts, unpriced tokens and token-based coverage. Complete pricing only describes
imported data; it does not prove the producer has flushed every record.

The last **successful** ingestion timestamp is displayed separately. Use the existing
re-ingest control or scheduled ingestion to import newer logs. Neither the leaderboard
nor its detail view refreshes usage implicitly. Current or deleted/unavailable logs,
missing metadata and missing parent imports can limit the visible history. API-equivalent
estimates are not invoices or subscription quota usage.

## Title and first-prompt filter

The Discussion header contains a search field. Filtering starts after **three trimmed
characters**, with a 250 ms debounce. It matches a literal substring of discussion
titles or the complete cleaned first prompt without regard to case, across all result
pages. Returning to two characters or fewer removes the text filter. Sorting and other filters remain active; changing
the search resets pagination and recomputes ranks within the matching selection.
The field stays available when there are no matches, retains its value and focus,
and is also visible on mobile. Reset clears it. First prompts are read only for discussions that do not already match by title,
after the other filters have selected candidates. Unavailable prompts cannot match
by content, but their titles remain searchable. Search therefore may read prompts
beyond the visible page; normal unfiltered preview loading remains page-scoped.

## First-prompt previews

The line below a discussion title now shows its first user prompt, replacing the
project/source/model summary. Project and model information remains in the detail.
The preview is limited visually to two lines with an ellipsis. Hover for the complete
plain text, preserving paragraphs; move into the popup to scroll long prompts.
Keyboard focus opens it, Enter/Space/Down moves into the scrollable text, and Escape
closes it. Touch users tap to open and use close or tap outside to dismiss.

Previews load in a separate batch **after** the leaderboard renders. Only the visible
page is requested. Changing page/filters cancels the old request and ignores stale
responses. Preview failures never remove the ranking or its usage data.

`POST /api/discussion-previews` accepts `{"session_ids":["codex:<id>","claude:<id>"]}`
with 1–20 source-qualified IDs. It returns a `previews` mapping, each value containing
`status` and `text` (null when unavailable). No arbitrary source-file path is accepted.
The response has `Cache-Control: no-store`; the texts are not persisted in either
tracker database or logged by this feature.

Statuses are `available`, `no_text` (attachment-only), `not_identifiable` (missing or
ambiguous beginning/identity), `unavailable` (missing/unreadable file), and
`limit_exceeded`. The server never silently substitutes a later user message.

The reader supports Codex structured user messages/events and Claude user records.
It removes recognized leading integration envelopes (including `recommended_plugins` and the structured ChatGPT conversation reference preceding `My request`), skips marked Claude metadata,
and excludes tools, assistant messages and child-agent sources. Copies of a log must
agree on the first prompt. Missing prefixes, compaction before a prompt, unsupported
content and malformed records fail closed. This is not a semantic classifier of
arbitrary integration instructions: unrecognized formats may require an adapter update.

Reads stop at the first identifiable prompt, with limits of 512 lines / 2 MiB per
file, eight files per session and 256 KiB of prompt text. Oversized prompts are
reported unavailable instead of presenting truncated text as complete. File-signature
checks detect changes during reads. The in-memory LRU holds up to 512 entries / 4 MiB
of text plus entry accounting; successes expire after five minutes and negative results
after five seconds. File changes invalidate entries immediately on the next request.

Measured on ten real first-page discussions: **3–7 ms** for the preview batch without
application cache, approximately **0.4 ms** cached; response size about **110 kB**.
These are local server-side measurements, not an end-to-end latency guarantee; OS file
caching may already be warm. The table does not wait for this batch, and opening an
already loaded popup makes no additional request. Synthetic browser checks cover
hover/keyboard/touch, long text, HTML treated literally, failures and stale requests.

## API

`GET /api/discussions`

| Parameter | Values / default |
|---|---|
| `tool` | `codex`, `claude`; all by default |
| `project`, `model`, `agent`, `entrypoint` | Exact matches; `agent=main` selects main usage |
| `start`, `end` | ISO dates/timestamps; selects activity, keeps lifetime totals |
| `include_children` | `true` (default), `false` |
| `view` | `discussions` (default), `unattached` |
| `pricing` | `complete`, `partial`, `unavailable`; all by default |
| `sort` | `rank`, `tokens` (API default), `cost`, `fresh_input`, `output`, `title`, `activity`, `subagents` |
| `search` | Title or first-prompt substring, active from 3 trimmed characters; at most 200 characters |
| `direction` | `desc` (default), `asc` |
| `limit`, `offset` | 1–100 (default 20), nonnegative (default 0) |

The response contains `discussions`, `total`, pagination, `totals_scope`,
`last_successful_ingestion`, `title_metadata_available` and `unattached_sessions`.
Costs use `known_cost` with an explicit `pricing_status`.

`GET /api/discussions/{source-qualified-root-id}?include_children=true`

Returns lifetime totals, main/child totals, model breakdown and included session
metadata. Use the root ID returned by the list endpoint. Unknown/non-root IDs return
404; invalid filters or pagination return 422. Reads use a consistent SQLite read
transaction. Aggregates run in SQL; raw messages are not returned.

## Validation and rollout

Run `python -m unittest discover -s tests -q` in the project environment.
Focused fixtures cover mixed models, reasoning overlap, full trees, duplicate links,
cycles, missing/conflicting parents, Claude children, lifetime filters, title priority
and renames, unavailable metadata, unknown/zero prices, pagination and API validation.
The complete suite passed 77 tests after the first-prompt preview addition.

A read-only source backup to a temporary SQLite database passed integrity checks and
preserved aggregate token counts and the known cost subtotal exactly after grouping.
Repeated leaderboard requests added no data. Desktop/mobile checks use synthetic data,
so no real titles or conversations are committed as fixtures or screenshots.

There is **no schema migration, rebuild, or snapshot format change**. Restart the
server after updating; refresh the browser. Existing ingestion and agent snapshot
workflows remain separate. No personal installation paths or credentials are needed
in repository configuration.
