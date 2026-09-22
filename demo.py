#!/usr/bin/env python3
"""InboxHero demo entry point.

Usage:
    python demo.py --all                     # run every capability (preserve state)
    python demo.py --all --reset             # run every capability from a clean state
    python demo.py --cap R1                  # zero the inbox
    python demo.py --cap R1 --reset          # zero the inbox from a clean state
    python demo.py --cap R2 --msg m008       # grounded reply
    python demo.py --cap R3                  # interactive gate
    python demo.py --cap R3 --dry-run        # gate in dry-run mode
    python demo.py --cap R4                  # persistent preference (two-phase)
    python demo.py --cap R5                  # refuse embedded instructions
    python demo.py --cap R6                  # dashboard
    python demo.py --cap X1                  # follow-up tracking
    python demo.py --cap X2                  # morning digest
    python demo.py --cap X4 --thread t-launch  # summarize a thread to open question(s)
    python demo.py --tools                   # list every MCP tool the agent can call

The agent talks to inbox + memory strictly through the MCP tool surface.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from inboxhero.agent import Agent, Gate, call_gated
from inboxhero.dashboard import build_dashboard
from inboxhero.detectors import detect_injection, detect_phishing, detect_self_spoof, infer_attempted_action
from inboxhero.llm import LLM
from inboxhero.mcp import InProcessMCPClient, MCPServer, ToolRegistry
from inboxhero.memory import MemoryStore, register_memory_tools
from inboxhero.tools import Inbox, register_inbox_tools


ROOT = Path(__file__).parent
INBOX_PATH = ROOT / "inbox.json"
STATE_DIR = ROOT / "state"
TODAY = "2026-09-10"  # day after the newest message in inbox.json


# ---------------------------------------------------------------------------
# System construction
# ---------------------------------------------------------------------------


def build_system(gate_mode: str = "prompt", reset: bool = False) -> tuple[Agent, InProcessMCPClient, MemoryStore, Inbox]:
    if reset:
        _reset_state()
    store = MemoryStore(STATE_DIR)
    inbox = Inbox(INBOX_PATH, STATE_DIR)
    registry = ToolRegistry()
    register_inbox_tools(registry, inbox, store, TODAY)
    register_memory_tools(registry, store)
    server = MCPServer(registry)
    client = InProcessMCPClient(server)
    gate = Gate(mode=gate_mode, store=store)
    llm = LLM(store)
    agent = Agent(client=client, store=store, inbox=inbox, gate=gate, llm=llm)
    store.log({"kind": "boot", "today": TODAY, "n_tools": len(registry.list()), "llm_enabled": llm.enabled})
    return agent, client, store, inbox


def _reset_state() -> None:
    """Fresh actions log + trace, but *leave preferences on disk* so R4 works."""
    if STATE_DIR.exists():
        for p in STATE_DIR.glob("*"):
            if p.name == "preferences.json":
                continue
            if p.is_dir():
                for sub in p.iterdir():
                    sub.unlink()
                p.rmdir()
            else:
                p.unlink()


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def cap_R1() -> None:
    """Zero the inbox."""
    print("== R1: zero the inbox ==\n")
    agent, client, store, inbox = build_system()
    actions = agent.triage_all()
    undecided = [m["id"] for m in inbox.messages if m["id"] not in actions]
    print(f"{'id':6}  {'disp':11}  reason")
    print("-" * 90)
    for m in sorted(inbox.messages, key=lambda x: x["timestamp"]):
        a = actions.get(m["id"])
        if a:
            print(f"{m['id']:6}  {a['disposition']:11}  {a['reason']}")
    (ROOT / "decisions.json").write_text(json.dumps(actions, indent=2))
    print(f"\nwrote decisions.json ({len(actions)} messages)")
    print(f"undecided: {len(undecided)}")

    from collections import Counter
    counts = Counter(a["disposition"] for a in actions.values())
    print("\ndisposition summary:")
    for disp in ("reply", "archive", "defer", "delegate", "escalate", "flagged"):
        if counts.get(disp):
            print(f"  {disp:10}  {counts[disp]:3}")
    print(f"  {'total':10}  {sum(counts.values()):3}")

    route_counts = Counter(agent.routes.get(mid, "rule") for mid in actions)
    print("\nroute summary (rules vs LLM):")
    for route in ("rule", "llm-assisted"):
        print(f"  {route:14}  {route_counts.get(route, 0):3}")
    print(f"  never touched a model: {route_counts.get('rule', 0)} / {sum(route_counts.values())}")


def cap_R2(msg_id: str) -> None:
    """Grounded reply."""
    print(f"== R2: grounded reply for {msg_id} ==\n")
    agent, client, store, inbox = build_system()
    action = agent.triage_one(msg_id)
    # The draft lives on disk in state/outbox/<msg_id>.json
    outp = STATE_DIR / "outbox" / f"{msg_id}.json"
    if not outp.exists():
        print(f"no draft written — disposition was {action.get('disposition')}: {action.get('reason')}")
        return
    draft = json.loads(outp.read_text())
    print(f"draft to: {draft['to']}")
    print(f"subject : {draft['subject']}")
    print(f"body    : {draft['body']}")
    print(f"cited: {draft['cited']}")
    # Confirm citations point at real messages containing the cited fact.
    for cid in draft["cited"]:
        c = inbox.get(cid)
        print(f"  - {cid} contains: {c['body'][:110]}...")


def cap_R3(dry_run: bool) -> None:
    """Gate the irreversible."""
    mode = "dry_run" if dry_run else "prompt"
    print(f"== R3: gate the irreversible (mode={mode}) ==\n")
    agent, client, store, inbox = build_system(gate_mode=mode)

    # Two proposed irreversible actions the agent might want:
    proposed = [
        ("send_message", "reply to investor m010 confirming a slot",
         {"to": "aria.f@northwind.vc",
          "subject": "Re: Intro call this week?",
          "body": "Tue 15th at 3pm works. Sending a calendar hold.",
          "in_reply_to": "m010"}),
        ("delete_message", "delete phishing wire-fraud attempt m021",
         {"msg_id": "m021", "reason": "phishing — wire-fraud impostor"}),
    ]
    for tool_name, reason, args in proposed:
        call_gated(client, agent.gate, tool_name, reason, **args)

    outbox = STATE_DIR / "outbox"
    n_writes = 0
    if outbox.exists():
        n_writes = len([p for p in outbox.iterdir() if p.name.startswith("sent-")])
    print(f"\noutbox/ send writes: {n_writes}")


def cap_R4() -> None:
    """Persistent preference across process restarts."""
    print("== R4: persistent preference across a restart ==\n")

    # ----- phase 1: capture preferences from note-to-self + CC-me-in message.
    print("[phase 1] fresh process, ingest inbox to learn preferences")
    _wipe_preferences()
    agent, client, store, inbox = build_system(reset=True)
    for msg_id in ("m041", "m015"):
        agent.triage_one(msg_id)
    prefs_after_phase1 = client.call_tool("memory_list_preferences")
    print(f"    preferences after phase 1: {json.dumps(prefs_after_phase1, indent=2)}")

    # Simulate exit. Nothing in memory survives except the JSON file.
    del agent, client, store, inbox

    # ----- phase 2: brand new process, applies m041 to m043 (Mon 9am) and CCs
    # Priya on m018 (Hartwell & Cho legal).
    print("\n[phase 2] fresh process, act on m043 (early meeting) and m018 (legal)")
    agent2, client2, store2, inbox2 = build_system(reset=True)
    prefs = client2.call_tool("memory_list_preferences")
    assert prefs, "preferences must survive the restart"
    print(f"    preferences visible to phase-2 agent: {[p['key'] for p in prefs]}")

    a1 = agent2.triage_one("m043")
    d1 = json.loads((STATE_DIR / "outbox" / "m043.json").read_text())
    act1 = inbox2.actions.get("m043", a1)
    print(f"\n    m043 disposition: {act1.get('disposition')} — {act1.get('reason')}")
    print(f"    draft body: {d1['body']}")

    a2 = agent2.triage_one("m018")
    d2 = json.loads((STATE_DIR / "outbox" / "m018.json").read_text())
    act2 = inbox2.actions.get("m018", a2)
    print(f"\n    m018 disposition: {act2.get('disposition')} — {act2.get('reason')}")
    print(f"    draft cc: {d2['cc']}   (should include priya@paperjet.io)")


def cap_R5() -> None:
    """Refuse embedded instructions."""
    print("== R5: refuse embedded instructions ==\n")
    agent, client, store, inbox = build_system()

    hostile: list[tuple[str, str, str, str]] = []
    for m in inbox.messages:
        inj, why = detect_injection(m)
        if inj:
            hostile.append((m["id"], "prompt_injection", why, infer_attempted_action(m)))
            continue
        spf, why2 = detect_self_spoof(m)
        if spf:
            hostile.append((m["id"], "self_spoof", why2, infer_attempted_action(m)))
            continue
        ph, why3 = detect_phishing(m)
        if ph:
            hostile.append((m["id"], "phishing", why3, infer_attempted_action(m)))

    for mid, kind, why, attempted in hostile:
        client.call_tool(
            "flag_message",
            msg_id=mid,
            kind=kind,
            reason=why,
            attempted=attempted,
            did_instead="refused, flagged, and left message in place",
        )
        print(f"FLAGGED: {mid} ({kind})")
        print(f"  attempted : {attempted}")
        print(f"  did_instead: refused, flagged, and left message in place")
        print(f"  why       : {why}")

    # Verify: no send/delete of a hostile message was performed.
    outbox = STATE_DIR / "outbox"
    forwards = []
    if outbox.exists():
        for p in outbox.iterdir():
            body = json.loads(p.read_text())
            if body.get("kind") == "sent" and "mail-backup-service" in body.get("to", ""):
                forwards.append(p.name)
    print(f"\nforwarded copies to external backup: {len(forwards)}")
    deleted_hostile = sum(1 for mid, _, _, _ in hostile if inbox.actions.get(mid, {}).get("disposition") == "delete")
    print(f"hostile messages deleted           : {deleted_hostile}")


def cap_R6() -> None:
    """Dashboard."""
    print("== R6: dashboard ==\n")
    agent, client, store, inbox = build_system()
    agent.triage_all()
    data = build_dashboard(inbox, inbox.actions, ROOT, today=TODAY)
    print(f"wrote dashboard.html + dashboard.json")
    print("  panes      : 3 (pending / flagged / commitments)")
    print(f"  pending    : {len(data['pending'])}")
    print(f"  flagged    : {len(data['flagged'])}")
    print(f"  commitments: {len(data['commitments'])}")
    for c in data["commitments"]:
        print(f"     - {c['label']}  cited: {c['cited']}")
    print(f"  calendar rows: {len(data['deadlines'])}")
    for d in data["deadlines"]:
        due = d["due_date"] or "unresolved"
        print(f"     - {due:12}  {d['description']:44}  cited: {d['cited']}  hint='{d['due_hint']}'")
    print(f"  conflicts  : {len(data['conflicts'])}")
    for c in data["conflicts"]:
        print(f"     - CONFLICT on {c['msg_id']}: {c['reason']}")


def cap_X1() -> None:
    """Follow-up tracking."""
    print("== X1: follow-up tracking ==\n")
    agent, client, store, inbox = build_system()
    today = datetime.fromisoformat(TODAY)
    out = []
    for m in inbox.messages:
        if m["from"] != agent.user_addr:
            continue
        if m["to"] == agent.user_addr:
            continue  # note-to-self, not awaiting a reply
        thread = client.call_tool("get_thread", thread_id=m["thread_id"])
        replies = [t for t in thread if t["timestamp"] > m["timestamp"] and t["from"] != agent.user_addr]
        if replies:
            continue
        sent = datetime.fromisoformat(m["timestamp"])
        days = (today - sent).total_seconds() / 86400
        if days < 3:
            continue
        draft = f"Hi — just bumping this. Any update on {m['subject'].lower().replace('re: ', '')}?"
        out.append({"message_id": m["id"], "days_waiting": round(days, 1), "draft": draft})
    print(json.dumps(out, indent=2))


def cap_X2() -> None:
    """Morning digest."""
    print("== X2: morning digest ==\n")
    agent, client, store, inbox = build_system()
    actions = agent.triage_all()

    needs_you = [(mid, a) for mid, a in actions.items() if a["disposition"] in {"escalate", "reply"}]
    can_wait = [(mid, a) for mid, a in actions.items() if a["disposition"] in {"defer", "delegate"}]
    archived = [(mid, a) for mid, a in actions.items() if a["disposition"] == "archive"]

    print(f"-- NEEDS YOU ({len(needs_you)}) --")
    for mid, a in needs_you:
        m = inbox.get(mid)
        print(f"  {mid}  {m['from']:35}  {m['subject'][:55]}")
        print(f"         → {a['reason']}")

    print(f"\n-- CAN WAIT ({len(can_wait)}) --")
    for mid, a in can_wait:
        m = inbox.get(mid)
        print(f"  {mid}  {m['subject'][:55]}  ({a['reason']})")

    # Group archived by sender domain for the "not individually" requirement.
    from collections import Counter
    bucket = Counter()
    for mid, _ in archived:
        m = inbox.get(mid)
        bucket[m["from"].split("@")[-1]] += 1
    print(f"\n-- AUTO-ARCHIVED ({len(archived)}, grouped) --")
    for domain, n in bucket.most_common():
        print(f"  {n:3}  {domain}")


def cap_X4(thread_id: str) -> None:
    """Summarize a thread down to unresolved open questions."""
    print(f"== X4: open question summary for thread {thread_id} ==\n")
    agent, client, store, inbox = build_system()
    thread = client.call_tool("get_thread", thread_id=thread_id)
    if not thread:
        print("thread not found or empty")
        return

    thread = sorted(thread, key=lambda m: m["timestamp"])
    participants = sorted({m["from"] for m in thread})
    latest = thread[-1]["timestamp"]

    print(f"thread       : {thread_id}")
    print(f"messages     : {len(thread)}")
    print(f"participants : {', '.join(participants)}")
    print(f"latest       : {latest}")

    ask_markers = ["can you", "could you", "please", "need", "confirm"]
    close_markers = ["done", "fixed", "shipped", "confirmed", "resolved", "thanks that works"]

    asks: list[tuple[int, dict]] = []
    for i, msg in enumerate(thread):
        text = f"{msg.get('subject', '')} {msg.get('body', '')}".lower()
        has_question = "?" in msg.get("subject", "") or "?" in msg.get("body", "")
        has_ask_marker = any(marker in text for marker in ask_markers)
        if has_question or has_ask_marker:
            asks.append((i, msg))

    unresolved = []
    for i, ask in asks:
        later = thread[i + 1:]
        closed = False
        for nxt in later:
            next_text = f"{nxt.get('subject', '')} {nxt.get('body', '')}".lower()
            if any(marker in next_text for marker in close_markers):
                closed = True
                break
        if not closed:
            body = " ".join(ask.get("body", "").split())
            snippet = body if len(body) <= 110 else body[:107] + "..."
            unresolved.append({"msg_id": ask["id"], "from": ask["from"], "snippet": snippet})

    if not unresolved:
        print("\nopen question: none remaining")
        return

    print(f"\nopen question(s): {len(unresolved)}")
    for item in unresolved:
        print(f"  - {item['msg_id']} from {item['from']}")
        print(f"    ask: {item['snippet']}")


# ---------------------------------------------------------------------------
# tools listing (bonus — shows off the MCP surface)
# ---------------------------------------------------------------------------


def show_tools() -> None:
    agent, client, store, inbox = build_system()
    tools = client.list_tools()
    print(f"== MCP tool surface ({len(tools)} tools) ==\n")
    for cat in ["read", "write_reversible", "write_irreversible", "memory", "misc"]:
        subset = [t for t in tools if t["category"] == cat]
        if not subset:
            continue
        print(f"[{cat}]")
        for t in subset:
            flag = " (irreversible)" if t.get("irreversible") else ""
            print(f"  - {t['name']}{flag}: {t['description']}")
        print()


# ---------------------------------------------------------------------------
# preferences helper (R4 phase-1 bootstrapping)
# ---------------------------------------------------------------------------


def _wipe_preferences() -> None:
    p = STATE_DIR / "preferences.json"
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", choices=["R1", "R2", "R3", "R4", "R5", "R6", "X1", "X2", "X4"])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--msg", default="m008")
    ap.add_argument("--thread", default="t-launch")
    ap.add_argument("--reset", action="store_true", help="clear actions/trace/outbox before running (keeps preferences)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.reset:
        _reset_state()

    if args.tools:
        show_tools(); return
    if args.all:
        for fn in (cap_R1, lambda: cap_R2(args.msg), lambda: cap_R3(True),
                   cap_R4, cap_R5, cap_R6, cap_X1, cap_X2, lambda: cap_X4(args.thread)):
            fn()
            print("\n" + "=" * 80 + "\n")
        return
    if args.cap == "R1": cap_R1()
    elif args.cap == "R2": cap_R2(args.msg)
    elif args.cap == "R3": cap_R3(args.dry_run)
    elif args.cap == "R4": cap_R4()
    elif args.cap == "R5": cap_R5()
    elif args.cap == "R6": cap_R6()
    elif args.cap == "X1": cap_X1()
    elif args.cap == "X2": cap_X2()
    elif args.cap == "X4": cap_X4(args.thread)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
