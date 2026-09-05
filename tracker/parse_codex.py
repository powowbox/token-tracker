"""Parse ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl files.

Relevant record types:
- session_meta: payload.{id, cwd, model_provider, cli_version}
- turn_context: payload.{cwd, model}
- response_item:function_call: name, call_id, arguments  (we track MCP via mcp__ prefix)
- response_item:function_call_output: call_id, output  (string)
- event_msg:mcp_tool_call_end: authoritative MCP completion, including wrapped calls
- event_msg:token_count: info.last_token_usage / total_token_usage  -> emit one MessageRow per `last_token_usage` (delta)

Token attribution: reconcile cumulative counters with last usage. Prefix replay
preserves counters and model metadata across incremental ingestion boundaries.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from .codex_usage import UsageCounter
from pathlib import Path

from .parse_claude import MessageRow, McpCallRow, SessionMeta, ParsedFile, _mcp_parse_name

CODEX_ROOT = Path.home() / ".codex" / "sessions"


MEDIA = {'image', 'audio', 'image_url', 'input_image', 'output_image', 'input_audio', 'output_audio'}


def result_stats(result):
    """Text characters, error flag, media blocks; never count binary payloads."""
    if isinstance(result, str):
        try:
            decoded = json.loads(result)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            return result_stats(decoded)
        return len(re.sub(r'data:[^\s,]+;base64,[A-Za-z0-9+/=]+', '', result)), 0, 0
    if isinstance(result, list):
        parts = [result_stats(x) for x in result]
        return sum(x[0] for x in parts), int(any(x[1] for x in parts)), sum(x[2] for x in parts)
    if not isinstance(result, dict):
        return (0 if result is None else len(str(result))), 0, 0
    if str(result.get('type', '')).lower() in MEDIA or str(result.get('mimeType','')).startswith(('image/', 'audio/')):
        return 0, 0, 1
    error = int(bool(result.get('isError') or result.get('is_error') or result.get('error')))
    if 'Err' in result:
        n, _, media = result_stats(result['Err'])
        return n, 1, media
    if 'Ok' in result:
        n, e, media = result_stats(result['Ok'])
        return n, max(e, error), media
    if 'content' in result:
        n, e, media = result_stats(result['content'])
        return n, max(e, error), media
    if isinstance(result.get('text'), str):
        n, _, _ = result_stats(result['text'])
        return n, error, 0
    parts = [result_stats(v) for k, v in result.items()
             if k not in {'data', 'base64', 'blob', 'bytes', '_meta', 'type', 'isError', 'is_error', 'mimeType'}]
    return sum(x[0] for x in parts), max(error, int(any(x[1] for x in parts))), sum(x[2] for x in parts)


def _result_stats(result):
    return result_stats(result)[:2]


def mcp_name(payload):
    name, namespace = payload.get('name', ''), payload.get('namespace', '')
    if namespace.startswith('mcp__') and name:
        return namespace[5:], name
    return _mcp_parse_name(name)


def classify(source):
    if isinstance(source, str) and source.lower() in ('guardian', 'auto_review', 'automatic_review'):
        return 'guardian'
    if source == 'subagent':
        return 'subagent'
    if isinstance(source, dict) and 'subagent' in source:
        child = source['subagent']
        if isinstance(child, dict) and any(str(v).lower() in ('guardian', 'auto_review', 'automatic_review') for v in child.values()):
            return 'guardian'
        return 'subagent'
    return 'user' if source is not None else 'unknown'


def parse_file(path: Path, *, start_offset: int = 0, end_offset: int | None = None) -> tuple[ParsedFile, int]:
    session_uuid = ""
    # File name pattern: rollout-2026-03-04T18-51-02-<uuid>.jsonl
    try:
        parts = path.stem.split("-")
        # uuid is last 5 dash-joined chunks
        session_uuid = "-".join(parts[-5:])
    except Exception:
        session_uuid = path.stem

    session_id = f"codex:{session_uuid}"
    meta = SessionMeta(
        session_id=session_id,
        tool="codex",
        session_uuid=session_uuid,
        cwd=None,
        model=None,
        started_at=None,
        ended_at=None,
        entrypoint="codex",
    )
    parsed = ParsedFile(session=meta)

    pending: dict[str, McpCallRow] = {}
    counter = UsageCounter()
    diagnostics = Counter()

    # Replay prefix metadata/calls so results arriving in a later ingest still
    # resolve to their original call. Emit only rows touched after start_offset.
    emitted: dict[str, McpCallRow] = {}
    completed: set[str] = set()
    line_no = 0
    with open(path, "rb") as f:
        buf = f.read() if end_offset is None else f.read(end_offset)

    parts_split = buf.split(b"\n")
    consumed = 0
    for i, raw in enumerate(parts_split):
        is_last = (i == len(parts_split) - 1)
        if is_last and raw:
            break
        historical = consumed < start_offset
        line_no += 1
        consumed += len(raw) + (1 if not is_last else 0)
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        ts = d.get("timestamp")
        if ts:
            if meta.started_at is None or ts < meta.started_at:
                meta.started_at = ts
            if meta.ended_at is None or ts > meta.ended_at:
                meta.ended_at = ts

        t = d.get("type")
        payload = d.get("payload") or {}

        if t == "session_meta":
            if payload.get("id"):
                meta.session_uuid = payload["id"]
                meta.session_id = f"codex:{payload['id']}"
                session_id = meta.session_id
            if payload.get("cwd"):
                meta.cwd = payload["cwd"]
            meta.originator = payload.get("originator") or meta.originator
            if "source" in payload:
                meta.source = json.dumps(payload["source"], sort_keys=True)
                meta.session_kind = classify(payload["source"])
            meta.entrypoint = meta.originator or (payload.get("source") if isinstance(payload.get("source"), str) else "codex")
            meta.model = payload.get("model") or meta.model

        elif t == "turn_context":
            if payload.get("cwd") and not meta.cwd:
                meta.cwd = payload["cwd"]
            if payload.get("model"):
                meta.model = payload["model"]
            meta.reasoning_effort = payload.get("effort") or payload.get("reasoning_effort") or meta.reasoning_effort

        elif t == "compacted":
            counter.compacted = True
        elif t == "event_msg":
            ptype = payload.get("type")
            if ptype == "context_compacted":
                counter.compacted = True
            if ptype == "item_completed":
                item = payload.get("item") or {}
                if item.get("type") == "ContextCompaction":
                    counter.compacted = True
                if item.get("type") in ("McpToolCall", "mcp_tool_call") and item.get("status") in ("completed", "failed"):
                    payload = dict(payload, invocation={"server": item.get("server"), "tool": item.get("tool")},
                                   call_id=item.get("call_id") or item.get("id"), result=item.get("result"),
                                   completion_error=item.get("error"), failed=item.get("status") == "failed")
                    ptype = "mcp_tool_call_end"
            if ptype == "mcp_tool_call_end":
                invocation = payload.get("invocation") or {}
                server, tool_name = invocation.get("server"), invocation.get("tool")
                call_id = payload.get("call_id")
                if not server or not tool_name or not call_id:
                    continue
                row = pending.get(call_id)
                if row is None:
                    row = McpCallRow(
                        session_id=session_id, tool="codex", ts=ts or "",
                        server=server, tool_name=tool_name, call_id=call_id,
                        source_file=str(path), source_line=line_no, model=meta.model,
                    )
                    pending[call_id] = row
                row.result_chars, row.is_error, row.media_count = result_stats(payload.get("result"))
                if payload.get("failed") or payload.get("completion_error"):
                    row.is_error = 1
                    if not row.result_chars:
                        row.result_chars, _, _ = result_stats(payload.get("completion_error"))
                row.completed = True
                completed.add(call_id)
                if not historical:
                    emitted[call_id] = row
            elif ptype == "token_count":
                last, status = counter.consume(payload.get("info"))
                if not historical:
                    diagnostics[status] += 1
                if last is None or historical:
                    continue
                input_tokens = last["input_tokens"]
                cached = last["cached_input_tokens"]
                output_tokens = last["output_tokens"]
                reasoning = last["reasoning_output_tokens"]
                uncached_in = input_tokens - cached
                row = MessageRow(
                    session_id=session_id,
                    tool="codex",
                    ts=ts or "",
                    model=meta.model,
                    input_tokens=uncached_in,
                    output_tokens=output_tokens,
                    cache_read=cached,
                    cache_write_5m=0,
                    cache_write_1h=0,
                    reasoning_tokens=reasoning,
                    source_file=str(path),
                    source_line=line_no,
                    reasoning_effort=meta.reasoning_effort,
                    usage_status=status,
                    cache_write_input_tokens=last["cache_write_input_tokens"],
                    agent_type=meta.session_kind if meta.session_kind in ("guardian", "subagent") else None,
                    agent_id=meta.session_uuid if meta.session_kind in ("guardian", "subagent") else None,
                )
                parsed.messages.append(row)

        elif t == "response_item":
            ptype = payload.get("type")
            if ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name", "")
                call_id = payload.get("call_id")
                if not call_id:
                    continue
                parsed_name = mcp_name(payload)
                if parsed_name is None:
                    continue
                server, tool_name = parsed_name
                row = pending.get(call_id) or McpCallRow(
                    session_id=session_id,
                    tool="codex",
                    ts=ts or "",
                    server=server,
                    tool_name=tool_name,
                    call_id=call_id,
                    source_file=str(path),
                    source_line=line_no, model=meta.model, completed=False,
                )
                # A generated call alone is not proof of execution.
                pending[call_id] = row
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                if call_id and call_id in pending:
                    row = pending[call_id]
                    # Completion events are authoritative if both forms exist.
                    if call_id not in completed:
                        row.result_chars, row.is_error, row.media_count = result_stats(payload.get("output"))
                    row.completed = True
                    if not historical:
                        emitted[call_id] = row

    parsed.mcp_calls = list(emitted.values())
    parsed.diagnostics = dict(diagnostics)
    return parsed, consumed


def discover_files() -> list[Path]:
    if not CODEX_ROOT.exists():
        return []
    return sorted(CODEX_ROOT.glob("*/*/*/*.jsonl"))
