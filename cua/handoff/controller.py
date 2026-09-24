"""Control transfer for one live session.

Exactly one party controls the session at a time:

    AUTOMATION --escalate--> PAUSED --claim--> HUMAN --resume(retry|skip)--> AUTOMATION
                               |                 |
                               +--approve/abort--+--approve/abort--------------> AUTOMATION
                               +--(no operator before timeout)--> AUTOMATION, run ends NEEDS_HUMAN

Enforcement is structural, not by convention: the surface calls `assert_automation()` before every
mutating action, so automation physically cannot click while a human holds the session. Human input is
captured by the surface's recorder and kept only while state == HUMAN (automation's own clicks fire the
same DOM listeners and are dropped here).
"""
import asyncio
import time
from dataclasses import asdict, dataclass
from enum import Enum

from cua.handoff.intervention import Intervention, InterventionStore, now_iso
from cua.policy.redact import Redactor


class Control(str, Enum):
    AUTOMATION = "AUTOMATION"
    PAUSED = "PAUSED"
    HUMAN = "HUMAN"


class ControlError(Exception):
    """An action was attempted by a party that does not hold control, or out of order."""


@dataclass(frozen=True)
class Decision:
    action: str  # approve | retry | skip | abort | session_lost
    operator: str
    note: str = ""


class SessionController:
    def __init__(self, store: InterventionStore, redactor: Redactor):
        self.store, self.redactor = store, redactor
        self.state = Control.AUTOMATION
        self.holder = "automation"
        self.current: Intervention | None = None
        self.surface = None
        self._decision: asyncio.Future | None = None

    async def attach(self, surface) -> None:
        """Bind to a live surface: lock its actions to the control state and start capturing human input."""
        self.surface = surface
        surface.controller = self
        await surface.install_human_recorder(self.record_human)

    # ── automation side ──
    def assert_automation(self) -> None:
        if self.state != Control.AUTOMATION:
            raise ControlError(f"automation may not act while control is {self.state.value} (held by {self.holder})")

    async def escalate(self, iv: Intervention, unclaimed_timeout_s: float) -> Decision | None:
        """Pause and wait for an operator. Returns their decision, or None if nobody claimed it in time.
        The timeout only runs while unclaimed: once a human holds the session we never yank it back."""
        self.assert_automation()
        self.current, self._decision = iv, asyncio.get_running_loop().create_future()
        self._move(Control.PAUSED, "nobody", f"{iv.kind} requested: {iv.reason}")
        deadline = time.monotonic() + unclaimed_timeout_s
        try:
            while not self._decision.done():
                if self.surface is not None and not self.surface.alive():
                    # nothing to hand back to: the live session is gone (window closed, browser crashed)
                    iv.state = "session_lost"
                    self._move(Control.AUTOMATION, "automation", "live session was closed while paused")
                    return Decision("session_lost", "system", "live session closed during handoff")
                if self.state == Control.PAUSED and time.monotonic() > deadline:
                    iv.state = "expired"
                    self._move(Control.AUTOMATION, "automation", "no operator responded in time")
                    return None
                await asyncio.wait([self._decision], timeout=0.2)
            return self._decision.result()
        finally:
            self.store.save(iv)
            self.current, self._decision = None, None

    def assert_human(self, operator: str) -> str:
        """Gate for remote human input: only the operator who holds the session may drive it."""
        op = self._op(operator)
        if self.state != Control.HUMAN or self.holder != op:
            raise ControlError(f"only the operator holding the session may act (control is {self.state.value}, "
                               f"held by {self.holder}); take control first")
        return op

    # ── operator side ──
    def claim(self, iv_id: str, operator: str) -> None:
        iv, op = self._open(iv_id), self._op(operator)
        if self.state != Control.PAUSED:
            raise ControlError(f"cannot take control: session is {self.state.value} (held by {self.holder})")
        iv.state, iv.claimed_by = "claimed", op
        self._move(Control.HUMAN, op, "operator took control of the live session")

    def approve(self, iv_id: str, operator: str, note: str = "") -> None:
        iv, op = self._open(iv_id), self._op(operator)
        if iv.kind != "approval":
            raise ControlError("only approval requests can be approved; take control and resume instead")
        self._check_may_decide(op)
        self._decide(iv, Decision("approve", op, note), "approved: automation will perform the step")

    def resume(self, iv_id: str, operator: str, mode: str = "retry", note: str = "") -> None:
        iv, op = self._open(iv_id), self._op(operator)
        if mode not in ("retry", "skip"):
            raise ControlError("mode must be 'retry' (automation redoes the step) or 'skip' (I did the step)")
        if mode == "skip" and self.state != Control.HUMAN:
            raise ControlError("'skip' means you performed the step yourself: take control first")
        self._check_may_decide(op)
        how = "automation retries the step" if mode == "retry" else "human completed the step; automation verifies and continues"
        self._decide(iv, Decision(mode, op, note), f"handed back: {how}")

    def abort(self, iv_id: str, operator: str, note: str = "") -> None:
        iv, op = self._open(iv_id), self._op(operator)
        self._check_may_decide(op)
        self._decide(iv, Decision("abort", op, note), "operator aborted the run")

    def record_human(self, payload: dict, frame: str = "") -> None:
        if self.state != Control.HUMAN or self.current is None:
            return
        entry = self.redactor.obj({**payload, "frame": frame})
        entry.update(ts=now_iso(), operator=self.holder)
        self.current.human_actions.append(entry)
        self.store.save(self.current)

    def snapshot(self) -> dict:
        return {"state": self.state.value, "holder": self.holder,
                "intervention": asdict(self.current) if self.current else None}

    # ── internals ──
    def _open(self, iv_id: str) -> Intervention:
        if self.current is None or self.current.id != iv_id:
            raise ControlError(f"intervention {iv_id} is not the open request")
        return self.current

    @staticmethod
    def _op(operator: str) -> str:
        op = (operator or "").strip()
        if not op or len(op) > 40:
            raise ControlError("an operator name (1-40 chars) is required: every decision is attributed")
        return op

    def _check_may_decide(self, op: str) -> None:
        if self.state == Control.HUMAN and self.holder != op:
            raise ControlError(f"session is held by {self.holder}; only they can hand it back")

    def _decide(self, iv: Intervention, d: Decision, why: str) -> None:
        iv.state = "aborted" if d.action == "abort" else "resolved"
        iv.decision = {"action": d.action, "operator": d.operator, "note": d.note, "ts": now_iso()}
        self._move(Control.AUTOMATION, "automation", why)
        self._decision.set_result(d)

    def _move(self, to: Control, holder: str, why: str) -> None:
        entry = {"ts": now_iso(), "from": self.state.value, "to": to.value, "holder": holder, "why": why}
        self.state, self.holder = to, holder
        if self.current is not None:
            self.current.control_log.append(entry)
            self.store.save(self.current)
