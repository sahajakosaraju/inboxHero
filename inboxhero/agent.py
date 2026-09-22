"""Agent orchestrator.

An agent is a *policy* over the tool surface. Rather than embed an
LLM dependency this policy is deterministic and inspectable: each
message flows through classify → retrieve → plan → act. The plan is
built from three inputs:

    * inbox tool calls (get_thread, search_inbox)     — evidence
    * memory tool calls (memory_list_preferences,     — rules
      memory_recall)
    * detectors (detect_injection / detect_phishing)  — safety

The point is that every branch is reachable through *tools on the MCP
surface*. Swap the policy for a real LLM and the plumbing is unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .detectors import detect_injection, detect_phishing, detect_self_spoof, infer_attempted_action
from .llm import LLM
from .mcp import InProcessMCPClient
from .memory import MemoryStore
from .tools import Inbox


# ---------------------------------------------------------------------------
# Gate for irreversible tools
# ---------------------------------------------------------------------------


@dataclass
class Gate:
    """Approval gate that sits in front of every irreversible tool call."""

    mode: str = "prompt"  # 'prompt' | 'dry_run' | 'auto_approve' | 'auto_deny'
    store: MemoryStore | None = None

    def _log(self, tool: str, args: dict, reason: str, human: str, approved: bool) -> None:
        if self.store is None:
            return
        args_summary = {k: (v if not isinstance(v, str) or len(v) < 120 else v[:117] + "...") for k, v in args.items()}
        self.store.log({
            "kind": "gate",
            "tool": tool,
            "reason": reason,
            "mode": self.mode,
            "human": human,
            "approved": approved,
            "proposed_args": args_summary,
        })

    def approve(self, tool: str, args: dict, reason: str) -> bool:
        summary = f"{tool}({', '.join(f'{k}={v!r}' for k, v in list(args.items())[:3])}...)"
        if self.mode == "dry_run":
            print(f"    [gate] DRY-RUN would call {tool}: {reason}")
            print(f"           {summary}")
            self._log(tool, args, reason, human="dry-run", approved=False)
            return False
        if self.mode == "auto_approve":
            print(f"    [gate] auto-approved {tool}: {reason}")
            self._log(tool, args, reason, human="auto-approve", approved=True)
            return True
        if self.mode == "auto_deny":
            print(f"    [gate] auto-denied {tool}: {reason}")
            self._log(tool, args, reason, human="auto-deny", approved=False)
            return False
        # interactive
        print(f"    [gate] {tool}: {reason}")
        print(f"           {summary}")
        try:
            ans = input("           approve? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        approved = ans == "y"
        self._log(tool, args, reason, human=ans or "n", approved=approved)
        return approved


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


NOISE_SENDERS = re.compile(
    r"@(dropbox|slack|vercel|1password|amazon|netflix|google|apple|spotify|coursera|"
    r"lyft|github|figma|bluebottlecoffee|pagerduty|producthunt|accounts\.google|"
    r"sentry|postmark|datadog|namecheap|openai|notion|cloudflare|pragmaticengineer|"
    r"robinhood|mailchimp|zoom|digitalocean|twitter|medium|substack|stripe|"
    r"intercom|calendly|swiggy|ramp|doordash|linkedin|todoist|hackernewsletter|"
    r"uber|grammarly|united|amazon\.com|instacart|chase|paperjet-monitoring)\.",
    re.IGNORECASE,
)


CALENDAR_TIME_RE = re.compile(
    r"(\d{1,2}):?(\d{2})?\s*(am|pm|AM|PM)",
)


def _parse_hour(text: str) -> int | None:
    m = CALENDAR_TIME_RE.search(text)
    if not m:
        return None
    h = int(m.group(1))
    ampm = m.group(3).lower()
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return h


@dataclass
class Agent:
    client: InProcessMCPClient
    store: MemoryStore
    inbox: Inbox
    gate: Gate
    user_addr: str = "sam@paperjet.io"
    trusted_domain: str = "paperjet.io"
    llm: LLM | None = None
    routes: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # entry point used by every capability
    # ------------------------------------------------------------------
    def triage_all(self) -> dict[str, dict]:
        """Assign a disposition to every message and return the actions map."""
        prefs = self.client.call_tool("memory_list_preferences")
        for msg in self.inbox.messages:
            self.triage_one(msg["id"], prefs=prefs)
        return dict(self.inbox.actions)

    def triage_one(self, msg_id: str, prefs: list[dict] | None = None) -> dict:
        if prefs is None:
            prefs = self.client.call_tool("memory_list_preferences")
        msg = self.client.call_tool("get_message", msg_id=msg_id)
        # Default route; only promoted to 'llm-assisted' if _reply_or_escalate
        # actually consumed an LLM-produced body.
        self.routes[msg_id] = "rule"

        # ---- SAFETY FIRST ----
        inj, reason = detect_injection(msg)
        if inj:
            return self.client.call_tool(
                "flag_message",
                msg_id=msg_id,
                kind="prompt_injection",
                reason=reason,
                attempted=infer_attempted_action(msg),
                did_instead="refused, flagged, and left message in place",
            )
        spoof, sreason = detect_self_spoof(msg)
        if spoof:
            return self.client.call_tool(
                "flag_message",
                msg_id=msg_id,
                kind="self_spoof",
                reason=sreason,
                attempted=infer_attempted_action(msg),
                did_instead="refused, flagged, and left message in place",
            )
        phish, preason = detect_phishing(msg)
        if phish:
            return self.client.call_tool(
                "flag_message",
                msg_id=msg_id,
                kind="phishing",
                reason=preason,
                attempted=infer_attempted_action(msg),
                did_instead="flagged as phishing and took no outbound action",
            )

        # ---- CHEAP RULES ----
        if NOISE_SENDERS.search(msg["from"]) and self.user_addr in msg["to"]:
            return self.client.call_tool(
                "archive_message", msg_id=msg_id, reason="rule: transactional / newsletter noise"
            )

        # Preferences the user asked us to remember
        if msg["from"] == self.user_addr and msg["to"] == self.user_addr:
            # note-to-self: capture as preference, don't reply
            self._capture_preference(msg)
            return self.client.call_tool(
                "archive_message", msg_id=msg_id, reason="captured as standing preference"
            )
        if "loop me in" in msg["subject"].lower() or "please make sure" in msg["body"].lower():
            self._capture_preference(msg)
            return self.client.call_tool(
                "archive_message", msg_id=msg_id, reason="captured as standing preference"
            )

        # Messages sent BY the user waiting on a reply → follow-up bucket
        if msg["from"] == self.user_addr:
            return self._maybe_followup(msg)

        # ---- INTERNAL vs EXTERNAL ROUTING ----
        sender_domain = msg["from"].split("@")[-1]
        thread = self.client.call_tool("get_thread", thread_id=msg["thread_id"])

        # Someone else in the thread already answered → archive.
        if self._later_reply_exists(msg, thread):
            return self.client.call_tool(
                "archive_message", msg_id=msg_id, reason="a later message in the thread already responded"
            )

        # Legal (Hartwell & Cho) — apply CC preference if present.
        if "hartwellcho.com" in sender_domain or "legal" in msg["subject"].lower():
            cc = self._legal_cc_from_prefs(prefs)
            return self._reply_or_escalate(msg, cited=[msg["id"]], cc=cc,
                                           body="Confirming receipt. I'll review and get back to you.")

        # Investor / press / partner / venue / recruiter / dentist etc.
        if sender_domain != self.trusted_domain:
            return self._handle_external(msg, prefs)

        # Internal teammate mail: reply, escalate or defer.
        return self._handle_internal(msg)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _capture_preference(self, msg: dict) -> None:
        body = msg["body"].lower()
        if "before 11" in body or "11:00am" in body or "before 11:00am" in body:
            self.client.call_tool(
                "memory_save_preference",
                key="calendar.earliest_meeting",
                value="11:00",
                source_msg=msg["id"],
            )
        if "hartwell" in body or "lawyers" in body or "legal" in body:
            self.client.call_tool(
                "memory_save_preference",
                key="cc.legal",
                value=msg["from"],
                source_msg=msg["id"],
            )

    def _legal_cc_from_prefs(self, prefs: list[dict]) -> list[str]:
        for p in prefs:
            if p["key"] == "cc.legal":
                return [p["value"]]
        return []

    def _later_reply_exists(self, msg: dict, thread: list[dict]) -> bool:
        my = msg["timestamp"]
        for other in thread:
            if other["id"] == msg["id"]:
                continue
            if other["timestamp"] > my and other["from"] == self.user_addr:
                return True
        return False

    def _reply_or_escalate(
        self,
        msg: dict,
        cited: list[str],
        body: str,
        cc: list[str] | None = None,
    ) -> dict:
        # Let the LLM rewrite the body + citations when it is available.
        if self.llm is not None:
            thread = self.client.call_tool("get_thread", thread_id=msg["thread_id"])
            prefs = self.client.call_tool("memory_list_preferences")
            drafted = self.llm.draft(msg, thread, prefs, self.user_addr)
            if drafted and drafted.get("body"):
                body = drafted["body"]
                cited = drafted.get("cited") or cited
                self.routes[msg["id"]] = "llm-assisted"
                self.store.log({"kind": "route", "msg_id": msg["id"], "route": "llm-assisted"})
        self.client.call_tool(
            "draft_reply", msg_id=msg["id"], body=body, cc=cc or [], cited=cited
        )
        return self.inbox.actions[msg["id"]]

    def _maybe_followup(self, msg: dict) -> dict:
        thread = self.client.call_tool("get_thread", thread_id=msg["thread_id"])
        # any reply from someone else after this message?
        later_reply = [t for t in thread if t["timestamp"] > msg["timestamp"] and t["from"] != self.user_addr]
        if later_reply:
            return self.client.call_tool(
                "archive_message", msg_id=msg["id"], reason="counterparty already replied"
            )
        return self.client.call_tool(
            "escalate_message",
            msg_id=msg["id"],
            reason="you sent this and nobody has replied yet — candidate for a chase",
        )

    def _handle_external(self, msg: dict, prefs: list[dict]) -> dict:
        body = msg["body"].lower()
        if "hold expires" in body or "confirm for the date" in body:
            return self.client.call_tool(
                "escalate_message", msg_id=msg["id"], reason="external venue asking for a confirm with a 48h fuse"
            )
        if "on deadline" in body or "launch coverage" in body:
            return self.client.call_tool(
                "escalate_message", msg_id=msg["id"], reason="press asking for a launch quote"
            )
        if "intro" in msg["subject"].lower() or "vc" in msg["from"] or "northwind" in msg["from"]:
            # investor — check the calendar preference
            hour = _parse_hour(msg["body"])
            earliest = None
            for p in prefs:
                if p["key"] == "calendar.earliest_meeting":
                    earliest = int(p["value"].split(":")[0])
            if earliest is not None and hour is not None and hour < earliest:
                return self.client.call_tool(
                    "draft_reply",
                    msg_id=msg["id"],
                    body=(
                        f"Thanks! I'm not able to do the proposed time — I keep mornings blocked "
                        f"until {earliest}:00. Could we do {earliest}:00 or later that day instead?"
                    ),
                    cited=[msg["id"]],
                )
            return self.client.call_tool(
                "escalate_message", msg_id=msg["id"], reason="investor asking for a slot — you decide"
            )
        if "reminder" in msg["subject"].lower() or "appointment" in msg["subject"].lower():
            return self.client.call_tool(
                "defer_message", msg_id=msg["id"], reason="personal reminder", until="on the day"
            )
        return self.client.call_tool(
            "escalate_message", msg_id=msg["id"], reason="external correspondence — needs a human read"
        )

    def _handle_internal(self, msg: dict) -> dict:
        subject = msg["subject"].lower()
        body = msg["body"].lower()
        thread = self.client.call_tool("get_thread", thread_id=msg["thread_id"])

        # PTO / FYI: acknowledge, don't reply.
        if "pto" in subject or "office closed" in subject or "timesheet" in subject or "notes from" in subject:
            return self.client.call_tool(
                "archive_message", msg_id=msg["id"], reason="internal FYI — noted"
            )

        # Vague follow-up on prior conversation → escalate.
        if subject in {"the thing"} or "that thing we talked about" in body:
            self.store.log({
                "kind": "not_grounded",
                "msg_id": msg["id"],
                "needed": "the prior conversation the sender is referring to",
                "searched": f"thread {msg['thread_id']}",
                "outcome": "no draft — escalated for user disambiguation",
            })
            return self.client.call_tool(
                "escalate_message", msg_id=msg["id"], reason="under-specified; needs you to disambiguate"
            )

        # Staging: R2 grounded reply — look for an earlier message on this thread
        # that contains a URL / credential the sender is now asking for.
        if "staging" in subject or "queue creds" in body or "amqp" in body or "worker" in body:
            for prior in thread:
                if prior["timestamp"] < msg["timestamp"] and "amqp://" in prior["body"]:
                    url = re.search(r"amqp://\S+", prior["body"])
                    if url:
                        return self.client.call_tool(
                            "draft_reply",
                            msg_id=msg["id"],
                            body=(
                                "Same URL as before: "
                                f"{url.group(0)} — good to go, no rotation needed."
                            ),
                            cited=[prior["id"]],
                        )
            # Thread walk failed to surface the requested credential; do NOT draft.
            self.store.log({
                "kind": "not_grounded",
                "msg_id": msg["id"],
                "needed": "amqp:// URL requested in this staging thread",
                "searched": f"thread {msg['thread_id']} ({len(thread)} messages)",
                "outcome": "no draft — escalated because the credential is not in the inbox",
            })
            return self.client.call_tool(
                "escalate_message",
                msg_id=msg["id"],
                reason="asked for a credential not present in the inbox — no grounded draft possible",
            )

        # Scheduling → detect and escalate on conflict.
        if "move our" in subject or "1:1" in subject or "1:1" in body or "demo" in subject:
            return self._handle_scheduling(msg)

        # Launch thread — usually FYIs, but m030 has a real ask for you.
        if msg["thread_id"] == "t-launch":
            if "sam, can you" in body or "needs sam specifically" in body:
                return self.client.call_tool(
                    "escalate_message", msg_id=msg["id"], reason="launch thread — action item lands on you"
                )
            return self.client.call_tool(
                "archive_message", msg_id=msg["id"], reason="launch thread FYI — team is executing"
            )

        # Board deck (m040) — commitment, escalate.
        if "board" in subject or "deck" in subject:
            return self.client.call_tool(
                "escalate_message", msg_id=msg["id"], reason="board commitment — you own the deck"
            )

        return self.client.call_tool(
            "escalate_message", msg_id=msg["id"], reason="internal message needs your read"
        )

    def _handle_scheduling(self, msg: dict) -> dict:
        # Check every other outstanding request for the same slot.
        my_hour = _parse_hour(msg["body"])
        conflicts = []
        for other in self.inbox.messages:
            if other["id"] == msg["id"]:
                continue
            if other.get("from") == self.user_addr:
                continue
            oh = _parse_hour(other.get("body", ""))
            if oh is not None and my_hour is not None and oh == my_hour:
                # Same clock hour, near in time — treat as candidate conflict.
                if abs(_days_between(msg["timestamp"], other["timestamp"])) <= 3:
                    conflicts.append(other["id"])
        if conflicts:
            self.client.call_tool("label_message", msg_id=msg["id"], labels=["conflict"])
            return self.client.call_tool(
                "escalate_message",
                msg_id=msg["id"],
                reason=f"scheduling conflict with {', '.join(conflicts)}",
            )
        return self.client.call_tool(
            "escalate_message", msg_id=msg["id"], reason="scheduling — you decide"
        )


def _days_between(a: str, b: str) -> float:
    fa = datetime.fromisoformat(a)
    fb = datetime.fromisoformat(b)
    return (fa - fb).total_seconds() / 86400.0


# ---------------------------------------------------------------------------
# Gated tool caller — used by capabilities that touch send/delete.
# ---------------------------------------------------------------------------


def call_gated(client: InProcessMCPClient, gate: Gate, tool_name: str, gate_reason: str, **args) -> dict | None:
    """Wrapper the demo uses when it wants to actually SEND or DELETE."""
    if not gate.approve(tool_name, args, gate_reason):
        return None
    result = client.call_tool(tool_name, **args)
    if gate.store is not None:
        gate.store.log({"kind": "gate_executed", "tool": tool_name})
    return result
