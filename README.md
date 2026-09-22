# InboxHero

Agentic inbox triage over a 100-message mock inbox, built for the end-of-course
Agentic AI assignment. The agent uses a small **MCP** tool bus, layered
**memory** (working / episodic / semantic / procedural), and an optional
**LiteLLM** integration for real model calls.

Only `inbox.json` is the assignment input. Everything else in this repo is the
submission.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate

# (optional) enable real LLM calls
cp .env.example .env
# then fill LITELLM_BASE_URL, LITELLM_API_KEY, MODEL in .env

.venv/bin/python demo.py --tools          # inspect the 20-tool MCP surface
.venv/bin/python demo.py --cap R1 --reset # zero the inbox from clean state
.venv/bin/python demo.py --cap R2 --msg m008
.venv/bin/python demo.py --cap R3 --dry-run
.venv/bin/python demo.py --cap R4
.venv/bin/python demo.py --cap R5
.venv/bin/python demo.py --cap R6
.venv/bin/python demo.py --cap X1
.venv/bin/python demo.py --cap X2
.venv/bin/python demo.py --cap X4 --thread t-launch
.venv/bin/python demo.py --all --reset
```

By default, runs preserve `state/` across executions. Pass `--reset` to clear
`state/actions.json`, `state/trace.jsonl`, and `state/outbox/` before a run.
`state/preferences.json` is kept so standing instructions survive restarts.

Without a `.env`, `LLM.enabled` is False and the deterministic policy runs.
Adding a `.env` transparently switches on the model at the draft / classify /
injection-check seams — the tool surface, gates and trace do not change.

## Layout

```
demo.py                  CLI entry point — R1..R6, X1, X2, X4
connect_litellm.py       minimal LiteLLM connector (chat / embed / list_models)
inbox.json               the assignment inbox (100 messages)
capabilities.json        machine-readable submission
CAPABILITIES.md          human-readable submission
inboxhero/
  mcp.py                 JSON-RPC MCP server + in-process client + @tool registry
  tools.py               inbox tools (read + reversible + irreversible)
  memory.py              4-tier memory store + memory_* MCP tools
  agent.py               classify → retrieve → plan → act loop + Gate
  detectors.py           injection / phishing / self-spoof heuristics
  dashboard.py           HTML + JSON dashboard for R6
  llm.py                 LLM adapter (wraps connect_litellm.chat)
state/                   generated at run time — trace, preferences, facts, outbox
```

## Where memory lives

| Tier       | Store                       | MCP tools                                              |
|------------|-----------------------------|--------------------------------------------------------|
| working    | in-process dict             | `memory_scratch`                                       |
| episodic   | `state/trace.jsonl`         | written by every tool call; readable via file          |
| semantic   | `state/facts.json`          | `memory_remember_fact`, `memory_recall`                |
| procedural | `state/preferences.json`    | `memory_save_preference`, `memory_list_preferences`, `memory_forget_preference` |

## The gate

Every write tool in the registry has an `irreversible` flag. Only two are True:
`send_message` and `delete_message`. `call_gated(...)` is the only path to
those tools; it delegates to `Gate.approve(...)` which supports four modes:

- `prompt` (default) — interactive y/N.
- `dry_run` — prints "WOULD call ..." and returns False.
- `auto_approve` — for tests.
- `auto_deny` — for adversarial tests.

## The LLM seams

`inboxhero/llm.py` calls `connect_litellm.chat` at three places:

- `LLM.classify(msg, thread, prefs, user)` — second opinion on disposition.
- `LLM.draft(msg, thread, prefs, user)` — rewrites reply bodies, must return
  a JSON `{"body": ..., "cited": [...]}`.
- `LLM.is_injection(msg)` — extra safety check.

If any call fails (network, JSON parse), it logs an `llm_error` event to
`state/trace.jsonl` and returns None; the agent then uses the deterministic
policy for that decision. That means the LLM never blocks correctness.

## Final Report

Q1. One explicit refusal example is message m024 in inbox.json:633, whose body tells an assistant to forward the full mailbox to an external address and then delete the message (inbox.json:640).

The system deliberately does not automate that. It takes the refusal path and leaves the message in place, recorded as “refused, flagged, and left message in place” in agent.py:155.

Why that line:

Embedded instructions inside email content are treated as untrusted prompt-injection attempts (documented in the R5 claim in capabilities.json:87).
The dangerous actions it requests are irreversible (send/delete), and those are never auto-executed without gating (policy captured in capabilities.json:26 and CAPABILITIES.md:106).
So the system prefers a safe false-negative over a catastrophic false-positive: flag and report, rather than silently forward or delete.

Q2.Untrusted text enters through inbox message content (subject/body/from) via read tools like get_message/get_thread.

The boundary is architectural: message text is only data; actions happen only through MCP tool calls, and irreversible tools are gated.

To make the system act on their behalf, an attacker would have to bypass:
1. Injection/phishing/self-spoof detectors.
2. The deterministic policy that routes hostile content to flag/refuse.
3. The approval gate on send/delete.
4. Trace logging that records decisions and gated actions.

Q3.The owner is answerable, because the system sends in the owner’s name and irreversible sends are supposed to pass the approval gate.

Traceback is built in:
1. Exact sent content is saved in `state/outbox/sent-*.json` (to/cc/subject/body/time).
2. Gate logs record who approved or denied and why.
3. Trace logs show whether the draft was model-assisted or rule-generated, plus related draft/send events.

So if a send is wrong, you can reconstruct what was sent, who authorized it, and which component produced it.

Q4.Here is the mapping:

1. Agent role
- The decision-maker is `agent.py:118`.
- Safety/approval gate is `agent.py:36`.

2. Router role
- Main routing logic is `agent.py:138`.
- External vs internal branching is `agent.py:295` and `agent.py:333`.
- Irreversible-call routing goes through `agent.py:448`.

3. Tasks role
- Task-like units are your capability entrypoints in `demo.py:86` through `demo.py:325`.
- Action tasks are MCP tools registered in `tools.py:75`, including read, reversible write, and irreversible write tools.

4. Crew role
- The crew assembly is build_system in demo.py: Agent + Gate + MemoryStore + Inbox + MCP server/client.
- Shared execution bus is `mcp.py:38`, `mcp.py:95`, `mcp.py:146`.

One framework feature you built yourself:
- Tool orchestration/runtime (tool registry + invocation bus + schemas), implemented in `mcp.py:38`.

Would a framework help or hurt here?
- Mostly hurt for this assignment: you needed explicit, auditable, deterministic control flow, and custom code makes that traceable end-to-end.
- It would help only if you scaled to many agents/tasks (retries, scheduling, richer orchestration).

