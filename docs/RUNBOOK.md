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
