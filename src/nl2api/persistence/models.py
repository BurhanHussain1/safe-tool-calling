"""Audit tables.

The requirement these are built to: **every run must be reconstructible from
these four tables alone** — what was asked, what was planned, what validation
said, what policy decided, what the preview showed, who approved it, and what
came back. If you need the application logs to explain a refund, the audit trail
has failed.

Payloads are stored as JSON rather than normalised columns because their shape
is the schema's, not ours, and it changes when the target API changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    """One natural-language request and everything that followed from it."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(32))
    #: planned | awaiting_approval | executing | completed | failed | halted
    #: | blocked | rejected
    status: Mapped[str] = mapped_column(String(32), index=True)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    clarifying_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    refusal: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Which endpoints were offered to the model — the first thing you want when
    #: a plan looks wrong.
    candidates: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[RunStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStep.position"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list[AuditEvent]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AuditEvent.id"
    )


class RunStep(Base):
    """One planned call: what was proposed, judged, previewed and returned."""

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(16))
    operation_id: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(256))
    risk: Mapped[str] = mapped_column(String(32))

    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    dry_run: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[Run] = relationship(back_populates="steps")


class Approval(Base):
    """A human decision on one step.

    Persisted rather than held in memory because an approval that vanishes on
    restart is not an approval.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(String(16))
    #: approved | rejected
    decision: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str] = mapped_column(String(120))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="approvals")


class AuditEvent(Base):
    """An append-only record of everything that happened, in order."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="events")
