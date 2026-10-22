"""Executor semantics on the rule-based workflow (no LLM needed)."""

from __future__ import annotations

import anyio
import pytest

from arp.platform import Platform, PlatformConfig
from arp.telemetry import load_trace
from arp.tools.itsm import FaultPlan


@pytest.fixture()
def make(tmp_path):
    def _make(faults=None, **cfg):
        return Platform(
            PlatformConfig(
                db_url=f"sqlite:///{tmp_path}/runs.db",
                itsm_path=str(tmp_path / "itsm.db"),
                trace_dir=tmp_path / "traces",
                versions=("v0-rules",),
                **cfg,
            ),
            faults,
        )

    return _make


def run(p, rid):
    return anyio.run(p.advance, rid)


def approve(p, tenant, ok=True):
    a = p.store.pending_approvals(tenant)[0]
    p.store.decide(a.id, tenant, ok, "human:test")


def types(p, rid):
    return [e.type for e in p.store.events(rid)]


def test_approval_pause_resume_replays_completed_steps(make):
    p = make()
    rid = p.submit("acme", "u100", "I need access to the finance-reports folder.")
    assert run(p, rid) == "waiting_approval"
    assert p.itsm.count("access", user_id="u100") == 0  # nothing happens before approval
    approve(p, "acme")
    assert run(p, rid) == "succeeded"
    ev = p.store.events(rid)
    started = [e.step for e in ev if e.type == "step_started"]
    assert started.count("classify") == 1  # replayed, not re-executed, after resume
    assert p.itsm.count("access", user_id="u100", folder="finance-reports") == 1


def test_denial_skips_action_but_notifies(make):
    p = make()
    rid = p.submit("acme", "u101", "Please grant me access to the eng-designs folder.")
    run(p, rid)
    approve(p, "acme", ok=False)
    assert run(p, rid) == "rejected"
    assert p.itsm.count("access", user_id="u101") == 0
    assert p.itsm.count("messages", user_id="u101") == 1


def test_transient_failures_are_retried_with_backoff(make):
    p = make(FaultPlan(fail_next={"notify_user": 2}))
    rid = p.submit("acme", "u102", "Please install Slack on my workstation.")
    assert run(p, rid) == "succeeded"
    fails = [e for e in p.store.events(rid) if e.type == "step_failed"]
    assert [f.payload["retry"] for f in fails] == [True, True]
    assert p.itsm.count("messages", user_id="u102") == 1


def test_permanent_failure_after_side_effect_is_compensated(make):
    p = make(FaultPlan(always_fail={"has_access"}))
    rid = p.submit("acme", "u100", "I need access to the finance-reports folder.")
    run(p, rid)
    approve(p, "acme")
    assert run(p, rid) == "failed"
    assert "compensated" in types(p, rid)
    assert p.itsm.count("access", user_id="u100") == 0  # granted, then revoked


def test_tool_timeout_is_enforced(make):
    p = make(FaultPlan(latency_s={"get_groups": 1.5}), tool_timeout_s=0.3)
    rid = p.submit("acme", "u101", "Please give me access to the sales-decks folder.")
    assert run(p, rid) == "failed"
    fails = [e for e in p.store.events(rid) if e.type == "step_failed"]
    assert len(fails) == 3 and all("timed out" in f.payload["error"] for f in fails)


def test_budget_exceeded_stops_before_side_effects(make):
    p = make()
    rid = p.submit("acme", "u100", "I need access to the finance-reports folder.", budget={"max_tool_calls": 2})
    assert run(p, rid) == "budget_exceeded"
    assert p.itsm.count("access") == 0


def test_cancellation_while_waiting(make):
    p = make()
    rid = p.submit("acme", "u100", "I need access to the finance-reports folder.")
    run(p, rid)
    p.store.update_run(rid, cancel_requested=True, status="pending")
    assert run(p, rid) == "cancelled"
    assert p.itsm.count("access") == 0


def test_cancellation_after_side_effect_compensates(make):
    p = make()
    rid = p.submit("acme", "u100", "I need access to the finance-reports folder.")
    run(p, rid)
    approve(p, "acme")
    # Let execute run, then request cancellation before verify.
    wf = p.workflows["it-ticket@v0-rules"]
    original = next(s for s in wf.steps if s.name == "execute").fn

    async def execute_then_cancel(ctx):
        out = await original(ctx)
        p.store.update_run(ctx.run_id, cancel_requested=True)
        return out

    next(s for s in wf.steps if s.name == "execute").fn = execute_then_cancel
    assert run(p, rid) == "cancelled"
    assert "compensated" in types(p, rid)
    assert p.itsm.count("access", user_id="u100") == 0


def test_lease_prevents_concurrent_execution(make):
    p = make()
    rid = p.submit("acme", "u102", "My VPN does not connect.")
    assert p.store.acquire_lease(rid, "other-worker", 60)
    assert run(p, rid) == "locked"


def test_tenant_concurrency_limit(make):
    p = make(max_running_per_tenant=1)
    a = p.submit("acme", "u102", "My VPN does not connect.")
    b = p.submit("acme", "u101", "My VPN does not connect.")
    p.store.update_run(a, status="running")  # simulate a run in flight
    assert run(p, b) == "throttled"


def test_trace_covers_run_steps_and_tools(make):
    p = make()
    rid = p.submit("acme", "u102", "Please install Slack on my workstation.")
    run(p, rid)
    spans = load_trace(p.cfg.trace_dir, rid)
    names = {s["name"] for s in spans}
    assert "workflow.run" in names and "workflow.step execute" in names and "tool.call create_ticket" in names
    assert len({s["trace_id"] for s in spans}) == 1
