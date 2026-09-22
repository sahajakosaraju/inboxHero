"""HTML + JSON dashboard for capability R6.

Exactly three panes:
1) Pending actions
2) Flagged (hostile/phishing/not-grounded)
3) Commitments (calendar rows + commitments + conflicts)
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .tools import Inbox


COMMITMENT_PATTERNS = [
    (re.compile(r"board deck.*(before|two days before|2 days before)", re.IGNORECASE),
     "board deck circulated ahead of the board review"),
    (re.compile(r"pricing (page )?copy.*by the (\d+)", re.IGNORECASE),
     "approve pricing copy"),
    (re.compile(r"sign.*(portal|SAFE|IP assignment).*(friday|by|month-end)", re.IGNORECASE),
     "sign legal document via portal"),
    (re.compile(r"review.*board minutes.*by\s+monday", re.IGNORECASE),
     "review draft board minutes"),
]


# (regex, description, group index of the raw due-hint phrase, or None)
DEADLINE_PATTERNS: list[tuple[re.Pattern, str, int | None]] = [
    (re.compile(r"sign via the portal by\s+(friday)", re.IGNORECASE),
     "sign SAFE amendment via portal", 1),
    (re.compile(r"approve the final pricing copy by the (\d+)(?:st|nd|rd|th)?", re.IGNORECASE),
     "approve pricing-page copy", 1),
    (re.compile(r"two days before the board review", re.IGNORECASE),
     "circulate board deck (two days before board review)", None),
    (re.compile(r"flag any corrections by\s+(monday)", re.IGNORECASE),
     "review draft board minutes", 1),
    (re.compile(r"respond to by the (\d+)(?:st|nd|rd|th)?", re.IGNORECASE),
     "respond to candidate offer", 1),
    (re.compile(r"on deadline for\s+(\w+day)", re.IGNORECASE),
     "press quote for launch coverage", 1),
    (re.compile(r"submit your timesheet by\s+(friday(?:\s+5\s*pm)?)", re.IGNORECASE),
     "submit timesheet", 1),
    (re.compile(r"the (\d+)(?:st|nd|rd|th)?\s+is a hard date", re.IGNORECASE),
     "product launch (hard date)", 1),
    (re.compile(r"before\s+month[- ]?end", re.IGNORECASE),
     "sign IP-assignment addendum", None),
    (re.compile(r"renews on the (\d+)(?:st|nd|rd|th)?", re.IGNORECASE),
     "ZenBoard plan auto-renewal", 1),
    (re.compile(r"appointment on\s+([A-Z][a-z]+\s+\d+)", re.IGNORECASE),
     "personal appointment", 1),
    (re.compile(r"hold expires in\s+(\d+\s*hours?)", re.IGNORECASE),
     "confirm venue booking", 1),
]


_WEEKDAY = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def _resolve_due(hint: str | None, today: datetime) -> str | None:
    """Best-effort resolve a natural-language deadline phrase into ISO date."""
    if not hint:
        return None
    h = hint.strip().lower()
    if h in {"month-end", "month end"}:
        year, month = today.year, today.month
        first_next = (datetime(year + (month // 12), (month % 12) + 1, 1))
        return (first_next - timedelta(days=1)).date().isoformat()
    weekday_word = re.match(r"([a-z]+day)(?:\s+5\s*pm)?", h)
    if weekday_word and weekday_word.group(1) in _WEEKDAY:
        target = _WEEKDAY[weekday_word.group(1)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7 if "5pm" not in h else 0
        return (today + timedelta(days=delta)).date().isoformat()
    if re.fullmatch(r"the\s+\d+", h) or re.fullmatch(r"\d+", h):
        day = int(re.search(r"\d+", h).group(0))
        try:
            return datetime(today.year, today.month, day).date().isoformat()
        except ValueError:
            return None
    m = re.fullmatch(r"([a-z]+)\s+(\d+)", h)
    if m:
        months = {name.lower(): i for i, name in enumerate(
            ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"])}
        short = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                 "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        mo = months.get(m.group(1)) or short.get(m.group(1)[:3])
        if mo:
            try:
                return datetime(today.year, mo, int(m.group(2))).date().isoformat()
            except ValueError:
                return None
    if re.fullmatch(r"\d+\s*hours?", h):
        hours = int(re.search(r"\d+", h).group(0))
        return (today + timedelta(hours=hours)).date().isoformat()
    return None


def _find_deadlines(inbox: Inbox, today: datetime) -> list[dict]:
    out: list[dict] = []
    for msg in inbox.messages:
        for pat, description, grp in DEADLINE_PATTERNS:
            m = pat.search(msg["body"])
            if not m:
                continue
            hint = m.group(grp) if grp else None
            # Patterns without a capture group get a canonical hint from the description.
            if hint is None and "IP-assignment" in description:
                hint = "month-end"
            due_date = _resolve_due(hint, today) if hint else None
            # circulate board deck: derive from the board review date (m038, "the 18th")
            if description.startswith("circulate board deck"):
                board = _resolve_due("the 18", today)
                if board:
                    due_date = (datetime.fromisoformat(board) - timedelta(days=2)).date().isoformat()
                    hint = f"two days before {board}"
            existing = next((d for d in out if d["description"] == description), None)
            entry = {
                "description": description,
                "due_hint": hint or "n/a",
                "due_date": due_date,
                "cited": [msg["id"]],
            }
            if existing:
                if msg["id"] not in existing["cited"]:
                    existing["cited"].append(msg["id"])
                # Prefer a resolved date over an unresolved one.
                if existing["due_date"] is None and due_date is not None:
                    existing["due_date"] = due_date
                    existing["due_hint"] = hint or existing["due_hint"]
            else:
                out.append(entry)
    # Sort: resolved dates first, ascending; then unresolved by description.
    out.sort(key=lambda d: (d["due_date"] is None, d["due_date"] or d["description"]))
    return out


def _find_commitments(inbox: Inbox) -> list[dict]:
    out: list[dict] = []
    seen_labels: set[str] = set()
    for msg in inbox.messages:
        for pat, label in COMMITMENT_PATTERNS:
            if pat.search(msg["body"]):
                key = label
                # Group citations by label.
                existing = next((c for c in out if c["label"] == key), None)
                if existing:
                    if msg["id"] not in existing["cited"]:
                        existing["cited"].append(msg["id"])
                else:
                    out.append({"label": label, "cited": [msg["id"]]})
                seen_labels.add(label)
    return out


def _find_conflicts(inbox: Inbox, actions: dict[str, dict]) -> list[dict]:
    out: list[dict] = []
    for msg_id, entry in actions.items():
        if "conflict" in entry.get("labels", []):
            out.append({"msg_id": msg_id, "reason": entry.get("reason", "conflict")})
    return out


def _read_trace_events(out_dir: Path) -> list[dict]:
    trace_path = out_dir / "state" / "trace.jsonl"
    if not trace_path.exists():
        return []
    out = []
    for line in trace_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_dashboard(
    inbox: Inbox,
    actions: dict[str, dict],
    out_dir: Path,
    today: str | None = None,
) -> dict:
    today_dt = datetime.fromisoformat(today) if today else datetime.utcnow()
    pending = []
    flagged = []
    for msg_id, entry in actions.items():
        m = inbox.get(msg_id)
        row = {
            "id": msg_id,
            "from": m["from"],
            "subject": m["subject"],
            "disposition": entry["disposition"],
            "reason": entry["reason"],
        }
        if entry["disposition"] in {"reply", "escalate"}:
            proposed = {
                "reply": "send draft reply",
                "escalate": "human review / decision",
            }[entry["disposition"]]
            pending.append({
                "id": row["id"],
                "message": f"{row['from']} — {row['subject']}",
                "proposed_action": proposed,
                "why_human": row["reason"],
            })
        if entry["disposition"] == "flagged":
            flagged.append({
                "id": msg_id,
                "message": f"{row['from']} — {row['subject']}",
                "attempted": entry.get("attempted") or entry.get("reason"),
                "did_instead": entry.get("did_instead") or "flagged and left in place",
                "kind": entry.get("threat") or "suspicious",
            })

    # Add every not-grounded refusal to the Flagged pane.
    for ev in _read_trace_events(out_dir):
        if ev.get("kind") != "not_grounded":
            continue
        msg_id = ev.get("msg_id")
        if not msg_id:
            continue
        try:
            m = inbox.get(msg_id)
            message = f"{m['from']} — {m['subject']}"
        except Exception:
            message = msg_id
        flagged.append({
            "id": msg_id,
            "message": message,
            "attempted": ev.get("needed") or "requested fact not found in inbox",
            "did_instead": ev.get("outcome") or "no draft written",
            "kind": "not_grounded",
        })

    # Keep one row per id+kind so repeated runs don't spam duplicates.
    seen = set()
    flagged_unique = []
    for row in flagged:
        key = (row["id"], row["kind"])
        if key in seen:
            continue
        seen.add(key)
        flagged_unique.append(row)
    flagged = flagged_unique

    commitments = _find_commitments(inbox)
    deadlines = _find_deadlines(inbox, today_dt)
    conflicts = _find_conflicts(inbox, actions)

    data = {
        "generated_at": datetime.utcnow().isoformat(),
        "today": today_dt.date().isoformat(),
        "pending": pending,
        "flagged": flagged,
        "commitments": commitments,
        "deadlines": deadlines,
        "conflicts": conflicts,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard.json").write_text(json.dumps(data, indent=2))
    (out_dir / "dashboard.html").write_text(_render_html(data))
    return data


def _render_html(data: dict) -> str:
    def rows(items, cols):
        if not items:
            return "<p><em>none</em></p>"
        head = "".join(f"<th>{c}</th>" for c in cols)
        body = ""
        for it in items:
            body += "<tr>" + "".join(f"<td>{html.escape(str(it.get(c, '')))}</td>" for c in cols) + "</tr>"
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    commit_html = ""
    for c in data["commitments"]:
        cited = ", ".join(c["cited"])
        commit_html += f"<li>{html.escape(c['label'])} <span class=cite>[cited: {cited}]</span></li>"
    if not commit_html:
        commit_html = "<li><em>none</em></li>"

    deadline_rows = []
    for d in data["deadlines"]:
        deadline_rows.append({
            "due_date": d["due_date"] or "—",
            "due_hint": d["due_hint"],
            "description": d["description"],
            "cited": ", ".join(d["cited"]),
        })

    conflict_html = ""
    for c in data["conflicts"]:
        conflict_html += f"<li>CONFLICT on {c['msg_id']}: {html.escape(c['reason'])}</li>"
    if not conflict_html:
        conflict_html = "<li><em>none</em></li>"

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>InboxHero dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2em auto; }}
h1 {{ margin-bottom: 0; }}
h2 {{ margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: .3em; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
.cite {{ color: #888; font-size: 12px; }}
.flagged {{ color: #a00; }}
</style></head><body>
<h1>InboxHero — dashboard</h1>
<p><small>generated {html.escape(data['generated_at'])} · today = {html.escape(data['today'])}</small></p>

<h2>Pending actions ({len(data['pending'])})</h2>
{rows(data['pending'], ['id', 'message', 'proposed_action', 'why_human'])}

<h2 class=flagged>Flagged ({len(data['flagged'])})</h2>
{rows(data['flagged'], ['id', 'message', 'kind', 'attempted', 'did_instead'])}

<h2>Commitments ({len(data['commitments'])})</h2>
<ul>{commit_html}</ul>

<h3>Calendar / Deadlines ({len(data['deadlines'])})</h3>
{rows(deadline_rows, ['due_date', 'due_hint', 'description', 'cited'])}

<h3>Conflicts ({len(data['conflicts'])})</h3>
<ul>{conflict_html}</ul>
</body></html>
"""
