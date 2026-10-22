# Product requirements: Agent Reliability Platform

## Audience
Enterprise AI / platform teams that run LLM agents against internal systems (service desk,
cloud operations, HR tools) and must answer: *what did the agent do, was it allowed, what
did it cost, and what happens when something fails half-way?*

## Problem
Agent demos call tools in a loop inside one process. In production that loop crashes,
retries duplicate side effects, a prompt in a ticket or a wiki page redirects the agent,
one tenant's agent sees another tenant's data, and nobody can compare two prompt versions
except by anecdote.

## Workflow (reference scenario: IT ticket)
`screen -> classify (LocalDesk) -> plan (rules | LLM agent with tools) -> policy -> human approval
-> execute (idempotent, compensable) -> verify -> notify -> finalize`, executed by a durable
engine over an event log, with tools served by MCP servers built per principal.

## Requirements and how they are met
| Requirement | Mechanism | Evidence |
|---|---|---|
| Multi-step orchestration | `Workflow` of `Step`s, versioned with a content hash | `src/arp/workflows.py` |
| Durable state, checkpoints, recovery | append-only event log; replay; leases | `tests/test_recovery.py` (real process kill) |
| Retries, timeouts, idempotency | per-step retry policy, SDK request timeouts, idempotency keys | `tests/test_engine.py` |
| Cancellation, compensation | cancel flag checked between steps; reverse-order compensation | `tests/test_engine.py` |
| Human approval | `waiting_approval` state, approvals inbox, approver role | `tests/test_api.py` |
| Tenant isolation, scoped tools | server built per principal; no tenant argument; tenant-scoped API keys | `tests/test_capguard.py`, `tests/test_security.py`, `tests/test_api.py` |
| Budgets (tokens, time, tool calls, cost), concurrency | `Budget`; tenant running-runs limit | `tests/test_engine.py` |
| Structured outputs and schema validation | MCP input schemas (Pydantic), JSON tool-call args, action schema | `src/arp/tools/server.py`, `src/arp/agent/planners.py` |
| Prompt-injection / unauthorized-action / leak defenses | policy engine, step tool allowlists, redaction, screening | `tests/test_security.py`, eval S04/S06/S11/S12/S13 |
| Tracing and dashboard | OpenTelemetry spans (GenAI conventions) -> JSONL -> dashboard waterfall | `src/arp/telemetry.py`, `src/arp/app/dashboard.html` |
| Version comparison and evaluation | scenario suite x versions -> `results/EVAL.md` | `src/arp/eval/` |

## Out of scope (stated, not hidden)
Multi-node deployment, real ITSM/IdP integrations, A2A between independent agents
(ADR-A2), Kubernetes manifests (not verifiable here).
