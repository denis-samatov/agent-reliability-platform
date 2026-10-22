# ADR-A4: Tool timeouts need abandon-on-cancel threads plus idempotency

Status: accepted (2026-10-22). Found by scenario S18.

## Observation
With mcp 2.3 and the in-memory client, neither an outer `anyio.fail_after` nor the SDK's
`read_timeout_seconds` interrupted a synchronous tool: a 1.5 s call with a 0.5 s timeout
returned after 1.5 s. Sync tools run via `anyio.to_thread.run_sync`, which by default waits
for the thread even when cancelled.

## Decision
Wrap every tool body in `anyio.to_thread.run_sync(..., abandon_on_cancel=True)` and use the
SDK request timeout. An abandoned write may still complete in the background; that is safe
only because writes carry idempotency keys and the retry replays the stored result.

## Result
The timeout scenario now fails after 3 timed-out attempts (~0.5 s each) and leaves no effect.
