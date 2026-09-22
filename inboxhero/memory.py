"""Layered memory for the InboxHero agent.

Four memory tiers, each with an explicit read/write path so the agent
can inspect and change what it remembers:

    working    : per-turn scratchpad (in-process dict, cleared per run)
    episodic   : append-only trace of every tool call + decision (trace.jsonl)
    semantic   : durable facts about the world — people, threads, commitments
                 (state/facts.json)
    procedural : learned rules the user asked us to keep (state/preferences.json)

Everything on disk is JSON so a human can diff it. The memory_* tools
below are the surface the agent calls; they are registered with the MCP
server the same way as the inbox tools.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcp import ToolRegistry, tool


# ---------------------------------------------------------------------------
# On-disk stores
# ---------------------------------------------------------------------------


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False))


@dataclass
class MemoryStore:
    """Container for all four memory tiers."""

    state_dir: Path
    prefs_path: Path = field(init=False)
    facts_path: Path = field(init=False)
    trace_path: Path = field(init=False)
    working: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.prefs_path = self.state_dir / "preferences.json"
        self.facts_path = self.state_dir / "facts.json"
        self.trace_path = self.state_dir / "trace.jsonl"

    # ---- episodic --------------------------------------------------------
    def log(self, event: dict) -> None:
        """Append an event to the durable trace."""
        event = {"ts": time.time(), **event}
        with self.trace_path.open("a") as fh:
            fh.write(json.dumps(event) + "\n")

    def read_trace(self) -> list[dict]:
        if not self.trace_path.exists():
            return []
        return [json.loads(l) for l in self.trace_path.read_text().splitlines() if l.strip()]

    def reset_trace(self) -> None:
        if self.trace_path.exists():
            self.trace_path.unlink()

    # ---- procedural ------------------------------------------------------
    def load_preferences(self) -> list[dict]:
        return _read_json(self.prefs_path, [])

    def save_preference(self, key: str, value: str, source_msg: str | None = None) -> dict:
        prefs = self.load_preferences()
        # Update in place if key already exists.
        for p in prefs:
            if p["key"] == key:
                p["value"] = value
                p["source_msg"] = source_msg
                _write_json(self.prefs_path, prefs)
                return p
        entry = {"key": key, "value": value, "source_msg": source_msg}
        prefs.append(entry)
        _write_json(self.prefs_path, prefs)
        return entry

    def forget_preference(self, key: str) -> bool:
        prefs = self.load_preferences()
        remaining = [p for p in prefs if p["key"] != key]
        if len(remaining) == len(prefs):
            return False
        _write_json(self.prefs_path, remaining)
        return True

    # ---- semantic --------------------------------------------------------
    def load_facts(self) -> dict:
        return _read_json(self.facts_path, {})

    def remember_fact(self, topic: str, key: str, value: Any, source_msg: str | None = None) -> dict:
        facts = self.load_facts()
        facts.setdefault(topic, {})
        facts[topic][key] = {"value": value, "source_msg": source_msg}
        _write_json(self.facts_path, facts)
        return facts[topic][key]

    def recall_facts(self, topic: str | None = None) -> dict:
        facts = self.load_facts()
        if topic is None:
            return facts
        return facts.get(topic, {})

    # ---- working ---------------------------------------------------------
    def scratch(self, key: str, value: Any | None = None) -> Any:
        if value is None:
            return self.working.get(key)
        self.working[key] = value
        return value


# ---------------------------------------------------------------------------
# MCP tools that wrap the store
# ---------------------------------------------------------------------------


def register_memory_tools(reg: ToolRegistry, store: MemoryStore) -> None:
    """Attach the memory tool surface to an MCP registry."""

    @tool(
        reg,
        name="memory_save_preference",
        description="Persist a standing preference the user asked the agent to remember.",
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "source_msg": {"type": "string"},
            },
            "required": ["key", "value"],
        },
        category="memory",
    )
    def _save_pref(key: str, value: str, source_msg: str | None = None) -> dict:
        entry = store.save_preference(key, value, source_msg)
        store.log({"kind": "memory_write", "tier": "procedural", "key": key})
        return entry

    @tool(
        reg,
        name="memory_list_preferences",
        description="List every standing preference the agent has been asked to remember.",
        input_schema={"type": "object", "properties": {}},
        category="memory",
    )
    def _list_prefs() -> list[dict]:
        return store.load_preferences()

    @tool(
        reg,
        name="memory_forget_preference",
        description="Delete a previously stored preference by key.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
        category="memory",
    )
    def _forget(key: str) -> dict:
        ok = store.forget_preference(key)
        store.log({"kind": "memory_delete", "tier": "procedural", "key": key, "ok": ok})
        return {"deleted": ok}

    @tool(
        reg,
        name="memory_remember_fact",
        description="Store a fact about a person, thread or commitment for later runs.",
        input_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "key": {"type": "string"},
                "value": {},
                "source_msg": {"type": "string"},
            },
            "required": ["topic", "key", "value"],
        },
        category="memory",
    )
    def _remember(topic: str, key: str, value: Any, source_msg: str | None = None) -> dict:
        entry = store.remember_fact(topic, key, value, source_msg)
        store.log({"kind": "memory_write", "tier": "semantic", "topic": topic, "key": key})
        return entry

    @tool(
        reg,
        name="memory_recall",
        description="Recall stored facts, optionally scoped to a topic.",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
        },
        category="memory",
    )
    def _recall(topic: str | None = None) -> dict:
        return store.recall_facts(topic)

    @tool(
        reg,
        name="memory_scratch",
        description="Read or write the in-process working-memory scratchpad.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {}},
            "required": ["key"],
        },
        category="memory",
    )
    def _scratch(key: str, value: Any | None = None) -> Any:
        return store.scratch(key, value)
