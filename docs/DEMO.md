# Approval and crash-recovery demo

Run a synthetic support ticket through a human approval and replay a worker crash. The dashboard uses the built-in rule planner, SQLite and a mock ITSM backend.

## Run the local demo

From a source checkout with Python 3.12 in a virtual environment, run `python -m pip install .`. Then start the local dashboard:

```bash
demo_dir="$(mktemp -d)"
export ARP_DATABASE_URL="sqlite:///$demo_dir/runs.db"
export ARP_ITSM_PATH="$demo_dir/itsm.db"
export ARP_TRACE_DIR="$demo_dir/traces"
export ARP_API_KEYS="acme-req:acme:requester,acme-appr:acme:approver"
arp serve --versions v0-rules --host 127.0.0.1
```

1. Open <http://127.0.0.1:8095>. Enter the local demo key `acme-req`, select **v0-rules** and keep the prefilled requester `u100` and folder-access ticket. Click **Submit and run**. The run pauses at `waiting_approval`.
2. Replace the key with `acme-appr`, click **Refresh**, then choose **Approve** in the **Approvals** panel.
3. Open the same run in **Runs**. It shows `succeeded`. Inspect the event log and trace for the resumed run and the mock folder grant. In the API, an attempt to approve with the requester role returns HTTP 403.

## Replay the worker-crash test

From the checkout root, install the dev dependencies and run the focused recovery test:

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/test_recovery.py -q
```

The test terminates a worker after the mock ticket is created and before the execution step is recorded as complete. A second worker resumes the run. The test checks that the mock backend contains one ticket and that the tool call was replayed using the same idempotency key. This result is scoped to the tested SQLite backend and crash point.

## Presenter notes

The optional LLM planners use a configured `ARP_LLM_BASE_URL`. The [scenario evaluation](../results/EVAL.md) includes the recorded runs.

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
| 1:10-1:35 | terminal: `pytest -q tests/test_recovery.py` | "The worker process is killed right after the side effect. A second worker finishes the run, and the database holds exactly one ticket. The retry uses the same idempotency key for this mock tool." |
| 1:35-2:00 | terminal: `pytest -q tests/test_external_mcp.py`, ADR-A5 | "Runbooks come from the real yandex-workspace-mcp server in its own process with a minimal environment. Only two of its eleven read tools are routed, and an injected Wiki page is screened like any other tool output." |
| 2:00-2:45 | `results/EVAL.md` version table | "Four planner versions on 21 scenarios. No version executed an unauthorized action. The local 1.7B model completes fewer tasks than the rules because it often ends with an incomplete proposal, which the policy refuses." |
| 2:45-3:00 | `results/TRACES.md` | "Every scenario's trace and key events are in the repository; 21 scenarios are a regression suite, not a reliability estimate." |
