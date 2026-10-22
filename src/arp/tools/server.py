"""MCP server exposing ITSM tools, built per principal (tenant + scopes).

Security properties enforced *by the server*, independent of what the model asks for:

* **Tenant isolation** - the tenant comes from the principal the server was built for.
  Tools have no ``tenant`` argument, so a prompt cannot redirect a call to another tenant.
* **Scoped exposure** - write tools are registered only when the principal holds the
  ``itsm:write`` scope; ``mcp-capguard`` tests the exposed tool set per profile
  (``tests/test_capguard.py``).
* **Restricted resources** - folders marked ``restricted`` cannot be granted by an agent
  principal at all (``itsm:grant_restricted`` is never given to agents).
* **Idempotency** - every write takes an ``idempotency_key`` (supplied by the orchestrator,
  not the model).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Annotated

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from arp.tools.itsm import ITSM, PermanentToolError, TransientToolError
from arp.tools.kb import KnowledgeBase

READ, WRITE, GRANT_RESTRICTED = "itsm:read", "itsm:write", "itsm:grant_restricted"
READ_TOOLS = ("classify_ticket", "search_kb", "get_user", "get_groups", "get_folder", "check_access")
WRITE_TOOLS = ("grant_folder_access", "revoke_folder_access", "reset_password", "create_ticket", "notify_user")
UserId = Annotated[str, Field(pattern=r"^u\d{3}$", description="Directory user id, e.g. u100")]
Folder = Annotated[str, Field(pattern=r"^[a-z0-9-]{3,40}$")]


@dataclass(frozen=True)
class Principal:
    tenant: str
    actor: str  # e.g. "agent:it-ticket" or "human:erin"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({READ}))


def surface_errors(fn):
    """Deliberate tool errors reach the client as ``ToolError`` with a machine-readable class
    prefix (``TRANSIENT:`` / ``PERMANENT:``) so the orchestrator can decide whether to
    retry. Any other exception stays a sanitized crash on the server side."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            # Blocking backend work runs in a worker thread that is *abandoned* on cancellation,
            # so caller timeouts take effect. An abandoned write may still finish later; that is
            # safe because retries reuse the same idempotency key.
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs), abandon_on_cancel=True)
        except TransientToolError as e:
            raise ToolError(f"TRANSIENT: {e}") from e
        except PermanentToolError as e:
            raise ToolError(f"PERMANENT: {e}") from e

    return wrapper


def build_server(principal: Principal, itsm: ITSM, kb: KnowledgeBase, classifier=None) -> MCPServer:
    mcp = MCPServer(name=f"itsm-{principal.tenant}")
    _tool = mcp.tool

    def tool(**kw):
        return lambda fn: _tool(**kw)(surface_errors(fn))

    mcp_tool = tool
    tenant = principal.tenant
    ro = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

    def need(scope: str) -> None:
        if scope not in principal.scopes:
            raise PermanentToolError(f"principal {principal.actor} lacks scope {scope}")

    @mcp_tool(
        name="classify_ticket",
        description="Classify an IT ticket into a service category (LocalDesk "
        "calibrated classifier). Returns category, probabilities and whether it should go to review.",
        annotations=ro,
    )
    def classify_ticket(text: Annotated[str, Field(min_length=3, max_length=4000)]) -> dict:
        need(READ)
        itsm.faults.before("classify_ticket")
        if classifier is None:
            raise PermanentToolError("classifier not configured")
        out = classifier(text)
        return {
            "category": out["category"],
            "action": out["action"],
            "review_reasons": out["review_reasons"],
            "probabilities": out["probabilities"]["category"],
        }

    @mcp_tool(name="search_kb", description="Search IT runbooks. Returns the top passages with ids.", annotations=ro)
    def search_kb(
        query: Annotated[str, Field(min_length=2, max_length=300)], k: Annotated[int, Field(ge=1, le=5)] = 3
    ) -> dict:
        need(READ)
        itsm.faults.before("search_kb")
        return {"results": [{"id": d.doc_id, "title": d.title, "text": d.text} for d in kb.search(query, k)]}

    @mcp_tool(name="get_user", description="Look up a user in the directory of the caller's tenant.", annotations=ro)
    def get_user(user_id: UserId) -> dict:
        need(READ)
        return itsm.get_user(tenant, user_id)

    @mcp_tool(name="get_groups", description="List the groups a user belongs to.", annotations=ro)
    def get_groups(user_id: UserId) -> dict:
        need(READ)
        return {"user_id": user_id, "groups": itsm.get_groups(tenant, user_id)}

    @mcp_tool(name="get_folder", description="Get a shared folder's owner group and sensitivity.", annotations=ro)
    def get_folder(folder: Folder) -> dict:
        need(READ)
        return itsm.get_folder(tenant, folder)

    @mcp_tool(name="check_access", description="Check whether a user already has access to a folder.", annotations=ro)
    def check_access(user_id: UserId, folder: Folder) -> dict:
        need(READ)
        return {"user_id": user_id, "folder": folder, "has_access": itsm.has_access(tenant, user_id, folder)}

    if WRITE in principal.scopes:
        rw = ToolAnnotations(read_only_hint=False, idempotent_hint=True, destructive_hint=False)

        @mcp_tool(name="grant_folder_access", description="Grant a user access to a shared folder.", annotations=rw)
        def grant_folder_access(user_id: UserId, folder: Folder, idempotency_key: str) -> dict:
            need(WRITE)
            if itsm.get_folder(tenant, folder)["sensitivity"] == "restricted":
                need(GRANT_RESTRICTED)
            return itsm.grant_folder_access(tenant, user_id, folder, principal.actor, idempotency_key)

        @mcp_tool(
            name="revoke_folder_access",
            description="Revoke a user's access to a folder (compensation).",
            annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True, destructive_hint=True),
        )
        def revoke_folder_access(user_id: UserId, folder: Folder, idempotency_key: str) -> dict:
            need(WRITE)
            return itsm.revoke_folder_access(tenant, user_id, folder, idempotency_key)

        @mcp_tool(
            name="reset_password",
            description="Reset a user's password; the temporary password is delivered out of band.",
            annotations=rw,
        )
        def reset_password(user_id: UserId, idempotency_key: str) -> dict:
            need(WRITE)
            return itsm.reset_password(tenant, user_id, idempotency_key)

        @mcp_tool(name="create_ticket", description="Escalate to a human support queue.", annotations=rw)
        def create_ticket(
            user_id: UserId,
            queue: Annotated[str, Field(pattern=r"^[a-z0-9-]{2,30}$")],
            summary: Annotated[str, Field(max_length=500)],
            idempotency_key: str,
        ) -> dict:
            need(WRITE)
            return itsm.create_ticket(tenant, user_id, queue, summary, idempotency_key)

        @mcp_tool(name="notify_user", description="Send a message to the requester.", annotations=rw)
        def notify_user(user_id: UserId, body: Annotated[str, Field(max_length=2000)], idempotency_key: str) -> dict:
            need(WRITE)
            return itsm.notify_user(tenant, user_id, body, idempotency_key)

    return mcp


def localdesk_classifier():
    """LocalDesk's packaged calibrated TF-IDF model (trained on public IT tickets)."""
    from localdesk.core import decide
    from localdesk.tfidf import demo_backend

    backend = demo_backend()
    return lambda text: decide(text, backend)
