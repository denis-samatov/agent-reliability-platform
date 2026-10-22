"""The IT-ticket workflow (four versions sharing the same durable skeleton).

    screen -> classify -> plan -> policy -> approval -> execute -> verify -> notify -> finalize

* ``plan`` is the only step that differs between versions (rules / LLM prompt v1 / v2 / v2 + proposal validation = v3).
* ``policy`` turns a disallowed proposal into an escalation to the security queue (the
  forbidden effect never happens; the event log records the violation).
* ``approval`` parks the run for significant actions; a denial skips to ``notify``.
* ``execute`` is side-effecting and compensable (folder grants are revoked if ``verify``
  fails or the run is cancelled afterwards).
"""

from __future__ import annotations

from arp.agent.planners import READ_TOOLS, LLMPlanner, RulePlanner
from arp.engine.executor import Rejected, RetryPolicy, Step, WaitForApproval, Workflow
from arp.security.guards import evaluate_action, redact, screen

STEP_TOOLS = {
    "classify": {"classify_ticket"},
    "plan": set(READ_TOOLS),
    "policy": {"get_groups", "get_folder"},
    "execute": {"grant_folder_access", "reset_password", "create_ticket"},
    "execute.compensate": {"revoke_folder_access"},
    "verify": {"check_access", "get_user"},
    "notify": {"notify_user"},
}
STEP_TOOLS["execute"] |= STEP_TOOLS["execute.compensate"]


async def screen_step(ctx) -> dict:
    found = screen(ctx.input["ticket"])
    if found:
        ctx.flags.append("injection_in_ticket")
        ctx.store.append(ctx.run_id, "flag", "screen", {"flag": "injection_in_ticket", "indicators": found})
    return {"indicators": found}


async def classify_step(ctx) -> dict:
    out = await ctx.tools.call("classify_ticket", {"text": ctx.input["ticket"]})
    return {"category": out["category"], "classifier_action": out["action"], "review_reasons": out["review_reasons"]}


def make_plan_step(planner):
    async def plan_step(ctx) -> dict:
        plan = await planner.plan(ctx, ctx.input["ticket"], ctx.outputs["classify"]["category"])
        return {
            "action": plan.action,
            "tools_called": plan.tools_called,
            "llm_turns": plan.llm_turns,
            "planner": planner.version,
            "note": plan.note,
        }

    return plan_step


async def policy_step(ctx) -> dict:
    action = dict(ctx.outputs["plan"]["action"])
    groups = (await ctx.tools.call("get_groups", {"user_id": ctx.requester}))["groups"]
    folder_info = None
    if action.get("action") == "grant_folder_access" and action.get("folder"):
        try:
            folder_info = await ctx.tools.call("get_folder", {"folder": action["folder"]})
        except Exception:  # noqa: BLE001 - unknown folder -> policy denies
            folder_info = None
    d = evaluate_action(action, requester=ctx.requester, requester_groups=groups, folder_info=folder_info, flags=ctx.flags)
    if not d.allowed:
        ctx.store.append(ctx.run_id, "policy_violation", "policy", {"proposed": action, "reason": d.reason})
        ctx.flags.append("policy_blocked")
        action = {
            "action": "create_ticket",
            "queue": "security",
            "summary": f"Blocked automated action ({d.reason}). Original request: {ctx.input['ticket'][:200]}",
        }
        return {
            "action": action,
            "allowed": False,
            "requires_approval": False,
            "risk": d.risk,
            "reason": d.reason,
            "blocked_proposal": ctx.outputs["plan"]["action"],
        }
    return {"action": action, "allowed": True, "requires_approval": d.requires_approval, "risk": d.risk, "reason": d.reason}


async def approval_step(ctx) -> dict:
    a = ctx.store.approval(ctx.run_id, "approval")
    if a is None:
        aid = ctx.store.request_approval(ctx.run_id, ctx.tenant, "approval", ctx.outputs["policy"]["action"])
        raise WaitForApproval(aid)
    if a.status == "pending":
        raise WaitForApproval(a.id)
    if a.status == "denied":
        raise Rejected(a.reason or "denied")
    return {"approved_by": a.decided_by}


async def execute_step(ctx) -> dict:
    a = ctx.outputs["policy"]["action"]
    kind = a["action"]
    if kind == "grant_folder_access":
        r = await ctx.tools.call("grant_folder_access", {"user_id": a["user_id"], "folder": a["folder"]})
    elif kind == "reset_password":
        r = await ctx.tools.call("reset_password", {"user_id": a["user_id"]})
    elif kind == "create_ticket":
        r = await ctx.tools.call(
            "create_ticket",
            {"user_id": ctx.requester, "queue": a["queue"], "summary": a.get("summary", ctx.input["ticket"])[:500]},
        )
    else:
        r = {"reply_only": True}
    return {"action": kind, "result": r}


async def compensate_execute(ctx, out: dict) -> dict:
    if out.get("action") != "grant_folder_access":
        return {"noop": True}
    a = ctx.outputs["policy"]["action"]
    ctx.current_step = "execute.compensate"
    return await ctx.tools.call("revoke_folder_access", {"user_id": a["user_id"], "folder": a["folder"]})


async def verify_step(ctx) -> dict:
    a = ctx.outputs["policy"]["action"]
    if a["action"] == "grant_folder_access":
        ok = (await ctx.tools.call("check_access", {"user_id": a["user_id"], "folder": a["folder"]}))["has_access"]
        if not ok:
            raise RuntimeError("verification failed: access not present after grant")
        return {"verified": True}
    return {"verified": True, "note": "no state check for this action"}


async def notify_step(ctx) -> dict:
    if ctx.rejected:
        body = "Your request was reviewed and not approved. A service-desk agent will contact you."
    else:
        a = ctx.outputs["policy"]["action"]
        res = ctx.outputs.get("execute", {}).get("result", {})
        body = {
            "grant_folder_access": f"Access to {a.get('folder')} has been granted.",
            "reset_password": "Your password was reset; the temporary password is sent by SMS.",
            "create_ticket": f"Your request was forwarded to the {a.get('queue')} team (ticket {res.get('ticket_id')}).",
            "reply_only": a.get("message", "We have received your request."),
        }[a["action"]]
    body = redact(body)
    await ctx.tools.call("notify_user", {"user_id": ctx.requester, "body": body})
    return {"message": body}


async def finalize_step(ctx) -> dict:
    pol = ctx.outputs.get("policy", {})
    return {
        "category": ctx.outputs.get("classify", {}).get("category"),
        "proposed": ctx.outputs.get("plan", {}).get("action"),
        "executed": None if ctx.rejected else pol.get("action"),
        "blocked": not pol.get("allowed", True),
        "rejected": ctx.rejected,
        "flags": sorted(set(ctx.flags)),
        "tools_called_by_planner": ctx.outputs.get("plan", {}).get("tools_called", []),
    }


def build_workflow(version: str, llm=None, model_fingerprint: dict | None = None) -> Workflow:
    from arp.agent.planners import PROMPTS

    if version == "v0-rules":
        planner = RulePlanner()
    elif version == "v3-llm":
        planner = LLMPlanner(llm, prompt_version="v2", validate=True, version="v3-llm")
    else:
        planner = LLMPlanner(llm, prompt_version=version.split("-")[0])

    tool_retry = RetryPolicy(max_attempts=3, base_delay_s=0.05)
    steps = [
        Step("screen", screen_step, timeout_s=5),
        Step("classify", classify_step, timeout_s=20, retry=tool_retry),
        Step("plan", make_plan_step(planner), timeout_s=300, retry=RetryPolicy(max_attempts=2)),
        Step("policy", policy_step, timeout_s=20, retry=tool_retry),
        Step("approval", approval_step, when=lambda c: c.outputs["policy"]["requires_approval"]),
        Step("execute", execute_step, timeout_s=20, retry=tool_retry, side_effect=True, compensate=compensate_execute),
        Step("verify", verify_step, timeout_s=20, retry=tool_retry),
        Step(
            "notify",
            notify_step,
            timeout_s=20,
            retry=RetryPolicy(max_attempts=4, base_delay_s=0.05),
            side_effect=True,
            always=True,
        ),
        Step("finalize", finalize_step, always=True),
    ]
    config = {
        "planner": planner.version,
        "prompt": PROMPTS.get(getattr(planner, "prompt_version", "")),
        "validate_proposal": getattr(planner, "validate", False),
        "model": model_fingerprint,
        "step_tools": {k: sorted(v) for k, v in STEP_TOOLS.items()},
    }
    return Workflow("it-ticket", version, steps, config=config)
