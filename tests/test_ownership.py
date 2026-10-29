"""Executor ownership regressions without an MCP server or external services."""

from contextlib import asynccontextmanager, nullcontext

import anyio

from arp.engine.budget import Budget
from arp.engine.executor import Executor, RunContext, Step, Workflow
from arp.engine.store import Store


class Tracer:
    def run_span(self, *args):
        return nullcontext(self)

    def step_span(self, *args):
        return nullcontext(self)

    def tool_span(self, *args):
        return nullcontext(self)

    def set_attribute(self, *args):
        pass


class Tools:
    @asynccontextmanager
    async def session(self):
        yield self


def setup(tmp_path, fn, lease_s=1):
    store = Store(f"sqlite:///{tmp_path}/runs.db")
    wf = Workflow("test", "v1", [Step("finalize", fn)])
    run = store.create_run(
        tenant="acme",
        requester="u001",
        workflow="test",
        version="v1",
        version_hash=wf.version_hash,
        input={},
        budget={},
        usage={},
    )

    def context(r, workflow):
        return RunContext(r.id, r.tenant, r.requester, r.input, store, Budget(), Tools(), None, Tracer())

    return store, run.id, Executor(store, {"test@v1": wf}, context, "same-process", lease_s=lease_s)


def test_overlapping_advances_in_same_process_execute_step_once(tmp_path):
    async def scenario():
        entered, finish = anyio.Event(), anyio.Event()
        calls, results = [], []

        async def step(ctx):
            calls.append(ctx.run_id)
            entered.set()
            await finish.wait()
            return {"ok": True}

        store, rid, engine = setup(tmp_path, step)

        async def first():
            results.append(await engine.run(rid))

        async with anyio.create_task_group() as group:
            group.start_soon(first)
            await entered.wait()
            # Release both invocations even on the pre-fix implementation.
            group.start_soon(release, finish)
            results.append(await engine.run(rid))
        assert calls == [rid]
        assert sorted(results) == ["locked", "succeeded"]
        assert sum(e.type == "step_completed" for e in store.events(rid)) == 1

    anyio.run(scenario)


async def release(event):
    await anyio.sleep(0.02)
    event.set()


def test_heartbeat_keeps_a_long_step_owned(tmp_path):
    async def scenario():
        entered, finish = anyio.Event(), anyio.Event()

        async def step(ctx):
            entered.set()
            await finish.wait()
            return {}

        store, rid, engine = setup(tmp_path, step, lease_s=0.12)
        results = []

        async def first():
            results.append(await engine.run(rid))

        async with anyio.create_task_group() as group:
            group.start_soon(first)
            await entered.wait()
            await anyio.sleep(0.3)
            assert not store.acquire_lease(rid, "other-process", 1)
            finish.set()
        assert results == ["succeeded"]

    anyio.run(scenario)


def test_lost_owner_cannot_commit_a_completed_step(tmp_path):
    async def scenario():
        async def step(ctx):
            # Simulate an expired lease already reclaimed by another invocation.
            store.update_run(ctx.run_id, lease_owner="new-owner")
            return {"stale": True}

        store, rid, engine = setup(tmp_path, step)
        assert await engine.run(rid) == "locked"
        assert not any(e.type in {"step_completed", "run_finished"} for e in store.events(rid))
        assert store.get_run(rid).lease_owner == "new-owner"

    anyio.run(scenario)


def test_resume_blocks_changed_workflow_before_executing(tmp_path):
    calls = []

    async def step(ctx):
        calls.append(ctx.run_id)
        return {}

    store, rid, engine = setup(tmp_path, step)
    engine.workflows["test@v1"].config["policy"] = "changed"
    assert anyio.run(engine.run, rid) == "blocked"
    assert not calls
    assert store.get_run(rid).status == "blocked"


def test_decision_before_parking_does_not_strand_run(tmp_path):
    from arp.engine.executor import WaitForApproval

    async def step(ctx):
        approval = ctx.store.approval(ctx.run_id, "finalize")
        if approval and approval.status != "pending":
            return {"approved": True}
        aid = ctx.store.request_approval(ctx.run_id, ctx.tenant, "finalize", {})
        store.decide(aid, ctx.tenant, True, "human")
        raise WaitForApproval(aid)

    store, rid, engine = setup(tmp_path, step)
    assert anyio.run(engine.run, rid) == "pending"
    assert store.pending_approvals() == []
    assert anyio.run(engine.run, rid) == "succeeded"


def test_gateway_does_not_dispatch_after_ownership_takeover(tmp_path):
    from types import SimpleNamespace

    import pytest

    from arp.engine.gateway import ToolGateway
    from arp.engine.store import FencedStore, LeaseLost

    async def scenario():
        async def step(ctx):
            return {}

        store, rid, _ = setup(tmp_path, step)
        assert store.acquire_lease(rid, "old-owner", 30)
        ctx = RunContext(rid, "acme", "u", {}, FencedStore(store, "old-owner"), Budget(), None, None, Tracer())
        ctx.current_step = "finalize"
        calls = []

        class Client:
            async def call_tool(self, name, args, **kwargs):
                calls.append(name)
                return SimpleNamespace(is_error=False, structured_content={}, content=[])

        gateway = ToolGateway(None, lambda: ctx, store, Tracer())
        gateway._client = Client()
        store.update_run(rid, lease_owner="replacement")
        with pytest.raises(LeaseLost):
            await gateway.call("create_ticket", {})
        assert calls == []

    anyio.run(scenario)


def test_dependency_upgrade_during_approval_pause_blocks_resume(tmp_path, monkeypatch):
    from importlib import metadata

    from arp.engine.executor import WaitForApproval

    effects = []

    async def step(ctx):
        approval = ctx.store.approval(ctx.run_id, "finalize")
        if approval is not None and approval.status == "approved":
            effects.append(ctx.run_id)
            return {}
        raise WaitForApproval(ctx.store.request_approval(ctx.run_id, ctx.tenant, "finalize", {}))

    store, rid, engine = setup(tmp_path, step)
    assert anyio.run(engine.run, rid) == "waiting_approval"
    version = metadata.version
    monkeypatch.setattr(metadata, "version", lambda name: "test-upgrade" if name == "mcp" else version(name))
    approval = store.approval(rid, "finalize")
    store.decide(approval.id, "acme", True, "human")
    assert anyio.run(engine.run, rid) == "blocked"
    assert effects == []


def test_restored_runtime_identity_can_resume_approval_pause(tmp_path, monkeypatch):
    from importlib import metadata

    from arp.engine.executor import WaitForApproval

    effects = []

    async def step(ctx):
        approval = ctx.store.approval(ctx.run_id, "finalize")
        if approval is not None and approval.status == "approved":
            effects.append(ctx.run_id)
            return {}
        raise WaitForApproval(ctx.store.request_approval(ctx.run_id, ctx.tenant, "finalize", {}))

    store, rid, engine = setup(tmp_path, step)
    workflow = engine.workflows["test@v1"]
    assert anyio.run(engine.run, rid) == "waiting_approval"
    recorded = store.get_run(rid).version_hash
    version = metadata.version
    with monkeypatch.context() as patch:
        patch.setattr(metadata, "version", lambda name: "test-upgrade" if name == "mcp" else version(name))
        assert workflow.version_hash != recorded
    assert workflow.version_hash == recorded
    store.decide(store.approval(rid, "finalize").id, "acme", True, "human")
    assert anyio.run(engine.run, rid) == "succeeded"
    assert effects == [rid]


def test_python_version_and_missing_distribution_change_runtime_hash(tmp_path, monkeypatch):
    import sys
    from importlib import metadata

    async def step(ctx):
        return {}

    _, _, engine = setup(tmp_path, step)
    workflow = engine.workflows["test@v1"]
    recorded = workflow.version_hash
    with monkeypatch.context() as patch:
        patch.setattr(sys, "version", sys.version + "-test-upgrade")
        assert workflow.version_hash != recorded
    version = metadata.version

    def missing(name):
        if name == "mcp":
            raise metadata.PackageNotFoundError(name)
        return version(name)

    with monkeypatch.context() as patch:
        patch.setattr(metadata, "version", missing)
        first = workflow.version_hash
        assert first == workflow.version_hash
        assert first != recorded
    assert workflow.version_hash == recorded


def test_runtime_metadata_scan_does_not_consume_execution_lease(tmp_path, monkeypatch):
    import time
    from importlib import metadata

    async def step(ctx):
        return {}

    store, rid, engine = setup(tmp_path, step, lease_s=0.12)
    version = metadata.version

    def slow_scan(name):
        if name == "mcp":
            time.sleep(0.15)  # metadata on a cold filesystem may outlast a short lease
        return version(name)

    monkeypatch.setattr(metadata, "version", slow_scan)
    assert anyio.run(engine.run, rid) == "succeeded"
    assert store.get_run(rid).status == "succeeded"


def test_runtime_scan_does_not_block_other_invocations_heartbeat(tmp_path, monkeypatch):
    import threading
    from importlib import metadata

    async def step(ctx):
        return {}

    _, rid, engine = setup(tmp_path, step)
    started, release_scan = threading.Event(), threading.Event()
    yielded_to_loop = []
    version = metadata.version

    def slow_scan(name):
        if name == "mcp":
            started.set()
            yielded_to_loop.append(release_scan.wait(timeout=1))
        return version(name)

    monkeypatch.setattr(metadata, "version", slow_scan)

    async def scenario():
        async def other_invocation_heartbeat():
            with anyio.fail_after(2):
                while not started.is_set():
                    await anyio.sleep(0.001)
            release_scan.set()

        async with anyio.create_task_group() as group:
            group.start_soon(other_invocation_heartbeat)
            assert await engine.run(rid) == "succeeded"
        assert yielded_to_loop == [True]

    anyio.run(scenario)
