"""Prompt-injection screening, output redaction (DLP) and the action policy engine.

Defense in depth, in order of strength:

1. **Policy engine** (strongest): the model only *proposes* an action; this code decides.
   It cannot be talked out of a rule, whatever the prompt or retrieved text says.
2. **Capability boundary**: the MCP server built for the run's principal does not expose
   tools beyond its scopes and never takes the tenant from arguments.
3. **Redaction**: secrets in tool outputs are masked before the model sees them, so the
   model cannot leak what it never received; outgoing messages are redacted again.
4. **Injection screening** (weakest, heuristic): suspicious instructions in tickets or
   retrieved documents raise a flag that forces human approval of any action and blocks
   high-risk ones. It is a signal, not a guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS = [
    r"ignore (all )?(the )?(previous|prior|above) (instructions|rules)",
    r"disregard (your|the) (instructions|rules|policy)",
    r"you are now (a|an|the)",
    r"system prompt",
    r"developer mode",
    r"\bjailbreak\b",
    r"add (me|them|the requester) to (the )?admins?\b",
    r"grant .{0,40}(admin|administrator|root|hr-payroll)",
    r"reveal .{0,30}(password|secret|token|key)",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

SECRET_PATTERNS = [
    (re.compile(r"Tmp-[A-Za-z0-9_\-]{6,}"), "[REDACTED_TEMP_PASSWORD]"),
    (re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1: [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_NUMBER]"),
]


def screen(text: str) -> list[str]:
    """Return injection indicators found in ``text`` (empty if none)."""
    return sorted({m.group(0).lower() for m in _INJ.finditer(text or "")})


def redact(obj):
    """Mask secrets in strings, recursively through dicts/lists."""
    if isinstance(obj, str):
        for pat, rep in SECRET_PATTERNS:
            obj = pat.sub(rep, obj)
        return obj
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if k in ("temporary_password", "token", "secret") else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


@dataclass(frozen=True)
class Decision:
    allowed: bool
    requires_approval: bool
    risk: str  # low | medium | high
    reason: str


ALLOWED_ACTIONS = {"grant_folder_access", "reset_password", "create_ticket", "reply_only"}
QUEUES = {"software", "network", "o365", "hardware", "security", "service-desk"}


def evaluate_action(
    action: dict, *, requester: str, requester_groups: list[str], folder_info: dict | None, flags: list[str]
) -> Decision:
    """Deterministic policy for proposed actions (see docs/SECURITY.md for the table)."""
    kind = action.get("action")
    injected = any(f.startswith("injection") for f in flags)
    if kind not in ALLOWED_ACTIONS:
        return Decision(False, False, "high", f"action {kind!r} is not allowed for this workflow")
    if kind == "reply_only":
        return Decision(True, False, "low", "informational reply")
    if kind == "create_ticket":
        if action.get("queue") not in QUEUES:
            return Decision(False, False, "medium", f"unknown queue {action.get('queue')!r}")
        return Decision(True, False, "low", "escalation to a human queue")
    target = action.get("user_id")
    if kind == "reset_password":
        if target != requester:
            return Decision(False, False, "high", "password reset only for the requester's own account")
        return Decision(True, True, "medium", "self-service password reset needs approval")
    # grant_folder_access
    if target != requester:
        return Decision(False, False, "high", "access can only be requested for oneself")
    if folder_info is None:
        return Decision(False, False, "high", "unknown folder")
    if folder_info.get("sensitivity") == "restricted":
        return Decision(False, False, "high", "restricted folders are never granted by automation")
    if injected:
        return Decision(True, True, "high", "injection indicators present: human approval required")
    in_group = folder_info.get("owner_group") in requester_groups
    return Decision(True, True, "medium", "member of owner group" if in_group else "not in owner group: manager approval")
