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
