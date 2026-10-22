# ADR-A5: yandex-workspace-mcp as an external, read-only knowledge server

Status: accepted (2026-10-22).

## Context
The task asked to reuse existing MCP projects where compatible. `yandex-workspace-mcp`
(github.com/denis-samatov/yandex-workspace-mcp, commit `92d9c30`) is a production-style MCP
server for Yandex Disk and Wiki with its own permission flags, path allowlists and audit log.
It uses the same MCP SDK major (2.x) as this platform. Runbooks in a company Wiki are the
natural documentation source for the IT-ticket workflow.

Two facts shaped the design:
* Its `server.py` builds an application at import time from the process environment, and its
  settings reject unknown `MCP_*` variables. Importing it into the platform process would tie
  the platform's environment to the tool server's validator.
* A tool server should not see the platform's secrets (API keys, database URL, cloud keys).

## Decision
* Run the server as a **separate process over stdio**, started per run with a **minimal
  environment** (`CHILD_ENV_ALLOWLIST`: PATH, HOME, locale, temp dir, Python paths).
* The gateway routes only an explicit map of local -> remote tools
  (`wiki_search` -> `search`, `wiki_get_page` -> `wiki_get_page`). Everything else the server
  exposes is invisible to workflows; the server itself also runs with Wiki write/delete and
  Disk disabled (defence in depth: two independent read-only guarantees).
* External calls go through the same step allowlist, budget, timeout, injection screening,
  redaction, tracing (`mcp.server` span attribute) and event log as internal tools.
* The demo replaces only the Yandex HTTP API (`httpx.MockTransport`, synthetic pages = the
  platform's runbooks, including the planted injection page). Settings, client, services,
  path policy and MCP tools are the upstream code.
* `ARP_KB_BACKEND=local` stays the default so that the reported evaluation is unchanged.

## Evidence
`tests/test_external_mcp.py`: a rule workflow cites the Wiki runbook (`it/kb-004`); only the
two routed tools are visible (the server offers 11 read tools); the injected Wiki page raises
`injection_in_tool_output` and causes no grant; a page outside the allowed root is refused by
the upstream path policy; the child environment withholds platform secrets.

## Consequences
* Cost: spawning the server per run adds ~1.3 s (10 runs, p50 0.14 s -> 1.47 s on 4 CPU
  cores). Production would keep a long-lived server per tenant over streamable HTTP with
  per-tenant OAuth (the upstream server supports multi-user OAuth), not one per run.
* The KB server is shared across tenants in the demo (one company Wiki). Per-tenant Wikis
  need one server (credential) per tenant; the gateway already takes a list of servers.
* The container image keeps the local KB: the extra is a git dependency and the image has no
  git; installing `.[workspace]` in the image is a one-line change where git is available.
* Not verified: live Yandex API behaviour (no credentials; out of scope by design).
