# Technical defense notes

**Q: What exactly is "durable" here?**
Every step appends `step_started` and `step_completed(output)` to an append-only event log
in the same database as the run. On resume the executor rebuilds state from the log and
skips completed steps; it never re-runs a completed step (`test_approval_pause_resume_replays_completed_steps`
checks `classify` started once across a pause).

**Q: A worker dies after calling an external system but before recording it. What happens?**
The lease expires, another worker resumes, the step runs again, and the tool receives the
same idempotency key (sha256 of run id + step + tool), so the backend returns the stored
result with `replayed: true`. `tests/test_recovery.py` does this with a real `os._exit` in a
subprocess and checks one ticket, not two. This gives exactly-once *effects*, not
exactly-once *execution* - the honest guarantee.

**Q: Why not LangGraph or Temporal?**
LangGraph checkpoints super-steps but leaves side-effect idempotency, compensation and
per-step tool policy to you; Temporal provides durable execution but needs a cluster and
hides the mechanism. The engine maps 1:1 to Temporal concepts (history, activities, retry
policies, signals, sagas), so migration is re-hosting (ADR-A1).

**Q: How do you stop prompt injection?**
I don't rely on detecting it. The model only proposes; a deterministic policy decides
(restricted folders never, password resets only for self, unknown actions denied). Tools
have no tenant argument; write tools are not available during planning (step allowlist ->
run `blocked`). Screening only raises flags that force approval. Eval S11/S12 contain direct
and indirect (retrieved document) injections; forbidden effects are checked in the database.

**Q: How do you know tenant isolation holds?**
Three layers, each tested: the MCP server is built for one tenant (no parameter to override),
`mcp-capguard` checks exposed tools per principal with a negative control, the API filters by
the key's tenant so cross-tenant reads/approvals return 404.

**Q: A tool hangs. Does your timeout work?**
It did not at first: with the MCP in-memory transport neither `fail_after` nor the SDK
request timeout interrupted a synchronous tool (1.5 s call, 0.5 s timeout -> returned after
1.5 s). Tool bodies now run in abandon-on-cancel threads; this is safe because writes are
idempotent (ADR-A4). Scenario S18 and `test_tool_timeout_is_enforced` cover it.

**Q: What do your metrics prove?**
That on 21 controlled scenarios the platform's guarantees hold and how planner versions
compare. It is a regression suite, not a reliability estimate; one failure would change rates
by ~5 points. No zero-error claim is made.

**Q: Your LLM versions are worse than the rule planner. Isn't that a failure?**
It is the result, and the useful one. With a local 1.7B model the procedure prompt (v2) made
tool use better (recall 0.23 -> 0.45) and completion worse (0.76 -> 0.52), because the model
more often ended with an incomplete proposal. The policy refused every one of them, so no
unsafe action happened; it just cost completion. Returning the incomplete proposal as a tool
error (v3) helped a little (5 -> 2 such cases). The next steps would be a stronger model, a
discriminated-union schema for `propose_action` so the grammar forbids incomplete calls, and
a held-out scenario set before tuning further on this one.

**Q: What would you change for production?**
PostgreSQL with `SKIP LOCKED` worker claims (already supported by SQLAlchemy dialect), an OTLP
exporter to a collector, real IdP-backed principals, outbox pattern for notifications,
policy-as-data (OPA/Cedar) instead of Python rules, and canary routing between workflow
versions driven by the eval suite.
