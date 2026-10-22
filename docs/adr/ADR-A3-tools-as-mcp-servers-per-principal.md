# ADR-A3: Tools as MCP servers built per principal

Status: accepted (2026-10-22)

## Decision
The ITSM tools are an MCP server (`mcp` 2.3, `MCPServer`) constructed for a principal
(tenant, actor, scopes). Tenant is not a tool argument; write tools exist only with the
write scope; restricted resources need a scope no agent gets. Exposure per profile is tested
with `mcp-capguard` (the author's library), including a negative control.

## Consequences
+ A prompt cannot redirect a call to another tenant: there is no parameter for it.
+ The same server can be run over stdio/HTTP for other clients.
- One server instance per run (cheap in-process; would be a connection per run remotely).
