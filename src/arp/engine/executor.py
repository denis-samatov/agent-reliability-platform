"""Durable workflow executor.

A workflow is an ordered list of :class:`Step`. The executor advances one run at a time
under a lease, records ``step_started`` / ``step_completed`` / ``step_failed`` events, and
on restart replays completed steps from the log instead of executing them again.

Guarantees and their mechanisms:

* **At-least-once step execution, exactly-once effects** - a crash between a side effect
  and its ``step_completed`` event re-runs the step; side-effecting tools receive an
  idempotency key derived from (run, step), so the second call is a replay
  (``tests/test_recovery.py`` kills a worker process at that point).
* **Retries** - per-step policy, exponential backoff, only for retryable errors.
* **Timeouts** - per-step deadline (``anyio.fail_after``).
* **Budgets** - tokens, tool calls, wall time and cost are checked before every LLM/tool
  call (:class:`arp.engine.budget.Budget`).
* **Human approval** - a step raises :class:`WaitForApproval`; the run parks in
  ``waiting_approval`` and resumes when a decision is recorded.
* **Cancellation and compensation** - cancellation is checked between steps; completed
  side-effecting steps are compensated in reverse order on cancel or failure.
* **Tenant concurrency limit** - a run starts only if its tenant has fewer than N running.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio

from arp.engine.budget import Budget, BudgetExceeded
from arp.engine.store import TERMINAL, Store


class TransientError(RuntimeError):
    pass


class PolicyViolation(RuntimeError):
    """The proposed action is not allowed. Terminal: the run ends ``blocked``."""


class WaitForApproval(Exception):  # noqa: N818 - control-flow signal, not an error
    def __init__(self, approval_id: int):
        super().__init__(approval_id)
        self.approval_id = approval_id


class Rejected(Exception):  # noqa: N818
    """A human denied the action; the workflow ends ``rejected`` after its cleanup steps."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.05
    max_delay_s: float = 1.0
    retry_on: tuple[type[BaseException], ...] = (TransientError, TimeoutError)

    def delay(self, attempt: int) -> float:
        return min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))


@dataclass
class Step:
    name: str
    fn: Callable[[RunContext], Awaitable[dict]]
    timeout_s: float = 60.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    side_effect: bool = False
    compensate: Callable[[RunContext, dict], Awaitable[dict]] | None = None
    when: Callable[[RunContext], bool] | None = None
    always: bool = False  # runs even after a rejection (e.g. notify the requester)


@dataclass
class RunContext:
    run_id: str
    tenant: str
    requester: str
    input: dict
    store: Store
    budget: Budget
    tools: object  # ToolGateway
    llm: object | None
    tracer: object
    outputs: dict[str, dict] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    rejected: bool = False
    current_step: str = ""
    attempt: int = 1

    def idempotency_key(self, suffix: str = "") -> str:
        return hashlib.sha256(f"{self.run_id}:{self.current_step}:{suffix}".encode()).hexdigest()[:32]


@dataclass
class Workflow:
    name: str
    version: str
    steps: list[Step]
    description: str = ""
    config: dict = field(default_factory=dict)  # prompts, model, tool allowlist -> part of version hash

    @property
    def version_hash(self) -> str:
        import inspect
        import json

        src = "".join(inspect.getsource(s.fn) for s in self.steps)
        return hashlib.sha256((src + json.dumps(self.config, sort_keys=True, default=str)).encode()).hexdigest()[:16]


class Executor:
    def __init__(
        self,
        store: Store,
        workflows: dict[str, Workflow],
        context_factory,
        owner: str,
        lease_s: float = 30.0,
        max_running_per_tenant: int = 4,
    ):
        self.store = store
        self.workflows = workflows
        self.context_factory = context_factory  # (run, workflow) -> RunContext
        self.owner = owner
        self.lease_s = lease_s
        self.max_running = max_running_per_tenant

    def _wf(self, run) -> Workflow:
        return self.workflows[f"{run.workflow}@{run.version}"]

    async def run(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        if run is None or run.status in TERMINAL or run.status == "waiting_approval":
            return run.status if run else "missing"
        if run.status == "pending" and self.store.running_count(run.tenant) >= self.max_running:
            return "throttled"
        if not self.store.acquire_lease(run_id, self.owner, self.lease_s):
            return "locked"
        try:
            return await self._advance(run_id)
        finally:
            self.store.release_lease(run_id, self.owner)

    async def _advance(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        wf = self._wf(run)
        ctx: RunContext = self.context_factory(run, wf)
        self.store.update_run(run_id, status="running")
        completed, compensated = {}, set()
        for ev in self.store.events(run_id):
            if ev.type == "step_completed":
                completed[ev.step] = ev.payload.get("output", {})
            elif ev.type == "compensated":
                compensated.add(ev.step)
            elif ev.type == "rejected":
                ctx.rejected = True
            elif ev.type == "flag":
                ctx.flags.append(ev.payload["flag"])
        if not completed:
            self.store.append(
                run_id,
                "run_started",
                payload={"workflow": wf.name, "version": wf.version, "version_hash": wf.version_hash, "owner": self.owner},
            )
        else:
            self.store.append(run_id, "run_resumed", payload={"owner": self.owner, "replayed_steps": list(completed)})
        with ctx.tracer.run_span(run, wf) as span:
            async with ctx.tools.session():
                status = await self._steps(ctx, wf, completed, compensated)
            span.set_attribute("run.status", status)
        return status

    async def _steps(self, ctx: RunContext, wf: Workflow, completed: dict, compensated: set) -> str:
        run_id = ctx.run_id
        for step in wf.steps:
            if self.store.get_run(run_id).cancel_requested:
                await self._compensate(ctx, wf, completed, compensated, reason="cancelled")
                return self._finish(ctx, "cancelled", "cancelled by request")
            if step.name in completed:
                ctx.outputs[step.name] = completed[step.name]
                continue
            if ctx.rejected and not step.always:
                continue
            ctx.current_step = step.name
            if step.when is not None and not step.when(ctx):
                self.store.append(run_id, "step_skipped", step.name)
                completed[step.name] = {"skipped": True}
                ctx.outputs[step.name] = {"skipped": True}
                continue
            for attempt in range(1, step.retry.max_attempts + 1):
                ctx.attempt = attempt
                self.store.append(run_id, "step_started", step.name, {"attempt": attempt})
                t0 = time.perf_counter()
                try:
                    with ctx.tracer.step_span(step.name, attempt):
                        with anyio.fail_after(step.timeout_s):
                            out = await step.fn(ctx)
                except WaitForApproval as w:
                    self.store.append(run_id, "approval_requested", step.name, {"approval_id": w.approval_id})
                    self._persist_usage(ctx)
                    self.store.update_run(run_id, status="waiting_approval")
                    return "waiting_approval"
                except Rejected as r:
                    self.store.append(run_id, "rejected", step.name, {"reason": str(r)})
                    ctx.rejected = True
                    completed[step.name] = {"rejected": True}
                    ctx.outputs[step.name] = {"rejected": True}
                    break
                except BudgetExceeded as b:
                    self.store.append(run_id, "budget_exceeded", step.name, b.details)
                    await self._compensate(ctx, wf, completed, compensated, reason="budget")
                    return self._finish(ctx, "budget_exceeded", str(b))
                except PolicyViolation as p:
                    self.store.append(run_id, "policy_violation", step.name, {"reason": str(p)})
                    await self._compensate(ctx, wf, completed, compensated, reason="policy")
                    return self._finish(ctx, "blocked", str(p))
                except step.retry.retry_on as e:
                    retry = attempt < step.retry.max_attempts
                    self.store.append(
                        run_id,
                        "step_failed",
                        step.name,
                        {"attempt": attempt, "error": _err(e), "retry": retry, "elapsed_s": time.perf_counter() - t0},
                    )
                    if retry:
                        await anyio.sleep(step.retry.delay(attempt))
                        continue
                    await self._compensate(ctx, wf, completed, compensated, reason="failure")
                    return self._finish(ctx, "failed", f"{step.name}: {_err(e)}")
                except Exception as e:  # noqa: BLE001 - non-retryable step failure
                    self.store.append(
                        run_id, "step_failed", step.name, {"attempt": attempt, "error": _err(e), "retry": False}
                    )
                    await self._compensate(ctx, wf, completed, compensated, reason="failure")
                    return self._finish(ctx, "failed", f"{step.name}: {_err(e)}")
                # Fault injection for recovery tests: die after the side effect happened but
                # before it was recorded. A new executor must not duplicate the effect.
                if os.environ.get("ARP_CRASH_AFTER_STEP") == step.name and os.environ.get("ARP_CRASH_ONCE_FILE"):
                    marker = os.environ["ARP_CRASH_ONCE_FILE"]
                    if not os.path.exists(marker):
                        open(marker, "w").close()
                        os._exit(17)
                self.store.append(
                    run_id,
                    "step_completed",
                    step.name,
                    {"output": out, "attempt": attempt, "elapsed_s": time.perf_counter() - t0},
                )
                completed[step.name] = out
                ctx.outputs[step.name] = out
                self.store.acquire_lease(run_id, self.owner, self.lease_s)  # renew
                self._persist_usage(ctx)
                break
        if ctx.rejected:
            return self._finish(ctx, "rejected", "action denied by approver")
        return self._finish(ctx, "succeeded", None)

    async def _compensate(self, ctx: RunContext, wf: Workflow, completed: dict, compensated: set, reason: str) -> None:
        for step in reversed(wf.steps):
            if step.compensate is None or step.name not in completed or step.name in compensated:
                continue
            out = completed[step.name]
            if out.get("skipped") or out.get("rejected"):
                continue
            ctx.current_step = f"{step.name}.compensate"
            try:
                res = await step.compensate(ctx, out)
                self.store.append(ctx.run_id, "compensated", step.name, {"reason": reason, "result": res})
            except Exception as e:  # noqa: BLE001
                self.store.append(ctx.run_id, "compensation_failed", step.name, {"error": _err(e)})
                ctx.flags.append(f"compensation_failed:{step.name}")

    def _persist_usage(self, ctx: RunContext) -> None:
        self.store.update_run(ctx.run_id, usage=ctx.budget.usage(), flags=sorted(set(ctx.flags)))

    def _finish(self, ctx: RunContext, status: str, error: str | None) -> str:
        self.store.append(ctx.run_id, "run_finished", payload={"status": status, "error": error})
        self.store.update_run(
            ctx.run_id,
            status=status,
            error=error,
            usage=ctx.budget.usage(),
            flags=sorted(set(ctx.flags)),
            output=ctx.outputs.get("finalize"),
        )
        return status


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:500]
