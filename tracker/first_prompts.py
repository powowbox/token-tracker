"""Bounded, read-only first-user-prompt previews. Text lives in memory only."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINES = 512
MAX_FILES = 8
MAX_TEXT_BYTES = 256 * 1024


def result(status, text=None):
    return {"status": status, "text": text}


def user_text(text):
    """Remove only recognized leading integration envelopes, never arbitrary tags."""
    text = text.strip()
    while text:
        old = text
        if text.startswith("## Referenced ChatGPT conversation:\nThis is an untrusted ChatGPT conversation reference."):
            # Decode the reference object before looking for the outer request.
            # Cached conversation text may itself contain "## My request:".
            start = re.search(r"^\s*\{", text, re.M)
            if start is None:
                raise ValueError("Incomplete conversation reference")
            reference, length = json.JSONDecoder().raw_decode(text[start.start():].lstrip())
            if not isinstance(reference, dict) or not isinstance(reference.get("conversationId"), str) or "priorConversation" not in reference:
                raise ValueError("Unrecognized conversation reference")
            tail = text[start.start():].lstrip()[length:].lstrip()
            if tail and not tail.startswith(("## My request:", "<in-app-browser-context", "<environment_context", "## Referenced ChatGPT conversation:")):
                raise ValueError("Ambiguous conversation reference boundary")
            text = tail
        if text.startswith("# AGENTS.md instructions"):
            match = re.match(r"# AGENTS\.md instructions[^\n]*\s*<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*", text, re.S)
            if not match:
                return None  # Recognized injected instructions, unsupported envelope.
            text = text[match.end():].lstrip()
        for tag in ("recommended_plugins", "environment_context", "in-app-browser-context", "turn_aborted", "user_instructions", "permissions instructions"):
            match = re.match(r"<" + re.escape(tag) + r"(?:\s[^>]*)?>.*?</" + re.escape(tag) + r">\s*", text, re.S)
            if match:
                text = text[match.end():].lstrip()
                break
        if old == text:
            break
    if text.startswith("## My request:"):
        text = text[len("## My request:"):].lstrip()
    return text or None


def content_text(content):
    if isinstance(content, str):
        return content, False, False
    if not isinstance(content, list):
        return "", False, True
    texts, media, unknown = [], False, False
    for block in content:
        if not isinstance(block, dict):
            unknown = True
            continue
        kind = block.get("type")
        if kind in ("input_text", "text") and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind in ("input_image", "image", "input_audio", "audio", "document", "input_file"):
            media = True
        elif kind != "tool_result":
            unknown = True
    return "\n".join(texts), media, unknown


def scan(path, source, uuid):
    """Stop at the first genuine candidate; fail closed if the prefix is ambiguous."""
    if source == "claude" and (path.parent.name == "subagents" or path.stem != uuid):
        return result("not_identifiable")
    identity = source == "claude"
    used = 0
    with path.open("rb") as stream:
        for _ in range(MAX_LINES):
            raw = stream.readline(MAX_FILE_BYTES - used + 1)
            used += len(raw)
            if used > MAX_FILE_BYTES:
                return result("limit_exceeded")
            if not raw:
                return result("not_identifiable")
            if not raw.endswith(b"\n"):
                return result("not_identifiable")
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                return result("not_identifiable")
            if not isinstance(event, dict):
                return result("not_identifiable")
            kind = event.get("type")
            text, media, unknown = "", False, False
            if source == "codex":
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    return result("not_identifiable")
                if kind == "session_meta":
                    if payload.get("id") != uuid or payload.get("parent_thread_id"):
                        return result("not_identifiable")
                    identity = True
                    continue
                if kind == "compacted":
                    return result("not_identifiable")
                if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                    text, media, unknown = content_text(payload.get("content"))
                elif kind == "event_msg" and payload.get("type") == "user_message":
                    text = payload.get("message") or ""
                    if not isinstance(text, str):
                        return result("not_identifiable")
                    media = any(payload.get(k) for k in ("images", "local_images", "audio", "local_audio"))
                elif (kind == "response_item" and payload.get("role") == "assistant") or (kind == "event_msg" and payload.get("type") == "token_count"):
                    return result("not_identifiable")  # Usage before a prompt: missing beginning.
                else:
                    continue
            else:
                if event.get("sessionId") not in (None, uuid):
                    return result("not_identifiable")
                if kind == "summary" or (kind == "system" and event.get("subtype") == "compact_boundary"):
                    return result("not_identifiable")
                if kind == "assistant":
                    return result("not_identifiable")
                if kind != "user" or event.get("isMeta") or event.get("isSidechain"):
                    continue
                if event.get("toolUseResult") is not None or event.get("sourceToolAssistantUUID"):
                    return result("not_identifiable")
                message = event.get("message") or {}
                text, media, unknown = content_text(message.get("content"))
                if isinstance(message.get("content"), list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in message["content"]):
                    return result("not_identifiable")
            if not identity or unknown:
                return result("not_identifiable")
            try:
                cleaned = user_text(text)
            except ValueError:
                return result("not_identifiable")
            if cleaned:
                if len(cleaned.encode("utf-8")) > MAX_TEXT_BYTES:
                    return result("limit_exceeded")
                return result("available", cleaned)
            if media:
                return result("no_text")
        return result("limit_exceeded")


class FirstPromptCache:
    def __init__(self, max_entries=512, max_bytes=4 * 1024 * 1024):
        self.entries = OrderedDict()
        self.lock = threading.RLock()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.bytes = 0

    @staticmethod
    def signature(path):
        s = path.stat()
        return s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns

    def read(self, session_id, paths):
        source, _, uuid = session_id.partition(":")
        if source not in ("codex", "claude"):
            return result("not_identifiable")
        paths = sorted(set(Path(p) for p in paths))
        if not paths:
            return result("unavailable")
        if len(paths) > MAX_FILES:
            return result("limit_exceeded")
        try:
            signatures = tuple((str(p), self.signature(p)) for p in paths)
        except OSError:
            return result("unavailable")
        with self.lock:
            cached = self.entries.get(session_id)
            if cached and cached[0] == signatures and time.monotonic() < cached[1]:
                self.entries.move_to_end(session_id)
                return dict(cached[2])
            try:
                values = [scan(p, source, uuid) for p in paths]
                if signatures != tuple((str(p), self.signature(p)) for p in paths):
                    return result("unavailable")
            except (OSError, ValueError, TypeError, AttributeError):
                return result("unavailable")
            # Copies must agree. Never choose a later/resumed file arbitrarily.
            value = values[0] if all(v == values[0] for v in values) else result("not_identifiable")
            size = len((value["text"] or "").encode("utf-8")) + 256
            if session_id in self.entries:
                self.bytes -= self.entries.pop(session_id)[3]
            ttl = 300 if value["status"] in ("available", "no_text") else 5
            self.entries[session_id] = signatures, time.monotonic() + ttl, value, size
            self.bytes += size
            while self.entries and (len(self.entries) > self.max_entries or self.bytes > self.max_bytes):
                self.bytes -= self.entries.popitem(last=False)[1][3]
            return dict(value)


cache = FirstPromptCache()


def previews(conn, session_ids, reader=None):
    reader = reader if reader is not None else cache
    items = {}
    for sid in dict.fromkeys(session_ids):
        session = conn.execute("SELECT tool, session_kind FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None or session["session_kind"] in ("guardian", "subagent"):
            items[sid] = result("not_identifiable")
            continue
        paths = [r[0] for r in conn.execute("SELECT DISTINCT source_file FROM messages WHERE session_id=? AND agent_id IS NULL AND agent_type IS NULL", (sid,))]
        items[sid] = reader.read(sid, paths)
    return {"previews": items}
