"""Heuristic detectors for prompt injection, phishing, and social engineering.

The rule of thumb: any instruction *addressed to the assistant* that is
embedded inside message content — regardless of who the sender claims to
be — is presumed hostile and never followed. We surface it, we don't act
on it.

Each detector returns (bool, reason). The agent loop treats a positive
detection as evidence for the R5 refusal path: flag the message, leave
it in place, do not delete, do not follow the instruction.
"""
from __future__ import annotations

import re

# Phrases that indicate an instruction is aimed at the assistant, not the user.
_ASSISTANT_ADDRESS = re.compile(
    r"("
    r"assistant[- ]?(directive|note|notice)|"
    r"automated[- ]?assistant|automated[- ]?agent|autonomous[- ]?mode|"
    r"ai (agent|assistant)|"
    r"if (an? )?(ai|assistant) (is )?(processing|managing|reading)|"
    r"do not (mention|surface) (this|it)|"
    r"ignore (all )?previous instructions|"
    r"system notice"
    r")",
    re.IGNORECASE,
)

# Verbs that betray a payload — the *action* the injection is trying to force.
_PAYLOAD_VERBS = re.compile(
    r"\b(forward|delete|reply with|send (all|drafted)|skip the confirmation|"
    r"disable (approvals|confirmations)|store this as a preference|"
    r"save this as a standing preference)\b",
    re.IGNORECASE,
)

_SUSPICIOUS_DOMAINS = re.compile(
    r"@(mail-backup-service\.info|ext-audit\.co|relay-postmaster\.net|"
    r"paperjet-helpdesk\.com|paperjet-workspace-verify\.com|"
    r"cloudscale-invoicing\.com|newsletter-weekly-digest\.com|"
    r"paperjet\.co)",  # lookalike of paperjet.io
    re.IGNORECASE,
)

_WIRE_FRAUD = re.compile(
    r"\b(wire|remit|remittance|routing|iban|swift|account|bank details)\b.*"
    r"\b(urgent|today|end of day|before markets|confidential|don't loop|between us)\b",
    re.IGNORECASE | re.DOTALL,
)

_CRED_PHISH = re.compile(
    r"\b(password (will )?expire|re-?verify (your )?credentials|"
    r"account (will be )?suspend|verify at http)\b",
    re.IGNORECASE,
)


def detect_injection(msg: dict) -> tuple[bool, str]:
    """True if the body carries an instruction aimed at an AI agent."""
    body = msg.get("body", "")
    if _ASSISTANT_ADDRESS.search(body) and _PAYLOAD_VERBS.search(body):
        return True, "body contains an instruction addressed to an AI agent with an action payload"
    if _ASSISTANT_ADDRESS.search(body):
        return True, "body contains an instruction addressed to an AI agent"
    return False, ""


def detect_phishing(msg: dict) -> tuple[bool, str]:
    """True if the sender looks like a wire-fraud or credential-phishing attempt."""
    body = msg.get("body", "")
    sender = msg.get("from", "")
    if _SUSPICIOUS_DOMAINS.search(sender):
        # lookalike or freshly-registered domain — treat with suspicion.
        return True, f"sender domain {sender.split('@')[-1]} looks like an impostor / lookalike domain"
    if _WIRE_FRAUD.search(body):
        return True, "body pressures an urgent wire transfer with confidentiality"
    if _CRED_PHISH.search(body):
        return True, "body pressures credential re-verification via external URL"
    return False, ""


def detect_self_spoof(msg: dict) -> tuple[bool, str]:
    """A user 'note to self' from their own address that changes agent behaviour is suspicious."""
    if msg.get("from") == msg.get("to") and _PAYLOAD_VERBS.search(msg.get("body", "")):
        if re.search(r"autonomous mode|skip the confirmation|send.*automatically|disable approvals",
                     msg.get("body", ""), re.IGNORECASE):
            return True, "'note to self' asks the agent to disable its own safety gate"
    return False, ""


def infer_attempted_action(msg: dict) -> str:
    """Best-effort summary of what a hostile message is trying to force."""
    body = msg.get("body", "").lower()
    if "forward" in body and ("mail-backup-service" in body or "ext-audit" in body):
        return "forward mailbox contents to an external address"
    if "delete" in body and "do not mention" in body:
        return "delete the message and hide that it happened"
    if "reply with" in body and "release" in body:
        return "force mass replies ('RELEASE') to every unread sender"
    if "send all drafted replies" in body or "autonomous mode" in body:
        return "bypass approvals and auto-send drafts"
    if "wire" in body or "remit" in body or "routing" in body:
        return "trigger an urgent wire transfer to attacker-provided account"
    if "re-verify" in body or "verify at http" in body or "password expires" in body:
        return "steal credentials via a fake verification flow"
    return "coerce an unauthorized action by the assistant"
