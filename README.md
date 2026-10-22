# agent-reliability-platform

**Run LLM agents against enterprise tools so that crashes, retries, prompt injection,
budgets and human approvals are handled by the platform - and prove it with a
failure-injection suite.**

| | |
|---|---|
| **Problem** | Agents connected to company systems fail half-way, duplicate side effects on retry, get redirected by text in tickets or wiki pages, and cannot be compared across prompt versions. |
| **For** | Enterprise AI / platform teams (e.g. a cloud or IT-operations group running many agents). |
| **What** | A durable workflow engine (event log, leases, replay), MCP tool servers built per principal (tenant isolation, scopes), a policy engine and redaction, human approvals, budgets, compensation, OpenTelemetry traces, a dashboard, and an evaluation suite of 21 scenarios run against three workflow versions. |
| **Reuses** | [`localdesk`](https://github.com/denis-samatov/localdesk) (calibrated ticket classifier, from PyPI) as a tool; [`mcp-capguard`](https://github.com/denis-samatov/mcp-capguard) to test the tool-exposure boundary per principal; MCP Python SDK 2.3. |
| **Status** | 27 tests (unit, integration, a real worker-process kill, API tenant isolation). LLM versions use a local Qwen3-1.7B on CPU; see [results/EVAL.md](results/EVAL.md). |

## Run it

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest -q                                   # 27 tests, no LLM needed
export ARP_API_KEYS="acme-req:acme:requester,acme-appr:acme:approver"
arp serve                                   # dashboard at http://127.0.0.1:8095 (paste a key)
# Optional LLM planner: llama.cpp server with tool-call templates
llama-server -m qwen3-1.7b-q4_k_m.gguf --port 8081 --jinja --reasoning off &
ARP_LLM_BASE_URL=http://127.0.0.1:8081/v1 arp eval --versions v0-rules,v1-llm,v2-llm
```

## Workflow

```
screen -> classify (LocalDesk via MCP) -> plan -> policy -> [approval] -> execute -> verify -> notify -> finalize
                                           |        |           |            |          |
                         rules | LLM agent |   deny -> security  waits for   idempotent  compensate
                         with read tools   |   escalation ticket a human     effects     (revoke) on
                                                                                         failure/cancel
```

* **Durability**: every step writes `step_started` / `step_completed` (with output) to an
  append-only log. A resumed run replays completed steps; a step interrupted after its side
  effect re-executes with the same idempotency key, so the tool returns the stored result.
  `tests/test_recovery.py` kills the worker process (`os._exit`) right after the effect and
  checks that a second process finishes the run with exactly one ticket created.
* **Security**: the model proposes, the policy engine decides (`src/arp/security/guards.py`);
  tools never take a tenant argument; per-step tool allowlists; secrets redacted before the
  model sees tool output. Threat model: [docs/SECURITY.md](docs/SECURITY.md).
* **Observability**: one OpenTelemetry trace per run across pauses/resumes; GenAI semantic
  convention attributes on LLM spans; per-run token/tool/cost usage; Prometheus-style `/metrics`.

## Evaluation

`arp eval` runs each scenario on a fresh database with injected faults and scripted human
decisions, then checks the final status, the executed action, and the ITSM database for
required and forbidden effects. Baseline (rule planner, no LLM, 20 scenarios):

| metric | v0-rules |
|---|---:|
| task completion | 0.85 (17/20) |
| unauthorized-action prevention (security scenarios) | 1.00 (5/5) |
| recovery success (failure + budget scenarios) | 1.00 (6/6) |
| latency p50 / p95 (executor-active) | 0.73 s / 1.14 s |

LLM planner results (v1, v2) and per-scenario outcomes: [results/EVAL.md](results/EVAL.md).

## Bugs found while building it

* MCP in-memory calls ignored timeouts for synchronous tools (both `anyio.fail_after` and the
  SDK request timeout). Tool bodies now run in abandon-on-cancel threads; safe because writes
  are idempotent ([ADR-A4](docs/adr/ADR-A4-timeouts-and-abandoned-threads.md)).
* Opening the MCP client inside a step's cancel scope broke structured concurrency
  ("exit a cancel scope that isn't the current task's"); the tool session now spans the run.
* The `create_ticket` queue pattern rejected `o365`, failing every mailbox ticket (scenario S09).

## Documentation

[PRD](docs/PRD.md) · [Security](docs/SECURITY.md) · [Runbook](docs/RUNBOOK.md) · [ADRs](docs/adr/)

## Limits

* Synthetic ITSM backend (SQLite), not a real IdP/ITSM; 21 scenarios are a regression
  suite, not a reliability estimate. No zero-error claim follows from them.
* Injection screening is heuristic; the guarantees come from the policy and capability layers.
* Single-machine deployment; Kubernetes manifests are not provided (not verifiable here).
