"""Composition root: wires store, tools, tracing, LLM and workflows into an executor."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

from arp.engine.budget import Budget, Prices
from arp.engine.executor import Executor, RunContext
from arp.engine.gateway import ToolGateway
from arp.engine.store import Store
from arp.telemetry import Tracer, make_provider
from arp.tools.itsm import ITSM, FaultPlan
from arp.tools.kb import KnowledgeBase
from arp.tools.server import READ, WRITE, Principal, build_server, localdesk_classifier
from arp.workflows import STEP_TOOLS, build_workflow

DEFAULT_BUDGET = {"max_tokens": 20000, "max_tool_calls": 30, "max_llm_calls": 12, "max_wall_s": 600.0, "max_cost_usd": 1.0}


@dataclass
class PlatformConfig:
    db_url: str = os.environ.get("ARP_DATABASE_URL", "sqlite:///./arp.db")
    itsm_path: str = os.environ.get("ARP_ITSM_PATH", "./itsm.db")
    trace_dir: Path = Path(os.environ.get("ARP_TRACE_DIR", "./traces"))
    versions: tuple[str, ...] = ("v0-rules", "v1-llm", "v2-llm", "v3-llm")
    llm_base_url: str | None = os.environ.get("ARP_LLM_BASE_URL")
    prices: Prices = field(default_factory=Prices)
    max_running_per_tenant: int = 4
    lease_s: float = 30.0
    tool_timeout_s: float = 10.0


class Platform:
    def __init__(self, cfg: PlatformConfig, faults: FaultPlan | None = None, span_exporter=None, owner: str | None = None):
        self.cfg = cfg
        self.store = Store(cfg.db_url)
        self.itsm = ITSM(cfg.itsm_path, faults)
        self.kb = KnowledgeBase()
        self.classifier = localdesk_classifier()
        self.provider = make_provider(cfg.trace_dir, span_exporter)
        self.llm = None
        fingerprint = None
        if cfg.llm_base_url:
            from arp.agent.llm import ChatLLM, ModelConfig

            mc = ModelConfig(base_url=cfg.llm_base_url)
            self.llm = ChatLLM(mc)
            fingerprint = mc.fingerprint()
        self.workflows = {}
        for v in cfg.versions:
            if v != "v0-rules" and self.llm is None:
                continue
            wf = build_workflow(v, self.llm, fingerprint)
            self.workflows[f"{wf.name}@{wf.version}"] = wf
        self.executor = Executor(
            self.store,
            self.workflows,
            self._context,
            owner or f"{socket.gethostname()}-{os.getpid()}",
            lease_s=cfg.lease_s,
            max_running_per_tenant=cfg.max_running_per_tenant,
        )

    def _context(self, run, wf) -> RunContext:
        principal = Principal(run.tenant, f"agent:{wf.name}", frozenset({READ, WRITE}))
        tracer = Tracer(self.provider, run.id)
        holder: dict = {}
        gw = ToolGateway(
            lambda: build_server(principal, self.itsm, self.kb, self.classifier),
            lambda: holder["ctx"],
            self.store,
            tracer,
            STEP_TOOLS,
            timeout_s=self.cfg.tool_timeout_s,
        )
        ctx = RunContext(
            run.id,
            run.tenant,
            run.requester,
            run.input,
            self.store,
            Budget.from_run(run.budget, run.usage or {}, self.cfg.prices),
            gw,
            self.llm,
            tracer,
        )
        holder["ctx"] = ctx
        return ctx

    def submit(self, tenant: str, requester: str, ticket: str, version: str = "v0-rules", budget: dict | None = None) -> str:
        wf = self.workflows[f"it-ticket@{version}"]
        run = self.store.create_run(
            tenant=tenant,
            requester=requester,
            workflow=wf.name,
            version=wf.version,
            version_hash=wf.version_hash,
            input={"ticket": ticket},
            budget={**DEFAULT_BUDGET, **(budget or {})},
            usage={},
        )
        return run.id

    async def advance(self, run_id: str) -> str:
        return await self.executor.run(run_id)
