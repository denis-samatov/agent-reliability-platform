"""Configurations of external MCP servers the platform can attach to a run."""

from __future__ import annotations

import sys

from mcp.client.stdio import StdioServerParameters

from arp.engine.gateway import ExternalServer, minimal_env

# Local tool name -> tool of yandex-workspace-mcp. Read-only by construction: the server runs
# with Wiki write/delete and Disk disabled, *and* only these two names are routed.
YANDEX_WIKI_EXPOSE = {"wiki_search": "search", "wiki_get_page": "wiki_get_page"}


def yandex_wiki_server() -> ExternalServer:
    """yandex-workspace-mcp (github.com/denis-samatov/yandex-workspace-mcp) against the synthetic
    Wiki of :mod:`arp.tools.yandex_wiki_demo`, one process per run, minimal environment."""
    try:
        import yandex_workspace_mcp  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("ARP_KB_BACKEND=yandex-wiki needs: pip install -e '.[workspace]'") from e
    params = StdioServerParameters(command=sys.executable, args=["-m", "arp.tools.yandex_wiki_demo"], env=minimal_env())
    return ExternalServer("yandex-workspace-mcp", params, dict(YANDEX_WIKI_EXPOSE))
