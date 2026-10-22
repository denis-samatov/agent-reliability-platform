# ADR-A2: No A2A protocol in this scenario

Status: accepted (2026-10-22)

A2A (a2a-sdk 1.2.3) is for agents owned by *different parties* that need discovery and task
delegation across trust boundaries. Here one organization runs one workflow; the "agents"
are steps under one policy engine. Adding A2A would add a network hop and an auth surface
without a capability gain. Revisit if a second team's agent (e.g. a security-review agent)
must be called as a black box.
