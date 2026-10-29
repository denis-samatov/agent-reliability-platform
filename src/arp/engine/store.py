"""Durable run state: runs, an append-only event log per run, approvals.

The event log is the source of truth for recovery: a step whose ``step_completed`` event is
present is never re-executed; its recorded output is replayed. Leases (owner + expiry)
guarantee that only one executor advances a run at a time; an expired lease (crashed
worker) can be taken over.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    case,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class LeaseLost(RuntimeError):
    """The invocation no longer owns a live execution lease."""


TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "budget_exceeded", "blocked"}


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    requester: Mapped[str] = mapped_column(String(64))
    workflow: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    version_hash: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    budget: Mapped[dict] = mapped_column(JSON)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))
    step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    step: Mapped[str] = mapped_column(String(64))
    action: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Store:
    def __init__(self, url: str):
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kw)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    # -- runs
    def create_run(self, **fields) -> RunRow:
        with self.Session() as s:
            run = RunRow(**fields)
            s.add(run)
            s.commit()
            return run

    def get_run(self, run_id: str) -> RunRow | None:
        with self.Session() as s:
            return s.get(RunRow, run_id)

    def update_run(self, run_id: str, *, execution_owner: str | None = None, **fields) -> None:
        with self.Session() as s:
            self._guard_owner(s, run_id, execution_owner)
            s.execute(update(RunRow).where(RunRow.id == run_id).values(**fields))
            s.commit()

    def request_cancel(self, run_id: str) -> None:
        with self.Session() as s:
            s.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.status.notin_(TERMINAL))
                .values(
                    cancel_requested=True,
                    status=case((RunRow.status == "waiting_approval", "pending"), else_=RunRow.status),
                )
            )
            s.commit()

    def assert_owner(self, run_id: str, *, execution_owner: str | None = None) -> None:
        with self.Session() as s:
            self._guard_owner(s, run_id, execution_owner)
            s.commit()

    def park_approval(self, run_id: str, approval_id: int, *, execution_owner: str | None = None) -> str:
        with self.Session() as s:
            self._guard_owner(s, run_id, execution_owner)
            approval = s.get(ApprovalRow, approval_id)
            if approval is None or approval.run_id != run_id:
                raise KeyError("approval not found")
            status = "waiting_approval" if approval.status == "pending" else "pending"
            s.execute(update(RunRow).where(RunRow.id == run_id).values(status=status))
            s.commit()
            return status

    def acquire_lease(self, run_id: str, owner: str, seconds: float) -> bool:
        """Claim a free or expired lease using a unique invocation token."""
        with self.Session() as s:
            t = now()
            res = s.execute(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status.notin_(TERMINAL | {"waiting_approval"}),
                    (RunRow.lease_owner.is_(None)) | (RunRow.lease_until < t),
                )
                .values(lease_owner=owner, lease_until=t + dt.timedelta(seconds=seconds))
            )
            s.commit()
            return res.rowcount == 1

    def renew_lease(self, run_id: str, owner: str, seconds: float) -> bool:
        with self.Session() as s:
            t = now()
            result = s.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.lease_owner == owner, RunRow.lease_until > t)
                .values(lease_until=t + dt.timedelta(seconds=seconds))
            )
            s.commit()
            return result.rowcount == 1

    @staticmethod
    def _guard_owner(session: Session, run_id: str, owner: str | None) -> None:
        if owner is None:
            # Administrative events share the sequence allocator with executor events.
            session.execute(update(RunRow).where(RunRow.id == run_id).values(lease_until=RunRow.lease_until))
            return
        # A conditional UPDATE locks the row through the following mutation/commit on
        # PostgreSQL and SQLite. A separate SELECT would leave a takeover race.
        result = session.execute(
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.lease_owner == owner, RunRow.lease_until > now())
            .values(lease_until=RunRow.lease_until)
        )
        if result.rowcount != 1:
            raise LeaseLost("execution lease lost")

    def release_lease(self, run_id: str, owner: str) -> None:
        with self.Session() as s:
            s.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.lease_owner == owner)
                .values(lease_owner=None, lease_until=None)
            )
            s.commit()

    def running_count(self, tenant: str) -> int:
        with self.Session() as s:
            return (
                s.scalar(select(func.count()).select_from(RunRow).where(RunRow.tenant == tenant, RunRow.status == "running"))
                or 0
            )

    # -- events
    def append(
        self,
        run_id: str,
        type_: str,
        step: str | None = None,
        payload: dict | None = None,
        *,
        execution_owner: str | None = None,
    ) -> int:
        with self.Session() as s:
            self._guard_owner(s, run_id, execution_owner)
            seq = (s.scalar(select(func.max(EventRow.seq)).where(EventRow.run_id == run_id)) or 0) + 1
            s.add(EventRow(run_id=run_id, seq=seq, type=type_, step=step, payload=payload or {}))
            s.commit()
            return seq

    def events(self, run_id: str) -> list[EventRow]:
        with self.Session() as s:
            return list(s.scalars(select(EventRow).where(EventRow.run_id == run_id).order_by(EventRow.seq)))

    # -- approvals
    def request_approval(
        self, run_id: str, tenant: str, step: str, action: dict, *, execution_owner: str | None = None
    ) -> int:
        with self.Session() as s:
            self._guard_owner(s, run_id, execution_owner)
            existing = s.scalars(select(ApprovalRow).where(ApprovalRow.run_id == run_id, ApprovalRow.step == step)).first()
            if existing:
                return existing.id
            a = ApprovalRow(run_id=run_id, tenant=tenant, step=step, action=action)
            s.add(a)
            s.commit()
            return a.id

    def approval(self, run_id: str, step: str) -> ApprovalRow | None:
        with self.Session() as s:
            return s.scalars(select(ApprovalRow).where(ApprovalRow.run_id == run_id, ApprovalRow.step == step)).first()

    def decide(self, approval_id: int, tenant: str, approve: bool, by: str, reason: str = "") -> ApprovalRow:
        with self.Session() as s:
            a = s.get(ApprovalRow, approval_id)
            if a is None or a.tenant != tenant:
                raise KeyError("approval not found")  # cross-tenant decisions look like "not found"
            # Serialize decisions with parking on the run row; refresh the approval
            # only after this lock, so a previous decision cannot be overwritten.
            s.execute(update(RunRow).where(RunRow.id == a.run_id).values(lease_until=RunRow.lease_until))
            s.refresh(a)
            if a.status != "pending":
                raise ValueError(f"approval already {a.status}")
            a.status, a.decided_by, a.reason, a.decided_at = ("approved" if approve else "denied"), by, reason, now()
            run = s.get(RunRow, a.run_id)
            if run.status == "waiting_approval":
                run.status = "pending"
            s.commit()
            s.refresh(a)
            decision = {"approval_id": a.id, "decision": a.status, "by": by, "reason": reason}
            run_id, step = a.run_id, a.step
        # The human decision is part of the run's audit trail, next to what the agent did.
        self.append(run_id, "approval_decided", step, decision)
        return a

    def pending_approvals(self, tenant: str | None = None) -> list[ApprovalRow]:
        with self.Session() as s:
            q = select(ApprovalRow).where(ApprovalRow.status == "pending")
            if tenant:
                q = q.where(ApprovalRow.tenant == tenant)
            return list(s.scalars(q.order_by(ApprovalRow.id)))

    def list_runs(self, limit: int = 100, tenant: str | None = None) -> list[RunRow]:
        with self.Session() as s:
            q = select(RunRow)
            if tenant:
                q = q.where(RunRow.tenant == tenant)
            return list(s.scalars(q.order_by(RunRow.created_at.desc()).limit(limit)))


def session_factory(store: Store) -> sessionmaker[Session]:
    return store.Session


class FencedStore:
    """Run-context view: fence executor mutations in their own DB transaction."""

    def __init__(self, store: Store, owner: str):
        self._store = store
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._store, name)

    def append(self, *args, **kwargs):
        return self._store.append(*args, execution_owner=self._owner, **kwargs)

    def update_run(self, *args, **kwargs):
        return self._store.update_run(*args, execution_owner=self._owner, **kwargs)

    def request_approval(self, *args, **kwargs):
        return self._store.request_approval(*args, execution_owner=self._owner, **kwargs)

    def assert_owner(self, *args, **kwargs):
        return self._store.assert_owner(*args, execution_owner=self._owner, **kwargs)

    def park_approval(self, *args, **kwargs):
        return self._store.park_approval(*args, execution_owner=self._owner, **kwargs)
