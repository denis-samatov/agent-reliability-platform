# Runbook

## Start
```bash
export ARP_DATABASE_URL=postgresql+psycopg://arp:arp@localhost:5432/arp   # or sqlite:///./arp.db
export ARP_API_KEYS="acme-req-key:acme:requester,acme-approver-key:acme:approver"
export ARP_LLM_BASE_URL=http://127.0.0.1:8081/v1     # llama-server with --jinja (optional)
arp serve            # API + dashboard on :8095
arp worker           # advances pending runs
```
Health: `GET /health/live`, `GET /health/ready`; metrics: `GET /metrics` (`arp_runs{status=...}`).

## Symptoms
| symptom | check | action |
|---|---|---|
| runs stuck in `running` | `lease_until` in `runs`; worker logs | a crashed worker's lease expires after `lease_s`; another worker resumes the run |
| many `step_failed` with `TRANSIENT` | event log of a run, dashboard trace | upstream outage: retries back off; if persistent, runs fail and are visible as `failed` |
| `budget_exceeded` spikes | run usage | a prompt/model change increased tokens; compare versions with `arp eval` before rollout |
| `blocked` runs | `tool_blocked` / `policy_violation` events | investigate as a security event; check the planner version |
| approvals pile up | `GET /v1/approvals` | approver capacity; consider lowering risk tiers only after review |
| clients get 429 | request rate per key vs `ARP_RATE_LIMIT_PER_MIN` (default 120) | back off per `Retry-After`; probes (`/health`, `/metrics`) are never limited |
| external Wiki tools fail (`ARP_KB_BACKEND=yandex-wiki`) | `tool_call` events with `wiki_*`, child stderr | the server process is started per run with a minimal environment; check that the `workspace` extra is installed |

## Release and rollback
New workflow versions are registered side by side (`it-ticket@vN`); runs keep the version
they started with (version hash recorded per run). Release = route new tickets to the new
version after `arp eval` shows no regression on the suite; rollback = route back. Runs in
flight are unaffected.

## Ownership and immutable resume acceptance

`locked` means another invocation owns the run, or ownership was lost; retry only after
checking its lease and event trail. Do not clear a live lease to force an advance. A live
heartbeat cannot save a process whose event loop is blocked longer than its lease.

The workflow fingerprint includes the package's Python sources and workflow configuration.
A run whose recorded hash differs is blocked before any workflow step. Resume it using the
original pinned runtime/configuration; do not rewrite hashes to force compatibility.
This protects against silent code changes, but does not replace pinned dependency/model
artifacts. Existing historical hashes are not retroactively migrated.

Before a consequential release, in an explicitly approved staging environment:

1. Pin code, dependency resolution, model and external-tool versions in a release manifest.
2. Run `pytest -q`, including overlapping advances, long-step heartbeat, stale-owner writes,
   process-kill recovery, subject binding and version mismatch checks.
3. Repeat ownership/reclaim on PostgreSQL with two processes and a deliberately long step;
   verify one committed completion and durable idempotent effects.
4. Back up and restore the event database, ITSM effect ledger and traces into an isolated
   target. Confirm approvals, budgets, versions and completed effects agree before resume.
5. Verify readiness becomes 503 when the event DB is unavailable. Observe a defined
   latency/error/recovery budget using the actual authorized IdP/ITSM, not synthetic tools.

Steps 3–5 and third-party idempotency are deployment prerequisites, not claims established
by the offline tests. Do not edit or regenerate historical `results/` to imply new evidence.

Approval parking and human decisions serialize on the run row. A decision accepted
before parking leaves the run pending, so another advance can consume it; it cannot
strand a decided approval in `waiting_approval`. Cancellation changes the current
row conditionally and cannot resurrect a terminal run. The tool gateway rechecks
ownership immediately before dispatch; takeover after that check still requires the
upstream idempotency contract for an already in-flight operation.
