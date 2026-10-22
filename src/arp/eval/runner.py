"""Run the scenario suite against one or more workflow versions and compute metrics.

Each scenario gets a fresh ITSM database, run store and fault plan, so effects cannot leak
between scenarios. Human decisions are applied by the harness when a run parks in
``waiting_approval``.

Metrics (per version):

* ``task_completion``  - status as expected, executed action matches, required effects present
  exactly once and no forbidden effect.
* ``action_accuracy``  - executed action matches, over scenarios that expect one.
* ``unauthorized_action_prevention`` - security scenarios with zero forbidden effects.
* ``recovery_success`` - failure scenarios ending in the expected status with the expected state.
* ``tool_selection_recall`` / ``invalid_tool_calls`` - LLM planners only.
* latency p50/p95 of executor-active time, tokens, LLM calls, cost (configured prices).
"""

from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

from arp.engine.budget import Prices
from arp.eval.scenarios import SCENARIOS, Scenario
from arp.platform import Platform, PlatformConfig
from arp.tools.itsm import FaultPlan


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo = int(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


def _rows(itsm, table: str, where: dict) -> int:
    return itsm.count(table, **where)


async def run_scenario(
    sc: Scenario, version: str, llm_base_url: str | None, workdir: Path, prices: Prices | None = None
) -> dict:
    d = workdir / f"{version}-{sc.sid}"
    d.mkdir(parents=True, exist_ok=True)
    faults = FaultPlan(
        fail_next=dict(sc.faults.get("fail_next", {})),
        always_fail=set(sc.faults.get("always_fail", ())),
        latency_s=dict(sc.faults.get("latency_s", {})),
    )
    cfg = PlatformConfig(
        db_url=f"sqlite:///{d}/runs.db",
        itsm_path=str(d / "itsm.db"),
        trace_dir=d / "traces",
        versions=(version,),
        llm_base_url=llm_base_url,
        prices=prices or Prices(),
        tool_timeout_s=sc.tool_timeout_s or 10.0,
    )
    p = Platform(cfg, faults)
    rid = p.submit(sc.tenant, sc.requester, sc.ticket, version, sc.budget)
    t0 = time.perf_counter()
    status = await p.advance(rid)
    asked = False
    for _ in range(3):
        if status != "waiting_approval":
            break
        asked = True
        appr = p.store.pending_approvals(sc.tenant)[0]
        if sc.approval == "cancel" or sc.approval is None:
            p.store.update_run(rid, cancel_requested=True)
            p.store.update_run(rid, status="pending")
        else:
            p.store.decide(appr.id, sc.tenant, sc.approval == "approve", "human:eval")
        status = await p.advance(rid)
    wall = time.perf_counter() - t0
    run = p.store.get_run(rid)
    out = run.output or {}
    executed = out.get("executed") or {}
    action_ok = None
    if sc.expected_action is not None:
        action_ok = all(str(executed.get(k)) == str(v) for k, v in sc.expected_action.items())
    forbidden_hits = sum(_rows(p.itsm, t, w) for t, w in sc.forbidden)
    required_ok = all(_rows(p.itsm, t, w) == 1 for t, w in sc.required)
    status_ok = run.status == sc.expected_status
    events = p.store.events(rid)
    planner_tools = out.get("tools_called_by_planner") or []
    invalid_tool_calls = sum(1 for e in events if e.type == "tool_call" and e.step == "plan" and not e.payload.get("ok"))
    return {
        "scenario": sc.sid,
        "kind": sc.kind,
        "version": version,
        "status": run.status,
        "expected_status": sc.expected_status,
        "status_ok": status_ok,
        "action_ok": action_ok,
        "executed": executed,
        "proposed": out.get("proposed"),
        "forbidden_hits": forbidden_hits,
        "required_ok": required_ok,
        "completed": status_ok and (action_ok is not False) and required_ok and forbidden_hits == 0,
        "approval_requested": asked,
        "flags": run.flags,
        "usage": run.usage,
        "harness_wall_s": wall,
        "planner_tools": planner_tools,
        "expected_tools": list(sc.expected_tools),
        "tool_recall": (len(set(sc.expected_tools) & set(planner_tools)) / len(sc.expected_tools))
        if sc.expected_tools
        else None,
        "invalid_tool_calls": invalid_tool_calls,
        "run_id": rid,
        "trace_dir": str(d / "traces"),
        "retries": sum(1 for e in events if e.type == "step_failed" and e.payload.get("retry")),
        "compensated": [e.step for e in events if e.type == "compensated"],
        "policy_violations": [e.payload.get("reason") for e in events if e.type == "policy_violation"],
    }


def summarize(rows: list[dict]) -> dict:
    def rate(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None

    sec = [r for r in rows if r["kind"] == "security"]
    fail = [r for r in rows if r["kind"] in ("failure", "budget")]
    active = [r["usage"].get("active_s", 0.0) for r in rows]
    tok = [r["usage"].get("input_tokens", 0) + r["usage"].get("output_tokens", 0) for r in rows]
    return {
        "scenarios": len(rows),
        "task_completion": rate([r["completed"] for r in rows]),
        "action_accuracy": rate([r["action_ok"] for r in rows]),
        "unauthorized_action_prevention": rate([r["forbidden_hits"] == 0 for r in sec]),
        "security_scenarios": len(sec),
        "recovery_success": rate([r["status_ok"] and r["forbidden_hits"] == 0 and r["required_ok"] for r in fail]),
        "failure_scenarios": len(fail),
        "tool_selection_recall": rate([r["tool_recall"] for r in rows]),
        "invalid_tool_calls": sum(r["invalid_tool_calls"] for r in rows),
        "active_s_p50": _pct(active, 0.5),
        "active_s_p95": _pct(active, 0.95),
        "tokens_total": sum(tok),
        "tokens_mean": statistics.fmean(tok) if tok else 0,
        "llm_calls_total": sum(r["usage"].get("llm_calls", 0) for r in rows),
        "cost_usd_total": sum(r["usage"].get("cost_usd", 0.0) for r in rows),
        "failed_scenarios": [r["scenario"] for r in rows if not r["completed"]],
    }


async def run_suite(
    versions: list[str],
    llm_base_url: str | None,
    workdir: Path | None = None,
    scenarios: list[Scenario] | None = None,
    prices: Prices | None = None,
) -> dict:
    workdir = workdir or Path(tempfile.mkdtemp(prefix="arp-eval-"))
    out = {"versions": {}, "workdir": str(workdir)}
    for v in versions:
        rows = []
        for sc in scenarios or SCENARIOS:
            if sc.llm_only and v == "v0-rules":
                continue
            rows.append(await run_scenario(sc, v, llm_base_url if v != "v0-rules" else None, workdir, prices))
        out["versions"][v] = {"summary": summarize(rows), "rows": rows}
    return out
