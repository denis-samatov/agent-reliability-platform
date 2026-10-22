"""HTTP API and execution dashboard.

Authentication: ``X-API-Key`` mapped to a tenant and a role (``ARP_API_KEYS`` =
``key:tenant:role,...``; roles ``requester`` / ``approver``). Every query is filtered by the
key's tenant, so another tenant's runs and approvals are indistinguishable from "not found".
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from arp.app.ratelimit import EXEMPT_PREFIXES, TokenBucketLimiter, client_id, retry_after
from arp.telemetry import load_trace

DASHBOARD = Path(__file__).with_name("dashboard.html")


class Ticket(BaseModel):
    requester: str = Field(pattern=r"^u\d{3}$")
    ticket: str = Field(min_length=3, max_length=4000)
    version: str = "v0-rules"


class Decision(BaseModel):
    approve: bool
    reason: str = Field("", max_length=500)


def _keys() -> dict[str, tuple[str, str]]:
    raw = os.environ.get("ARP_API_KEYS", "")
    out = {}
    for item in filter(None, raw.split(",")):
        key, tenant, role = item.split(":")
        out[key] = (tenant, role)
    return out


def create_app(
    platform,
    keys: dict[str, tuple[str, str]] | None = None,
    results_dir: Path = Path("results"),
    limiter: TokenBucketLimiter | None = None,
) -> FastAPI:
    app = FastAPI(title="Agent Reliability Platform", version="0.1.0")
    limiter = limiter or TokenBucketLimiter(int(os.environ.get("ARP_RATE_LIMIT_PER_MIN", "120")))
    keys = keys if keys is not None else _keys()
    counters: dict[str, int] = {}

    def principal(request: Request) -> tuple[str, str]:
        given = request.headers.get("x-api-key", "")
        for k, v in keys.items():
            if hmac.compare_digest(given.encode(), k.encode()):
                return v
        raise HTTPException(401, "invalid API key")

    def own_run(run_id: str, tenant: str):
        run = platform.store.get_run(run_id)
        if run is None or run.tenant != tenant:
            raise HTTPException(404, "run not found")
        return run

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        if request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)
        host = request.client.host if request.client else None
        wait = limiter.take(client_id(request.headers.get("x-api-key"), host))
        if wait:
            return JSONResponse(
                {"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": retry_after(wait)}
            )
        return await call_next(request)

    @app.middleware("http")
    async def count(request: Request, call_next):
        resp = await call_next(request)
        k = f"{request.method} {getattr(request.scope.get('route'), 'path', '?')} {resp.status_code}"
        counters[k] = counters.get(k, 0) + 1
        return resp

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        return {"status": "ready", "workflows": sorted(platform.workflows)}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        runs = platform.store.list_runs(10000)
        by = {}
        for r in runs:
            by[r.status] = by.get(r.status, 0) + 1
        lines = [f'arp_runs{{status="{s}"}} {n}' for s, n in sorted(by.items())]
        lines += [f'arp_http_requests_total{{route="{k}"}} {v}' for k, v in sorted(counters.items())]
        return "\n".join(lines) + "\n"

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD.read_text(encoding="utf-8")

    @app.post("/v1/runs", status_code=202)
    def submit(t: Ticket, who=Depends(principal)):
        tenant, _ = who
        if f"it-ticket@{t.version}" not in platform.workflows:
            raise HTTPException(422, f"unknown workflow version {t.version}")
        return {"run_id": platform.submit(tenant, t.requester, t.ticket, t.version)}

    @app.post("/v1/runs/{run_id}/advance")
    async def advance(run_id: str, who=Depends(principal)):
        own_run(run_id, who[0])
        return {"status": await platform.advance(run_id)}

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel(run_id: str, who=Depends(principal)):
        run = own_run(run_id, who[0])
        platform.store.update_run(
            run_id, cancel_requested=True, status="pending" if run.status == "waiting_approval" else run.status
        )
        return {"cancel_requested": True}

    @app.get("/v1/runs")
    def runs(who=Depends(principal)):
        return [
            {
                "run_id": r.id,
                "version": r.version,
                "status": r.status,
                "requester": r.requester,
                "ticket": r.input["ticket"][:120],
                "usage": r.usage,
                "flags": r.flags,
                "created_at": r.created_at.isoformat(),
            }
            for r in platform.store.list_runs(200, who[0])
        ]

    @app.get("/v1/runs/{run_id}")
    def run_detail(run_id: str, who=Depends(principal)):
        r = own_run(run_id, who[0])
        return {
            "run_id": r.id,
            "status": r.status,
            "version": r.version,
            "version_hash": r.version_hash,
            "input": r.input,
            "output": r.output,
            "usage": r.usage,
            "flags": r.flags,
            "error": r.error,
            "events": [
                {"seq": e.seq, "type": e.type, "step": e.step, "payload": e.payload, "ts": e.ts.isoformat()}
                for e in platform.store.events(run_id)
            ],
            "trace": load_trace(platform.cfg.trace_dir, run_id),
        }

    @app.get("/v1/approvals")
    def approvals(who=Depends(principal)):
        return [
            {"approval_id": a.id, "run_id": a.run_id, "action": a.action, "created_at": a.created_at.isoformat()}
            for a in platform.store.pending_approvals(who[0])
        ]

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(approval_id: int, d: Decision, who=Depends(principal)):
        tenant, role = who
        if role != "approver":
            raise HTTPException(403, "approver role required")
        try:
            a = platform.store.decide(approval_id, tenant, d.approve, f"human:{tenant}-approver", d.reason)
        except KeyError as e:
            raise HTTPException(404, "approval not found") from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        status = await platform.advance(a.run_id)
        return {"run_id": a.run_id, "status": status}

    @app.get("/v1/eval")
    def eval_results(_=Depends(principal)):
        p = results_dir / "eval.json"
        if not p.exists():
            raise HTTPException(404, "no evaluation results")
        data = json.loads(p.read_text())
        return {v: d["summary"] for v, d in data["versions"].items()}

    return app
