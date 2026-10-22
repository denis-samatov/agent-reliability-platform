# ADR-A1: A small durable engine on an event log instead of LangGraph or Temporal

Status: accepted (2026-10-22)

## Context
Needed: step-level durability, exactly-once *effects* via idempotency keys, human approval
pauses that survive process restarts, compensation, per-step tool allowlists, budgets and
tenant concurrency limits - all testable on one machine without extra servers.

## Options
| option | pros | cons |
|---|---|---|
| LangGraph 1.2.x + Postgres checkpointer | mature graph model, interrupts for HITL, large ecosystem | durability is per super-step; idempotency of tool side effects, compensation and per-step tool policy would still be our code; the framework hides what we need to demonstrate |
| Temporal (temporalio 1.34) | industry standard durable execution, retries, signals for approvals | requires a Temporal cluster; workflow determinism constraints; heavy for a single-repo demo |
| Own engine (~400 LOC) on SQLAlchemy | every guarantee is visible and tested; runs on SQLite or PostgreSQL | must be maintained; no visual graph editor; fewer integrations |

## Decision
Own engine. The design maps 1:1 to Temporal concepts (event history, activities with retry
policies, signals = approvals, sagas = compensation), so migrating later is a re-hosting
exercise, documented in `docs/ARCHITECTURE.md`.
