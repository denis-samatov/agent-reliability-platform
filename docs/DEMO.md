# Demo script (about 4 minutes)

Setup: `ARP_API_KEYS="acme-req:acme:requester,acme-appr:acme:approver" arp serve`
(+ optional `ARP_LLM_BASE_URL` for v1-v3 planners).

1. Submit "I need access to the finance-reports folder." as u100 -> run parks in
   `waiting_approval`; show the event log and trace (spans per step and tool call).
2. Approve -> resumes; replayed steps are not re-executed; `grant_folder_access` once.
3. Submit the injection ticket (S11) -> policy violation, escalation to `security`, no grant.
4. Show `pytest tests/test_recovery.py -q`: worker killed after the side effect; second
   worker finishes; one ticket in the database.
5. Show `results/EVAL.md`: v0 rules vs v1-v3 LLM planners on 21 scenarios; point at S11 (injection stopped at the approval gate) and the v2 incomplete-proposal failure mode.

## Video walkthrough script (about 3 minutes)

| time | on screen | narration |
|---|---|---|
| 0:00-0:15 | README header | "A platform that runs LLM agents against company tools so that crashes, retries, injections, budgets and approvals are handled by the platform - and a failure-injection suite that checks it." |
| 0:15-0:45 | dashboard: submit folder-access ticket, run waits for approval, approve | "The agent proposes a folder grant; the policy requires approval; the run parks durably. After approval it resumes, replaying completed steps instead of re-running them." |
| 0:45-1:10 | dashboard: injection ticket, trace and event log (`docs/img/dashboard.png` state) | "An injected ticket asks for HR payroll. The screening flags it, the deterministic policy refuses restricted folders, and an escalation ticket goes to security. The trace shows every step and tool call." |
| 1:10-1:35 | terminal: `pytest -q tests/test_recovery.py` | "The worker process is killed right after the side effect. A second worker finishes the run, and the database holds exactly one ticket: idempotency keys give exactly-once effects." |
| 1:35-2:00 | terminal: `pytest -q tests/test_external_mcp.py`, ADR-A5 | "Runbooks come from the real yandex-workspace-mcp server in its own process with a minimal environment. Only two of its eleven read tools are routed, and an injected Wiki page is screened like any other tool output." |
| 2:00-2:45 | `results/EVAL.md` version table | "Four planner versions on 21 scenarios. No version executed an unauthorized action. The local 1.7B model completes fewer tasks than the rules because it often ends with an incomplete proposal, which the policy refuses." |
| 2:45-3:00 | `results/TRACES.md` | "Every scenario's trace and key events are in the repository; 21 scenarios are a regression suite, not a reliability estimate." |
