# Engineering process artifacts

These documents show how the project would be run by a team. The project itself was built by
one person with an AI coding agent; nothing here claims team leadership that did not happen.

## Review checklist (every PR)
- [ ] The change has a test that fails without it (or a reason why not, in the PR).
- [ ] Claims in docs/README match a committed result file or test.
- [ ] Security-sensitive paths have negative tests (auth, tenant, policy, secrets).
- [ ] New config keys are validated (Pydantic, `extra="forbid"` where applicable).
- [ ] Errors are classified (retryable vs not) and logged without secrets.
- [ ] Benchmarks: same inputs, same hardware note, manifest written; CPU vs GPU labelled.
- [ ] `ruff check`, `ruff format --check`, `pytest -q` green locally.

## Release policy
* Semantic versions; `main` is always releasable; release notes list behaviour changes and
  any change in reported metrics with the result file that shows it.
* A model/workflow version is promoted only through its quality gate (eval suite / dev metrics),
  never by editing a pointer by hand without a recorded reason.

## Rollback plan
* Code: redeploy the previous image tag; DB migrations are additive (new columns nullable), so
  the previous version runs against the new schema.
* Models: re-point the registry alias (`champion`) or workflow version routing to the previous
  version; runs/cases record the version they used, so impact is queryable.

## Definition of done
Feature works end to end, tests and docs updated, result files regenerated if affected,
limits section updated if a new limitation was discovered.

## Roadmap and issue decomposition
| # | issue | size | depends on |
|---|---|---|---|
| 1 | PostgreSQL worker pool with `SKIP LOCKED` claims and lease heartbeats | M | - |
| 2 | OTLP exporter to an OpenTelemetry Collector; Grafana Tempo dashboard | S | collector |
| 3 | Policy-as-data (Cedar/OPA) with the current Python rules as tests | M | - |
| 4 | Outbox pattern for notifications | S | 1 |
| 5 | Remote MCP servers (streamable HTTP) with per-principal OAuth tokens | M | IdP |
| 6 | Grow the scenario suite with real anonymized tickets; canary routing between versions | L | data |
