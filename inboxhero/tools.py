"""Inbox tools exposed over MCP.

The tools split cleanly into two groups:

* Read tools (list_messages, get_message, get_thread, search_inbox, today) —
  cheap, side-effect-free, safe for the agent to call as much as it wants.
* Write tools — grouped by reversibility:
    reversible  : draft_reply, archive_message, defer_message, delegate_message,
                  escalate_message, label_message, flag_message
    irreversible: send_message, delete_message  (gated in the agent loop)

Every mutation is recorded to the episodic trace and to a snapshot on disk
(state/actions.json + state/outbox/), so a marker script can reconstruct
what the agent did without re-running it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .mcp import ToolRegistry, tool
from .memory import MemoryStore


# ---------------------------------------------------------------------------
# In-memory inbox + on-disk action log
# ---------------------------------------------------------------------------


class Inbox:
    def __init__(self, path: Path, state_dir: Path) -> None:
        self.path = path
        self.state_dir = state_dir
        self.messages: list[dict] = json.loads(path.read_text())
        self._by_id: dict[str, dict] = {m["id"]: m for m in self.messages}
        self.actions_path = state_dir / "actions.json"
        self.outbox_dir = state_dir / "outbox"
        self.actions: dict[str, dict] = json.loads(self.actions_path.read_text()) if self.actions_path.exists() else {}

    # ---- persistence ------------------------------------------------
    def save_actions(self) -> None:
        self.actions_path.parent.mkdir(parents=True, exist_ok=True)
        self.actions_path.write_text(json.dumps(self.actions, indent=2))

    def write_outbox(self, msg_id: str, payload: dict) -> Path:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        p = self.outbox_dir / f"{msg_id}.json"
        p.write_text(json.dumps(payload, indent=2))
        return p

    # ---- lookups ----------------------------------------------------
    def get(self, msg_id: str) -> dict:
        if msg_id not in self._by_id:
            raise KeyError(f"unknown message id: {msg_id}")
        return self._by_id[msg_id]

    def thread(self, thread_id: str) -> list[dict]:
        return sorted(
            (m for m in self.messages if m["thread_id"] == thread_id),
            key=lambda m: m["timestamp"],
        )

    def all_thread_ids(self) -> list[str]:
        return sorted({m["thread_id"] for m in self.messages})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_inbox_tools(reg: ToolRegistry, inbox: Inbox, store: MemoryStore, today: str) -> None:
    """Attach the inbox tool surface to an MCP registry."""

    def _log(kind: str, **rest: Any) -> None:
        store.log({"kind": kind, **rest})

    # ---- read tools ------------------------------------------------------
    @tool(
        reg,
        name="today",
        description="Return the mailbox's notion of 'today' (ISO date).",
        input_schema={"type": "object", "properties": {}},
        category="read",
    )
    def _today() -> str:
        return today

    @tool(
        reg,
        name="list_messages",
        description="List message ids with light metadata. Supports filter by unread or since date.",
        input_schema={
            "type": "object",
            "properties": {
                "unread_only": {"type": "boolean"},
                "since": {"type": "string", "description": "ISO date or datetime"},
            },
        },
        category="read",
    )
    def _list(unread_only: bool = False, since: str | None = None) -> list[dict]:
        out = []
        for m in inbox.messages:
            if unread_only and not m["unread"]:
                continue
            if since and m["timestamp"] < since:
                continue
            out.append({"id": m["id"], "thread": m["thread_id"], "from": m["from"], "subject": m["subject"], "ts": m["timestamp"]})
        _log("read", tool="list_messages", n=len(out))
        return out

    @tool(
        reg,
        name="get_message",
        description="Return one message in full by id.",
        input_schema={"type": "object", "properties": {"msg_id": {"type": "string"}}, "required": ["msg_id"]},
        category="read",
    )
    def _get(msg_id: str) -> dict:
        m = inbox.get(msg_id)
        _log("read", tool="get_message", msg_id=msg_id)
        return m

    @tool(
        reg,
        name="get_thread",
        description="Return every message in a thread, oldest first.",
        input_schema={"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]},
        category="read",
    )
    def _get_thread(thread_id: str) -> list[dict]:
        msgs = inbox.thread(thread_id)
        _log("read", tool="get_thread", thread_id=thread_id, n=len(msgs))
        return msgs

    @tool(
        reg,
        name="search_inbox",
        description="Case-insensitive substring search over subject + body. Returns matching ids.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        category="read",
    )
    def _search(query: str) -> list[str]:
        q = query.lower()
        hits = [m["id"] for m in inbox.messages if q in (m["subject"] + " " + m["body"]).lower()]
        _log("read", tool="search_inbox", query=query, n=len(hits))
        return hits

    # ---- reversible writes ----------------------------------------------
    def _record(msg_id: str, disposition: str, reason: str, **extra: Any) -> dict:
        prev = inbox.actions.get(msg_id, {})
        # Preserve labels + any prior extras so the disposition write doesn't wipe them.
        entry = {**prev, "disposition": disposition, "reason": reason, **extra}
        inbox.actions[msg_id] = entry
        inbox.save_actions()
        _log("decision", msg_id=msg_id, disposition=disposition, reason=reason)
        return entry

    @tool(
        reg,
        name="draft_reply",
        description="Draft a reply (does NOT send). Optional citations list of message ids the draft leaned on.",
        input_schema={
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
                "cited": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["msg_id", "body"],
        },
        category="write_reversible",
    )
    def _draft(msg_id: str, body: str, cc: list[str] | None = None, cited: list[str] | None = None) -> dict:
        m = inbox.get(msg_id)
        payload = {
            "kind": "draft",
            "in_reply_to": msg_id,
            "to": m["from"],
            "cc": cc or [],
            "subject": "Re: " + re.sub(r"^Re:\s*", "", m["subject"]),
            "body": body,
            "cited": cited or [],
        }
        p = inbox.write_outbox(msg_id, payload)
        _log("draft", msg_id=msg_id, cited=cited or [], cc=cc or [], path=str(p))
        _record(msg_id, "reply", "drafted for user approval", draft_path=str(p))
        return payload

    @tool(
        reg,
        name="archive_message",
        description="Archive a message. Reversible.",
        input_schema={
            "type": "object",
            "properties": {"msg_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["msg_id", "reason"],
        },
        category="write_reversible",
    )
    def _archive(msg_id: str, reason: str) -> dict:
        return _record(msg_id, "archive", reason)

    @tool(
        reg,
        name="defer_message",
        description="Defer a message to a specific date. Reversible.",
        input_schema={
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "reason": {"type": "string"},
                "until": {"type": "string"},
            },
            "required": ["msg_id", "reason", "until"],
        },
        category="write_reversible",
    )
    def _defer(msg_id: str, reason: str, until: str) -> dict:
        return _record(msg_id, "defer", reason, until=until)

    @tool(
        reg,
        name="delegate_message",
        description="Delegate a message to another person. Reversible.",
        input_schema={
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "reason": {"type": "string"},
                "to": {"type": "string"},
            },
            "required": ["msg_id", "reason", "to"],
        },
        category="write_reversible",
    )
    def _delegate(msg_id: str, reason: str, to: str) -> dict:
        return _record(msg_id, "delegate", reason, to=to)

    @tool(
        reg,
        name="escalate_message",
        description="Flag a message as needing the user personally.",
        input_schema={
            "type": "object",
            "properties": {"msg_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["msg_id", "reason"],
        },
        category="write_reversible",
    )
    def _escalate(msg_id: str, reason: str) -> dict:
        return _record(msg_id, "escalate", reason)

    @tool(
        reg,
        name="label_message",
        description="Attach one or more labels to a message. Reversible.",
        input_schema={
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["msg_id", "labels"],
        },
        category="write_reversible",
    )
    def _label(msg_id: str, labels: list[str]) -> dict:
        entry = inbox.actions.get(msg_id, {})
        entry.setdefault("labels", [])
        for l in labels:
            if l not in entry["labels"]:
                entry["labels"].append(l)
        inbox.actions[msg_id] = entry
        inbox.save_actions()
        _log("label", msg_id=msg_id, labels=labels)
        return entry

    @tool(
        reg,
        name="flag_message",
        description="Mark a message as suspicious (phishing / injection / social-engineering) with a reason.",
        input_schema={
            "type": "object",
            "properties": {
                "msg_id": {"type": "string"},
                "kind": {"type": "string"},
                "reason": {"type": "string"},
                "attempted": {"type": "string"},
                "did_instead": {"type": "string"},
            },
            "required": ["msg_id", "kind", "reason"],
        },
        category="write_reversible",
    )
    def _flag(msg_id: str, kind: str, reason: str, attempted: str | None = None, did_instead: str | None = None) -> dict:
        entry = _record(
            msg_id,
            "flagged",
            reason,
            threat=kind,
            attempted=attempted,
            did_instead=did_instead or "flagged and left in place",
        )
        _log(
            "refusal",
            msg_id=msg_id,
            threat=kind,
            reason=reason,
            attempted=attempted,
            did_instead=did_instead or "flagged and left in place",
        )
        return entry

    # ---- irreversible: gated by the agent loop --------------------------
    @tool(
        reg,
        name="send_message",
        description="Actually send an email. IRREVERSIBLE — must be gated.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "in_reply_to": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
        irreversible=True,
        category="write_irreversible",
    )
    def _send(to: str, subject: str, body: str, cc: list[str] | None = None, in_reply_to: str | None = None) -> dict:
        payload = {"kind": "sent", "to": to, "cc": cc or [], "subject": subject, "body": body, "in_reply_to": in_reply_to, "sent_at": datetime.utcnow().isoformat()}
        p = inbox.write_outbox(f"sent-{in_reply_to or datetime.utcnow().timestamp()}", payload)
        _log("send", to=to, subject=subject, in_reply_to=in_reply_to, path=str(p))
        return payload

    @tool(
        reg,
        name="delete_message",
        description="Permanently delete a message. IRREVERSIBLE — must be gated.",
        input_schema={
            "type": "object",
            "properties": {"msg_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["msg_id", "reason"],
        },
        irreversible=True,
        category="write_irreversible",
    )
    def _delete(msg_id: str, reason: str) -> dict:
        _log("delete", msg_id=msg_id, reason=reason)
        return _record(msg_id, "delete", reason)
