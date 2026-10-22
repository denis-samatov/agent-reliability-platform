"""Run the real `yandex-workspace-mcp` server over stdio against a synthetic Yandex Wiki.

Everything except the Yandex HTTP API is the upstream project's code: settings validation,
permission flags, path allowlists, the Wiki HTTP client and its error classification, the
services and the MCP tools. The HTTP API is replaced by an ``httpx.MockTransport`` serving the
IT runbooks of :mod:`arp.tools.kb` as Wiki pages under the ``it`` cluster, including the
planted injection page. The server is read-only: Wiki write/delete and Disk are disabled.

Run as a separate process (``python -m arp.tools.yandex_wiki_demo``): the upstream module
builds its application at import time from the process environment, and the platform starts
it with a minimal environment so that platform secrets never reach a tool server.
"""

from __future__ import annotations

import json

import httpx

from arp.tools.kb import RUNBOOKS

CLUSTER = "it"
BASE_URL = "https://api.wiki.yandex.net/v1"


def _pages() -> dict[str, dict]:
    return {
        f"{CLUSTER}/{d.doc_id}": {"id": i + 1, "slug": f"{CLUSTER}/{d.doc_id}", "title": d.title, "content": d.text}
        for i, d in enumerate(RUNBOOKS)
    }


def handler(request: httpx.Request) -> httpx.Response:
    """Minimal stand-in for the Wiki API endpoints used by read-only tools."""
    pages = _pages()
    if request.method == "POST" and request.url.path.endswith("/search"):
        body = json.loads(request.content)
        terms = [t for t in body["query"].lower().split() if len(t) > 2]
        cluster = (body.get("filters") or {}).get("cluster")
        hits = []
        for p in pages.values():
            if cluster and not p["slug"].startswith(cluster + "/"):
                continue
            score = sum((p["title"] + " " + p["content"]).lower().count(t) for t in terms)
            if score:
                hits.append((score, p))
        hits.sort(key=lambda x: (-x[0], x[1]["id"]))
        results = [{**p, "type": "page"} for _, p in hits[: body.get("limit", 20)]]
        return httpx.Response(200, json={"results": results})
    if request.method == "GET" and request.url.path.endswith("/pages"):
        page = pages.get(request.url.params.get("slug", ""))
        return httpx.Response(200, json=page) if page else httpx.Response(404, json={"error": "not found"})
    return httpx.Response(404, json={"error": "not found"})


def build_application():
    from pydantic import SecretStr
    from yandex_workspace_mcp.clients.wiki import YandexWikiClient
    from yandex_workspace_mcp.config import Settings
    from yandex_workspace_mcp.server import ApplicationDependencies, create_application

    settings = Settings(
        _env_file=None,
        yandex_oauth_token=SecretStr("synthetic-token"),
        yandex_disk_enabled=False,
        wiki_read=True,
        wiki_allowed_roots=[CLUSTER],
    )

    def wiki_client():
        transport = httpx.MockTransport(handler)
        return YandexWikiClient(
            "synthetic-token", "demo-org", client=httpx.AsyncClient(base_url=BASE_URL, transport=transport)
        )

    return create_application(settings, ApplicationDependencies(wiki_client_factory=wiki_client))


def main() -> None:
    import logging

    from yandex_workspace_mcp.logging import setup_logging

    setup_logging("WARNING")  # stderr of the child process; keep it to warnings and errors
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application().mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
