"""Typed access to the audit tables.

Everything written here passes through :func:`nl2api.guardrails.redaction.redact`
first. Redaction on the way *in* rather than on the way out means a value that
was never stored cannot leak from storage later.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from nl2api.config import Role
from nl2api.guardrails.redaction import redact
from nl2api.persistence.db import session_scope
from nl2api.persistence.models import Approval, AuditEvent, Run, RunStep, utcnow


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}"


class RunRepository:
    """Reads and writes the audit trail for one database."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine

    def _scope(self):
        return session_scope(self.engine)

    # -- writes ------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str,
        request: str,
        role: Role,
        status: str,
        intent: str | None = None,
        candidates: tuple[str, ...] = (),
        clarifying_question: str | None = None,
        refusal: str | None = None,
    ) -> str:
        with self._scope() as session:
            session.add(
                Run(
                    id=run_id,
                    request=redact(request),
                    role=role.value,
                    status=status,
                    intent=redact(intent) if intent else None,
                    candidates=list(candidates),
                    clarifying_question=clarifying_question,
                    refusal=refusal,
                )
            )
        return run_id

    def add_step(
        self,
        run_id: str,
        *,
        position: int,
        step_id: str,
        operation_id: str,
        method: str,
        path: str,
        risk: str,
        arguments: dict[str, Any],
        validation: dict[str, Any],
        decision: str | None = None,
        decision_reasons: tuple[str, ...] = (),
        dry_run: dict[str, Any] | None = None,
    ) -> None:
        with self._scope() as session:
            session.add(
                RunStep(
                    run_id=run_id,
                    position=position,
                    step_id=step_id,
                    operation_id=operation_id,
                    method=method,
                    path=path,
                    risk=risk,
                    arguments=redact(arguments),
                    validation=redact(validation),
                    decision=decision,
                    decision_reasons=list(decision_reasons),
                    dry_run=redact(dry_run) if dry_run else None,
                )
            )

    def record_outcome(
        self,
        run_id: str,
        step_id: str,
        *,
        status: str,
        status_code: int | None = None,
        response: Any = None,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        with self._scope() as session:
            step = session.scalar(
                select(RunStep).where(RunStep.run_id == run_id, RunStep.step_id == step_id)
            )
            if step is None:
                return
            step.status = status
            step.status_code = status_code
            step.response = redact(response)
            step.error = error
            step.latency_ms = latency_ms

    def record_approval(
        self,
        run_id: str,
        step_id: str,
        *,
        decision: str,
        decided_by: str,
        note: str | None = None,
    ) -> None:
        with self._scope() as session:
            session.add(
                Approval(
                    run_id=run_id,
                    step_id=step_id,
                    decision=decision,
                    decided_by=decided_by,
                    note=note,
                )
            )

    def log(self, run_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        with self._scope() as session:
            session.add(AuditEvent(run_id=run_id, kind=kind, payload=redact(payload or {})))

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        final_answer: str | None = None,
        clarifying_question: str | None = None,
        completed: bool = False,
    ) -> None:
        with self._scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            if status is not None:
                run.status = status
            if outcome is not None:
                run.outcome = outcome
            if final_answer is not None:
                run.final_answer = redact(final_answer)
            if clarifying_question is not None:
                run.clarifying_question = clarifying_question
            if completed:
                run.completed_at = utcnow()

    # -- reads -------------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        with self._scope() as session:
            run = session.get(Run, run_id)
            if run is not None:
                _load(session, run)
            return run

    def approved_steps(self, run_id: str) -> frozenset[str]:
        """Steps a human has explicitly approved.

        The executor's gate reads this, so a rejection is simply an absence —
        there is no path where a rejected step is mistaken for an approved one.
        """
        with self._scope() as session:
            rows = session.scalars(
                select(Approval).where(Approval.run_id == run_id, Approval.decision == "approved")
            ).all()
            return frozenset(row.step_id for row in rows)

    def list_runs(self, limit: int = 50) -> list[Run]:
        with self._scope() as session:
            runs = list(
                session.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit)).all()
            )
            for run in runs:
                _load(session, run)
            return runs


def _load(session: Session, run: Run) -> None:
    """Force relationship loading before the session closes."""
    _ = run.steps, run.approvals, run.events
    session.expunge(run)
