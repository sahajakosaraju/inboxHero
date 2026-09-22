# CAPABILITIES.md — InboxHero

**Student:** REPLACE_ME, REPLACE_STUDENT_ID
**Repository:** https://github.com/REPLACE_ME/inboxhero

Run everything through the entry point:

```
.venv/bin/python demo.py --cap R1 --reset   # one capability, clean state
.venv/bin/python demo.py --cap X4 --thread t-launch
.venv/bin/python demo.py --all --reset  # all of them in order, clean state
.venv/bin/python demo.py --tools        # inspect the MCP tool surface
```

---

## The system, in one paragraph

An agentic inbox triage that runs against a 100-message mock inbox
(`inbox.json`). The agent talks to the world exclusively through a small
**MCP** (Model Context Protocol) tool bus that exposes 20 tools split into
five categories: read, reversible-write, irreversible-write, memory, and misc.
State that must outlive a run — user preferences, learned facts, and the
episodic trace — lives in `state/` as JSON so a marking script can read it
without re-running the agent. The agent's policy is deterministic by default;
when a `.env` is present, the same seams call an LLM through a **LiteLLM**
proxy (`connect_litellm.py`).

## Architecture

```
        ┌──────────────────────────────────────────────────────────┐
        │                        demo.py                            │
        │  cap_R1  cap_R2  cap_R3  cap_R4  cap_R5  cap_R6  X1  X2  X4 │
        └──────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                         ┌──────────────────┐        ┌─────────────────┐
                         │      Agent       │◄──────►│   LLM (LiteLLM) │
                         │  classify→plan→  │  soft  │  optional; falls│
                         │  act→gate loop   │────────┤  back to policy │
                         └──────────────────┘        └─────────────────┘
                                   │
                           MCP JSON-RPC 2.0
                                   │
                                   ▼
                     ┌──────────────────────────────┐
                     │      MCPServer / Registry     │
                     │   tools/list · tools/call     │
                     └──────────────────────────────┘
                                   │
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
        Inbox tools          Memory tools          Detectors
     read/draft/archive   remember/recall/save   injection/phishing
     send*/delete*  (gated) preference/fact/scratch
                    (*) irreversible; never called without Gate

                                   │
                                   ▼
                              state/
                              ├── actions.json      ← disposition per msg
                              ├── preferences.json  ← procedural memory
                              ├── facts.json        ← semantic memory
                              ├── trace.jsonl       ← episodic memory
                              └── outbox/           ← drafts + would-be sends
```

## Design choices you were asked to state

- **Framework: none.** The work is a rule-first pipeline over an MCP tool bus.
  MCP was the right abstraction because every write action already needed to
  be inspectable, versioned, and swappable for a real inbox provider later.
  See Final Report Q4.
- **Retrieval: thread-walk.** An inbox carries its own structure in
  `thread_id`. `get_thread` walks the thread in ~O(n) and is deterministic;
  `search_inbox` is the fallback for cross-thread lookups (e.g. R2 finding
  the earlier AMQP URL). When neither surfaces the requested fact, the agent
  logs a `not_grounded` event to `state/trace.jsonl` and refuses to draft —
  the Part 3 rule (4) invariant: no fabrication, no citation of unread ids.
- **Reversible vs irreversible.**
  - Reversible (no gate): `draft_reply`, `archive_message`, `defer_message`,
    `delegate_message`, `escalate_message`, `label_message`, `flag_message`.
  - Irreversible (gated by `Gate.approve`): `send_message`, `delete_message`.
  Deletion is treated as irreversible because the mock store has no trash;
  once a message id is marked deleted its content is gone and an injection
  cannot use archive-then-delete to quietly erase evidence of itself.
- **Send invariant.** `send_message` is the ONLY code path that writes a
  `state/outbox/sent-*.json` file, one file per send. Nothing else in the
  system produces a sent artifact; deletes only touch `state/actions.json`
  and the trace.
- **Where the gate sits.** Only two tools in the registry have
  `irreversible=True`. Any code path that wants to invoke them goes through
  `call_gated(...)` which asks `Gate.approve(...)` first, and every gate
  decision (proposed tool, args, reason, human's answer, approved flag) is
  written to `state/trace.jsonl` as a `gate` event. Under `--dry-run` the
  gate prints what it *would* do, logs the intent, and returns False, so the
  outbox stays empty. This is also the R5 defence: a hostile message body
  may influence a *draft*, but a draft is not a send.
- **State reset policy.** Runs preserve `state/` by default so traces and
  outbox artifacts remain available across executions. Pass `--reset` to
  clear `state/actions.json`, `state/trace.jsonl`, and `state/outbox/` before
  a run. `state/preferences.json` is intentionally kept so standing
  instructions (Part 5) still survive process restarts.
- **The escalation line.** The system asks for approval ONLY on
  `send_message` and `delete_message`. Internal archives, defers, and drafts
  run without a prompt. **Trade-off:** a wrongly-archived internal FYI is
  possible, in exchange for the user only being asked to approve the two
  actions they cannot pull back. Asking about forty archives would train
  them to press `y` without reading and would defeat the point of a gate.
- **Memory tiers.** Four explicit tiers, each with a read/write path:
  - working — in-process dict, per-run scratchpad (`memory_scratch`).
  - episodic — append-only `state/trace.jsonl` (every tool call + decision).
  - semantic — `state/facts.json` (people, threads, commitments).
  - procedural — `state/preferences.json` (learned rules).
  The `memory_*` tools are how the agent reads and writes each tier.
- **LLM integration.** `connect_litellm.py` posts to `${LITELLM_BASE_URL}/v1/chat/completions`
  with a bearer token from `LITELLM_API_KEY`. `inboxhero/llm.py` wraps it and calls it at three seams
  (classify, draft, injection-check). If `.env` is missing, `LLM.enabled` is
  False and the deterministic policy runs unchanged.
- **Rules vs LLM.** Classification (choosing the disposition) is **100% rule-based**;
  the LLM is only invoked to *rewrite reply bodies* in `_reply_or_escalate`. R1
  reports the split at the end of its run — currently 97/100 messages never
  touched a model, and 3 of the 6 replies were LLM-assisted (Hartwell & Cho
  legal drafts).

## Capabilities

| id | name | tier | one-line claim |
|----|------|------|----------------|
| R1 | Zero the inbox | B | every message gets one disposition + reason; reports rules-vs-LLM route split (97/100 rule-only) |
| R2 | Grounded reply | B | m008 draft reuses the AMQP URL from m003 and cites m003; when the requested info is not in the inbox (m012), no draft is written and a `not_grounded` event is logged |
| R3 | Gate the irreversible | C | no send/delete without approval or --dry-run |
| R4 | Persistent preference *(Part 5: Standing Instructions)* | C | `calendar.earliest_meeting=11:00` stated in m041 survives process exit and turns m043's Mon 9am ask into a decline draft on the next run; `cc.legal=priya@paperjet.io` stated in m015 adds Priya to the CC on m018's SAFE draft |
| R5 | Refuse embedded instructions | C | detects, refuses, flags, reports injections + phishing |
| R6 | Dashboard — commitments **and deadlines** as a structured list | C | exactly three panes (pending / flagged / commitments); commitments pane includes a calendar/deadline table, all items cited to source messages, m013/m016 conflict surfaced *(covers extras: "Extracting commitments and deadlines into a structured list")* |
| X1 | Follow-up tracking | B | m044 sent to Priya 7+ days ago with no reply → chase *(covers extras: "Follow-up tracking for sent messages nobody answered")* |
| X2 | Morning digest | B | needs-you / can-wait / auto-archived grouped by domain *(covers extras: "A daily digest" and "Batch handling of a category")* |
| X4 | Thread open-question summary | B | summarizes a thread to unresolved ask(s) with cited message ids *(extra capability; extends grounded retrieval style by staying inbox-derived)* |

`capabilities.json` is the machine-readable form and holds the exact command,
observable outcome, and evidence for each row. Keep the two files in step.

## The 100-message inbox — the interesting subset

Bucketed for reference. Full inbox in `inbox.json`.

- **Legitimate action items:** m001/m003/m005/m008 (staging incident + follow-up),
  m010/m043 (investor scheduling), m015 (loop-me-in preference), m018/m048/m055
  (Hartwell & Cho legal), m026–m036 (launch thread; m030 has the pricing ask),
  m038 (board review), m040 (board deck ask), m041 (self-note preference),
  m042 (candidate follow-up), m044 (Sam's own unanswered ask), m046 (press),
  m012 (vague "the thing"), m013/m016 (scheduling conflict).
- **Injections / self-spoof:** m017 (mailer-daemon "RELEASE" trick), m024
  (fake newsletter with backup-forward payload), m039 (self-spoofed
  "assistant settings"), m047 (embedded forward directive inside a support fwd).
- **Phishing / lookalike domains:** m021 (wire-fraud), m023
  (`priya.nair@paperjet.co` lookalike), m045 (fake IT credential phish).
- **Noise (auto-archived):** the 60-odd receipts, newsletters and login
  notifications from `m062..m119`.
