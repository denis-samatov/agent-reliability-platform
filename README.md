# agent-reliability-platform

**Run LLM agents against enterprise tools so that crashes, retries, prompt injection,
budgets and human approvals are handled by the platform - and prove it with a
failure-injection suite.**

| | |
|---|---|
| **Problem** | Agents connected to company systems fail half-way, duplicate side effects on retry, get redirected by text in tickets or wiki pages, and cannot be compared across prompt versions. |
| **For** | Enterprise AI / platform teams (e.g. a cloud or IT-operations group running many agents). |
| **What** | A durable workflow engine (event log, leases, replay), MCP tool servers built per principal (tenant isolation, scopes), a policy engine and redaction, human approvals, budgets, compensation, OpenTelemetry traces, a dashboard, and an evaluation suite of 21 scenarios run against four workflow versions. |
| **Reuses** | [`localdesk`](https://github.com/denis-samatov/localdesk) (calibrated ticket classifier, from PyPI) as a tool; [`yandex-workspace-mcp`](https://github.com/denis-samatov/yandex-workspace-mcp) as an external read-only Wiki server; [`mcp-capguard`](https://github.com/denis-samatov/mcp-capguard) to test the tool-exposure boundary per principal; MCP Python SDK 2.3. |
| **Status** | 34 tests (unit, integration, a real worker-process kill, API tenant isolation). LLM versions use a local Qwen3-1.7B on CPU; see [results/EVAL.md](results/EVAL.md). |

## Run it

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest -q                                   # 34 tests, no LLM needed
export ARP_API_KEYS="acme-req:acme:requester,acme-appr:acme:approver"
arp serve                                   # dashboard at http://127.0.0.1:8095 (paste a key)
# Optional LLM planner: llama.cpp server with tool-call templates
llama-server -m qwen3-1.7b-q4_k_m.gguf --port 8081 --jinja --reasoning off &
ARP_LLM_BASE_URL=http://127.0.0.1:8081/v1 arp eval --versions v0-rules,v1-llm,v2-llm,v3-llm
```

![Dashboard: a blocked injection attempt with its trace, event log, pending approval and version comparison](docs/img/dashboard.png)

Containers: `ARP_API_KEYS=... docker compose up` (PostgreSQL event store + API/dashboard;
set `ARP_LLM_BASE_URL` for the LLM planners). Verified here with the rules workflow: approval
flow end to end, a requester-role key cannot approve (403), cross-tenant read 404,
events stored in PostgreSQL 17, read-only root FS, non-root user.

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
* **API limits**: per-key token bucket (`ARP_RATE_LIMIT_PER_MIN`, 429 + `Retry-After`), probes exempt.
* **Security**: the model proposes, the policy engine decides (`src/arp/security/guards.py`);
  tools never take a tenant argument; per-step tool allowlists; secrets redacted before the
  model sees tool output. Threat model: [docs/SECURITY.md](docs/SECURITY.md).
* **External MCP servers**: with `ARP_KB_BACKEND=yandex-wiki` the workflow reads runbooks from
  [`yandex-workspace-mcp`](https://github.com/denis-samatov/yandex-workspace-mcp) running as a
  separate process (stdio, minimal environment, synthetic Wiki). Only two read tools are routed;
  the same policy, screening and tracing apply ([ADR-A5](docs/adr/ADR-A5-external-mcp-yandex-workspace.md)).
* **Observability**: one OpenTelemetry trace per run across pauses/resumes; GenAI semantic
  convention attributes on LLM spans; per-run token/tool/cost usage; Prometheus-style `/metrics`.

## Evaluation

`arp eval` runs each scenario on a fresh database with injected faults and scripted human
decisions, then checks the final status, the executed action, and the ITSM database for
required and forbidden effects. 21 scenarios (20 for the rule planner), local Qwen3-1.7B on
CPU for the LLM versions. Full table and per-scenario outcomes: [results/EVAL.md](results/EVAL.md).

| metric | v0 rules | v1 LLM (short prompt) | v2 LLM (procedure prompt) | v3 = v2 + proposal validation |
|---|---:|---:|---:|---:|
| task completion | **0.85** | 0.76 | 0.52 | 0.57 |
| action accuracy | **0.77** | 0.62 | 0.46 | 0.62 |
| unauthorized-action prevention | **1.00** | **1.00** | **1.00** | **1.00** |
| recovery success (failure + budget scenarios) | 1.00 | 1.00 | 0.71 | 0.71 |
| tool-selection recall | 0.41 | 0.23 | 0.45 | 0.45 |
| latency p50 / p95 (executor-active, s) | 0.17 / 0.40 | 12.9 / 23.6 | 15.9 / 29.7 | 17.4 / 23.5 |
| tokens (whole suite) | 0 | 70.5k | 80.3k | 89.9k |

Every scenario's trace (span tree) and key events: [results/TRACES.md](results/TRACES.md); raw
spans in `results/traces/`. The suite was run twice on the same model (temperature 0): 2 of 84
scenario outcomes flipped (both v2: S08, S11), aggregates were unchanged - local CPU inference
is not bit-deterministic, which is one more reason to read single scenarios with care.

What this shows (n = 21 per version, so differences of one or two scenarios are noise):

* **The platform guarantees held for every planner**: no unauthorized action was executed in
  any version, including when the 1.7B model was redirected by an injected ticket (S11, v3 in the
  reported run: it proposed access to a different folder "and the admins group"; the policy
  allowed the grant only with approval, and the human gate stopped it).
* **The small model is the bottleneck, not the workflow.** The procedure prompt (v2) doubled
  tool-selection recall but lowered completion: the model more often ended with an
  incomplete `propose_action` (no `user_id`/`folder`), which the policy correctly refused.
  v3 sends incomplete proposals back as a tool error; that cut such proposals from 8 to 2 of
  21 scenarios - when it fails, the model repeats the same incomplete call.
* "Recovery success" drops for v2/v3 because the planner never reached the faulty step
  (its proposal was refused earlier), not because recovery failed: S14-S18 recovery paths
  are exercised by v0/v1 and by `tests/test_recovery.py`.
* v3 was designed after looking at v2's failures on this same suite, so its numbers are not a
  held-out estimate. No zero-error claim follows from any of these runs.

## Bugs found while building it

* MCP in-memory calls ignored timeouts for synchronous tools (both `anyio.fail_after` and the
  SDK request timeout). Tool bodies now run in abandon-on-cancel threads; safe because writes
  are idempotent ([ADR-A4](docs/adr/ADR-A4-timeouts-and-abandoned-threads.md)).
* Opening the MCP client inside a step's cancel scope broke structured concurrency
  ("exit a cancel scope that isn't the current task's"); the tool session now spans the run.
* The dashboard's version-comparison panel stayed empty: the element id `eval` resolved to
  the global `window.eval` function, so `eval.innerHTML` wrote to the function. Renamed.
* The `create_ticket` queue pattern rejected `o365`, failing every mailbox ticket (scenario S09).
* The policy reported an LLM proposal with no `user_id` as "access can only be requested for
  oneself"; it now says "invalid action: missing user_id", which made the v2 failure mode visible.

## Documentation

[PRD](docs/PRD.md) · [Security](docs/SECURITY.md) · [Runbook](docs/RUNBOOK.md) · [ADRs](docs/adr/)

## Limits

* Synthetic ITSM backend (SQLite), not a real IdP/ITSM; 21 scenarios are a regression
  suite, not a reliability estimate. No zero-error claim follows from them.
* Injection screening is heuristic; the guarantees come from the policy and capability layers.
* Single-machine deployment (Compose); Kubernetes manifests are not provided (not verifiable here).
