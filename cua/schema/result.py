"""The replay result contract. Exactly four statuses; recoveries are notes on a run, not a status.

  SUCCESS           flow ran and the checkpoint verified          -> use `outputs`
  BUSINESS_OUTCOME  a *declared* outcome occurred (not found...)   -> a legitimate answer, see `outcome`
  NEEDS_HUMAN       cannot safely proceed                          -> wait on `intervention_id`
  FAILED            hard failure                                   -> debug with `failure`
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

REDACTED = "●●●●"


class Status(str, Enum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class FailureCategory(str, Enum):
    invalid_input = "invalid_input"
    precondition = "precondition"
    target_not_found = "target_not_found"
    checkpoint_mismatch = "checkpoint_mismatch"
    timeout = "timeout"
    app_error = "app_error"
    session_expired_unrecoverable = "session_expired_unrecoverable"
    policy_blocked = "policy_blocked"
    unexpected_state = "unexpected_state"


class RecoveryKind(str, Enum):
    interstitial_dismissed = "interstitial_dismissed"
    retry_transient = "retry_transient"
    reauthenticated = "reauthenticated"
    locator_fallback = "locator_fallback"  # primary locator missed; a fallback matched: a drift signal


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recovery(Strict):
    kind: RecoveryKind
    step_id: str
    detail: str
    attempt: int = 1


class Failure(Strict):
    step_id: str | None
    category: FailureCategory
    expected: str
    observed: str
    evidence_ref: str | None = Field(default=None, description="Path to screenshot/DOM snapshot for this failure.")


class OutcomeInfo(Strict):
    code: str
    description: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None


class ReplayResult(Strict):
    status: Status
    capability_id: str
    capability_version: str
    variant: str
    run_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    steps_executed: int = 0
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: OutcomeInfo | None = None
    failure: Failure | None = None
    intervention_id: str | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    evidence_dir: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ReplayResult":
        s = self.status
        if (s == Status.BUSINESS_OUTCOME) != (self.outcome is not None):
            raise ValueError("outcome must be set exactly when status is BUSINESS_OUTCOME")
        if (s == Status.FAILED) != (self.failure is not None):
            raise ValueError("failure must be set exactly when status is FAILED")
        if s == Status.NEEDS_HUMAN and not self.intervention_id:
            raise ValueError("NEEDS_HUMAN requires an intervention_id")
        if s != Status.SUCCESS and self.outputs:
            raise ValueError("outputs are only returned on SUCCESS")
        return self

    @property
    def ok(self) -> bool:
        return self.status == Status.SUCCESS

    def for_log(self, sensitive_outputs: set[str]) -> dict[str, Any]:
        """JSON-safe dict with sensitive outputs masked. The caller gets raw values; logs never do."""
        d = self.model_dump(mode="json")
        d["outputs"] = {k: (REDACTED if k in sensitive_outputs else v) for k, v in d["outputs"].items()}
        return d
