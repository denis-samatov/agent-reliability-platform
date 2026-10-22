"""Tool gateway: the only path from a workflow to tools.

Every call goes through: allowlist check for the calling step -> budget check -> MCP call
with timeout -> error classification (``TRANSIENT:`` -> retryable) -> output parsing ->
secret redaction -> span + event. Tool arguments chosen by a model never include tenant or
idempotency keys; the gateway adds the idempotency key for side-effecting tools.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import anyio
from mcp import Client

from arp.engine.executor import PolicyViolation, TransientError
from arp.security.guards import redact, screen

SIDE_EFFECT_TOOLS = {"grant_folder_access", "revoke_folder_access", "reset_password", "create_ticket", "notify_user"}


class ToolError(RuntimeError):
    pass


class ToolGateway:
    def __init__(
        self, server_factory, ctx_ref, store, tracer, allowlist: dict[str, set[str]] | None = None, timeout_s: float = 10.0
    ):
        self.server_factory = server_factory  # () -> MCPServer built for the run's principal
        self.ctx_ref = ctx_ref  # callable returning the RunContext
        self.store = store
        self.tracer = tracer
        self.allowlist = allowlist or {}
        self.timeout_s = timeout_s
        self._client: Client | None = None
        self._schemas: dict[str, dict] | None = None

    @asynccontextmanager
    async def session(self):
        """Open the MCP session for the whole run. It must be entered *outside* any step's
        cancel scope (``anyio.fail_after``): the client owns a task group that has to be
        closed by the task and scope that opened it."""
        async with Client(self.server_factory()) as client:
            self._client = client
            try:
                yield self
            finally:
                self._client = None

    async def _session(self) -> Client:
        if self._client is None:
            raise RuntimeError("tool session not open: wrap the run in `async with gateway.session()`")
        return self._client

    async def schemas(self) -> dict[str, dict]:
        if self._schemas is None:
            client = await self._session()
            tools = await client.list_tools()
            self._schemas = {t.name: {"description": t.description, "input_schema": t.input_schema} for t in tools.tools}
        return self._schemas

    async def call(self, tool: str, args: dict, *, step: str | None = None, key_suffix: str = "") -> dict:
        ctx = self.ctx_ref()
        step = step or ctx.current_step
        allowed = self.allowlist.get(step.split(".")[0])
        if allowed is not None and tool not in allowed:
            self.store.append(ctx.run_id, "tool_blocked", step, {"tool": tool, "reason": "not allowed in this step"})
            raise PolicyViolation(f"tool {tool!r} is not allowed in step {step!r}")
        ctx.budget.tool_calls += 1
        ctx.budget.check()
        args = dict(args)
        if tool in SIDE_EFFECT_TOOLS:
            args["idempotency_key"] = ctx.idempotency_key(f"{tool}:{key_suffix}")
        client = await self._session()
        t0 = time.perf_counter()
        with self.tracer.tool_span(tool, tool in SIDE_EFFECT_TOOLS) as span:
            try:
                # The SDK's own request timeout is authoritative: an outer cancel scope does not
                # interrupt an in-flight request (observed with mcp 2.3 in-memory transport).
                # The outer scope is a backstop with a small margin.
                with anyio.fail_after(self.timeout_s + 2.0):
                    result = await client.call_tool(tool, args, read_timeout_seconds=self.timeout_s)
            except Exception as e:  # noqa: BLE001 - classify transport-level failures
                if isinstance(e, TimeoutError) or "timed out" in str(e).lower() or "timeout" in type(e).__name__.lower():
                    span.set_attribute("error.type", "timeout")
                    self._event(ctx, step, tool, args, False, time.perf_counter() - t0, "timeout")
                    raise TransientError(f"{tool}: timed out after {self.timeout_s}s") from None
                raise
            latency = time.perf_counter() - t0
            if result.is_error:
                text = result.content[0].text if result.content else "unknown error"
                transient = "TRANSIENT:" in text
                span.set_attribute("error.type", "transient" if transient else "permanent")
                self._event(ctx, step, tool, args, False, latency, redact(text))
                if transient:
                    raise TransientError(text)
                raise ToolError(text)
            data = result.structured_content
            if data is None:
                data = json.loads(result.content[0].text) if result.content else {}
            data = data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
            flags = screen(json.dumps(data, ensure_ascii=False))
            if flags:
                ctx.flags.append("injection_in_tool_output")
                self.store.append(
                    ctx.run_id, "flag", step, {"flag": "injection_in_tool_output", "tool": tool, "indicators": flags}
                )
                span.set_attribute("security.injection_indicators", len(flags))
            safe = redact(data)
            self._event(ctx, step, tool, args, True, latency, None, replayed=isinstance(data, dict) and data.get("replayed"))
            return safe

    def _event(self, ctx, step, tool, args, ok, latency, error, replayed=None):
        payload = {
            "tool": tool,
            "args": redact({k: v for k, v in args.items() if k != "idempotency_key"}),
            "ok": ok,
            "latency_s": round(latency, 4),
        }
        if error:
            payload["error"] = error
        if replayed:
            payload["replayed"] = True
        self.store.append(ctx.run_id, "tool_call", step, payload)
