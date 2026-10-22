"""yandex-workspace-mcp attached as an external MCP server (separate process, stdio)."""

from __future__ import annotations

import json

import anyio
import pytest

pytest.importorskip("yandex_workspace_mcp", reason="install the 'workspace' extra")

from arp.engine.gateway import minimal_env  # noqa: E402
from arp.platform import Platform, PlatformConfig  # noqa: E402


@pytest.fixture()
def p(tmp_path):
    return Platform(
        PlatformConfig(
            db_url=f"sqlite:///{tmp_path}/r.db",
            itsm_path=str(tmp_path / "i.db"),
            trace_dir=tmp_path / "t",
            versions=("v0-rules",),
            kb_backend="yandex-wiki",
        )
    )


def _tool_events(p, rid):
    return [
        json.loads(e.payload) if isinstance(e.payload, str) else e.payload
        for e in p.store.events(rid)
        if e.type == "tool_call"
    ]


def test_child_environment_withholds_platform_secrets(monkeypatch):
    monkeypatch.setenv("ARP_API_KEYS", "acme-req:acme:requester")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("ARP_DATABASE_URL", "postgresql://u:p@db/arp")
    env = minimal_env()
    assert not {"ARP_API_KEYS", "AWS_SECRET_ACCESS_KEY", "ARP_DATABASE_URL"} & set(env)
    assert "PATH" in env


def test_rule_workflow_cites_a_runbook_from_the_wiki_server(p):
    rid = p.submit("acme", "u102", "My VPN keeps disconnecting every few minutes.")
    assert anyio.run(p.advance, rid) == "succeeded"
    calls = {e["tool"]: e for e in _tool_events(p, rid)}
    assert calls["wiki_search"]["ok"] and calls["wiki_get_page"]["ok"]
    out = p.store.get_run(rid).output
    assert out["executed"]["queue"] == "network"
    assert "runbook it/kb-004: VPN problems" in out["proposed"]["rationale"]


def test_only_exposed_remote_tools_are_reachable(p):
    """The server offers 11 read tools; the workflow sees exactly the two routed ones."""

    async def schemas():
        ctx = p._context(p.store.get_run(rid), p.workflows["it-ticket@v0-rules"])
        async with ctx.tools.session():
            return await ctx.tools.schemas()

    rid = p.submit("acme", "u102", "hello")
    names = set(anyio.run(schemas))
    assert {"wiki_search", "wiki_get_page"} <= names
    assert not {"fetch", "search", "wiki_get_tree", "wiki_get_descendants"} & names


def test_injected_wiki_page_is_flagged_and_cannot_cause_a_grant(p):
    """The community FAQ page (kb-007) carries an injection; it is data, never instructions."""
    from arp.agent import planners

    orig = planners.RulePlanner._attach_runbook

    async def read_faq(self, ctx, ticket, plan):
        await ctx.tools.call("wiki_get_page", {"slug": "it/kb-007"})

    planners.RulePlanner._attach_runbook = read_faq
    try:
        rid = p.submit("acme", "u101", "Please give me access to hr-payroll.")
        anyio.run(p.advance, rid)
    finally:
        planners.RulePlanner._attach_runbook = orig
    flags = [e for e in p.store.events(rid) if e.type == "flag"]
    assert any("injection_in_tool_output" in str(e.payload) for e in flags)
    assert p.itsm.count("access") == 0


def test_path_policy_of_the_wiki_server_is_enforced(p):
    """Pages outside the allowed Wiki root are refused by yandex-workspace-mcp itself."""

    async def outside():
        ctx = p._context(p.store.get_run(rid), p.workflows["it-ticket@v0-rules"])
        ctx.current_step = "plan"
        async with ctx.tools.session():
            try:
                await ctx.tools.call("wiki_get_page", {"slug": "hr/salaries"})
            except Exception as e:  # noqa: BLE001
                return type(e).__name__, str(e)
        return None

    rid = p.submit("acme", "u102", "hello")
    kind, msg = anyio.run(outside)
    assert kind == "ToolError" and "wiki_get_page" in msg
    assert _tool_events(p, rid)[-1]["ok"] is False  # the refusal is in the event log
