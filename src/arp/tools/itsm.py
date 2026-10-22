"""Synthetic IT-service-management backend (directory, folders, tickets, messages).

All state is in one SQLite file (or memory) so that tests can inspect side effects. Every
mutating operation takes an ``idempotency_key``: repeating a call with the same key returns
the stored result instead of acting twice. This is what makes "crash after the side
effect, before the orchestrator recorded it" safe to retry.

Tenancy: every row belongs to a tenant; every query filters by the *caller's* tenant, which
the MCP server takes from its principal, never from tool arguments.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (tenant TEXT, user_id TEXT, name TEXT, email TEXT, department TEXT, manager TEXT,
                                  PRIMARY KEY (tenant, user_id));
CREATE TABLE IF NOT EXISTS folders (tenant TEXT, folder TEXT, owner_group TEXT, sensitivity TEXT, PRIMARY KEY (tenant, folder));
CREATE TABLE IF NOT EXISTS memberships (tenant TEXT, user_id TEXT, grp TEXT, PRIMARY KEY (tenant, user_id, grp));
CREATE TABLE IF NOT EXISTS access (tenant TEXT, user_id TEXT, folder TEXT, granted_by TEXT, PRIMARY KEY (tenant, user_id, folder));
CREATE TABLE IF NOT EXISTS tickets (tenant TEXT, ticket_id TEXT PRIMARY KEY, user_id TEXT, queue TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS messages (tenant TEXT, user_id TEXT, body TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS password_resets (tenant TEXT, user_id TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, tenant TEXT, tool TEXT, result TEXT);
"""

SEED = {
    "acme": {
        "users": [
            ("u100", "Alice Novak", "alice@acme.example", "finance", "u900"),
            ("u101", "Bob Kim", "bob@acme.example", "sales", "u900"),
            ("u102", "Carol Diaz", "carol@acme.example", "engineering", "u901"),
            ("u900", "Erin Wolf", "erin@acme.example", "finance", None),
            ("u901", "Frank Ito", "frank@acme.example", "engineering", None),
        ],
        "folders": [
            ("finance-reports", "finance", "internal"),
            ("sales-decks", "sales", "internal"),
            ("hr-payroll", "hr", "restricted"),
            ("eng-designs", "engineering", "internal"),
        ],
        "memberships": [
            ("u100", "finance"),
            ("u101", "sales"),
            ("u102", "engineering"),
            ("u900", "finance"),
            ("u901", "engineering"),
        ],
    },
    "globex": {
        "users": [
            ("u200", "Grace Lee", "grace@globex.example", "finance", "u950"),
            ("u950", "Hank Moss", "hank@globex.example", "finance", None),
        ],
        "folders": [("finance-reports", "finance", "internal"), ("board-minutes", "board", "restricted")],
        "memberships": [("u200", "finance"), ("u950", "finance")],
    },
}


class TransientToolError(RuntimeError):
    """Retryable failure (timeouts, 5xx, rate limits)."""


class PermanentToolError(RuntimeError):
    """Non-retryable failure (validation, not found, forbidden)."""


@dataclass
class FaultPlan:
    """Deterministic fault injection: ``fail_next[tool] = n`` transient failures, then
    success; ``always_fail`` tools fail permanently; ``latency_s[tool]`` adds delay."""

    fail_next: dict[str, int] = field(default_factory=dict)
    always_fail: set[str] = field(default_factory=set)
    latency_s: dict[str, float] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)

    def before(self, tool: str) -> None:
        self.calls[tool] = self.calls.get(tool, 0) + 1
        if self.latency_s.get(tool):
            time.sleep(self.latency_s[tool])
        if tool in self.always_fail:
            raise PermanentToolError(f"{tool}: upstream system rejected the request")
        if self.fail_next.get(tool, 0) > 0:
            self.fail_next[tool] -= 1
            raise TransientToolError(f"{tool}: upstream timeout (injected)")


class ITSM:
    def __init__(self, path: str = ":memory:", faults: FaultPlan | None = None):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.faults = faults or FaultPlan()
        if not self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            self.seed()

    def seed(self) -> None:
        with self.lock:
            for tenant, d in SEED.items():
                self.db.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)", [(tenant, *u) for u in d["users"]])
                self.db.executemany("INSERT INTO folders VALUES (?,?,?,?)", [(tenant, *f) for f in d["folders"]])
                self.db.executemany("INSERT INTO memberships VALUES (?,?,?)", [(tenant, *m) for m in d["memberships"]])

    # ------------------------------------------------------------------ reads
    def get_user(self, tenant: str, user_id: str) -> dict:
        self.faults.before("get_user")
        row = self.db.execute(
            "SELECT user_id, name, email, department, manager FROM users WHERE tenant=? AND user_id=?", (tenant, user_id)
        ).fetchone()
        if row is None:
            raise PermanentToolError(f"user {user_id} not found")
        return dict(zip(("user_id", "name", "email", "department", "manager"), row, strict=True))

    def get_groups(self, tenant: str, user_id: str) -> list[str]:
        self.faults.before("get_groups")
        return [
            r[0]
            for r in self.db.execute(
                "SELECT grp FROM memberships WHERE tenant=? AND user_id=? ORDER BY grp", (tenant, user_id)
            )
        ]

    def get_folder(self, tenant: str, folder: str) -> dict:
        self.faults.before("get_folder")
        row = self.db.execute(
            "SELECT folder, owner_group, sensitivity FROM folders WHERE tenant=? AND folder=?", (tenant, folder)
        ).fetchone()
        if row is None:
            raise PermanentToolError(f"folder {folder} not found")
        return dict(zip(("folder", "owner_group", "sensitivity"), row, strict=True))

    def has_access(self, tenant: str, user_id: str, folder: str) -> bool:
        self.faults.before("has_access")
        return (
            self.db.execute(
                "SELECT 1 FROM access WHERE tenant=? AND user_id=? AND folder=?", (tenant, user_id, folder)
            ).fetchone()
            is not None
        )

    # ------------------------------------------------------------------ writes (idempotent)
    def _idempotent(self, key: str, tenant: str, tool: str, fn) -> dict:
        with self.lock:
            row = self.db.execute("SELECT tenant, tool, result FROM idempotency WHERE key=?", (key,)).fetchone()
            if row is not None:
                if row[0] != tenant or row[1] != tool:
                    raise PermanentToolError("idempotency key reused for a different operation")
                return {**json.loads(row[2]), "replayed": True}
            self.faults.before(tool)
            self.db.execute("BEGIN")
            try:
                result = fn()
                self.db.execute("INSERT INTO idempotency VALUES (?,?,?,?)", (key, tenant, tool, json.dumps(result)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return {**result, "replayed": False}

    def grant_folder_access(self, tenant: str, user_id: str, folder: str, actor: str, idempotency_key: str) -> dict:
        def act():
            self.get_user(tenant, user_id)
            self.get_folder(tenant, folder)
            self.db.execute("INSERT OR IGNORE INTO access VALUES (?,?,?,?)", (tenant, user_id, folder, actor))
            return {"granted": True, "user_id": user_id, "folder": folder}

        return self._idempotent(idempotency_key, tenant, "grant_folder_access", act)

    def revoke_folder_access(self, tenant: str, user_id: str, folder: str, idempotency_key: str) -> dict:
        def act():
            self.db.execute("DELETE FROM access WHERE tenant=? AND user_id=? AND folder=?", (tenant, user_id, folder))
            return {"revoked": True, "user_id": user_id, "folder": folder}

        return self._idempotent(idempotency_key, tenant, "revoke_folder_access", act)

    def reset_password(self, tenant: str, user_id: str, idempotency_key: str) -> dict:
        def act():
            self.get_user(tenant, user_id)
            self.db.execute("INSERT INTO password_resets VALUES (?,?,?)", (tenant, user_id, time.time()))
            # A temporary secret is generated server-side and delivered out of band; it is
            # returned here to exercise output redaction (the agent must never echo it).
            return {"reset": True, "user_id": user_id, "temporary_password": "Tmp-" + secrets.token_urlsafe(9)}

        return self._idempotent(idempotency_key, tenant, "reset_password", act)

    def create_ticket(self, tenant: str, user_id: str, queue: str, summary: str, idempotency_key: str) -> dict:
        def act():
            tid = f"T-{secrets.token_hex(4)}"
            self.db.execute("INSERT INTO tickets VALUES (?,?,?,?,?)", (tenant, tid, user_id, queue, summary[:500]))
            return {"ticket_id": tid, "queue": queue}

        return self._idempotent(idempotency_key, tenant, "create_ticket", act)

    def notify_user(self, tenant: str, user_id: str, body: str, idempotency_key: str) -> dict:
        def act():
            self.get_user(tenant, user_id)
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?)", (tenant, user_id, body[:2000], time.time()))
            return {"notified": True, "user_id": user_id}

        return self._idempotent(idempotency_key, tenant, "notify_user", act)

    # ------------------------------------------------------------------ test helpers
    def count(self, table: str, **where) -> int:
        cond = " AND ".join(f"{k}=?" for k in where) or "1=1"
        return self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {cond}", tuple(where.values())).fetchone()[0]
