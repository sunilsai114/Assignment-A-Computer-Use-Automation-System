"""Deterministic replay: no LLM in the loop. Same artifact + same inputs -> same steps -> same result.

Per-step classification order (the heart of the error model), applied whenever a step's expectation
is not (yet) met:
    1. declared business OUTCOME   -> terminal BUSINESS_OUTCOME (a legitimate answer, not an error)
    2. known INTERSTITIAL          -> dismiss, resume where the artifact says (recorded as a recovery)
    3. APP ERROR (5xx / marker)    -> terminal FAILED. Never blindly retried: re-submitting could double-apply.
    4. SESSION expired             -> re-authenticate once, restart the flow (state was lost)
    5. otherwise keep polling until the step deadline, then FAILED with expected-vs-observed.
Irreversible steps never run unattended: they yield NEEDS_HUMAN unless explicitly pre-approved.

Attended mode (a SessionController is attached): instead of ending on NEEDS_HUMAN or on a failure a person
could fix, the run pauses on the same live session and waits for an operator (see cua/handoff/controller.py).
"""
import asyncio
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from cua.evidence.recorder import EvidenceRecorder
from cua.handoff.controller import SessionController
from cua.handoff.intervention import InterventionStore
from cua.policy.engine import Policy
from cua.policy.redact import MASK
from cua.replay.conditions import describe, fill, holds
from cua.schema.capability import Action, Capability, InputError, Step, coerce_inputs, resolve_variant
from cua.schema.result import (
    Failure, FailureCategory as FC, OutcomeInfo, Recovery, RecoveryKind as RK, ReplayResult, Status,
)
from cua.surface.base import NavigationBlocked, SurfaceError, TargetNotFound

POLL_S = 0.15
TRANSIENT_WAIT_MS = 1500
SESSION_GRACE_S = 0.8  # an unmet step must persist this long before we suspect an expired session


class MissingSecret(Exception):
    pass


class _Run:
    def __init__(self, cap: Capability, params: dict[str, str], rec: EvidenceRecorder, run_id: str):
        self.cap, self.params, self.rec, self.run_id = cap, params, rec, run_id
        self.outputs: dict = {}
        self.recoveries: list[Recovery] = []
        self.steps_executed = 0
        self.dismissals = 0
        self.reauths = 0
        self.signed_in = False
        self.handoffs = 0
        self.approved: set[str] = set()  # steps a human approved during this run only
        self.t0 = time.monotonic()
        self.deadline = self.t0  # set in run(); pushed back by time spent waiting on a human


class Replayer:
    def __init__(self, surface, policy: Policy, runs_dir: Path = Path("runs"),
                 secrets: Mapping[str, str] | None = None, approvals: frozenset[str] = frozenset(),
                 controller: SessionController | None = None, handoff_timeout_s: float | None = None):
        self.surface, self.policy = surface, policy
        self.runs_dir = Path(runs_dir)
        self.secrets = secrets if secrets is not None else os.environ
        self.approvals = approvals
        self.controller = controller
        self.handoff_timeout_s = handoff_timeout_s or policy.limits["handoff_timeout_s"]
        self.interventions = controller.store if controller else InterventionStore(self.runs_dir)

    # ───────────────────────── public entry ─────────────────────────
    async def run(self, cap: Capability, inputs: dict, variant: str | None = None) -> ReplayResult:
        run_id = f"run-{uuid.uuid4().hex[:8]}"
        rec = EvidenceRecorder(self.runs_dir, run_id, self.policy.redactor)
        st = _Run(cap, {}, rec, run_id)
        try:
            st.cap = resolve_variant(cap, variant) if variant else cap
            st.params = coerce_inputs(st.cap, inputs)
        except (InputError, KeyError) as e:
            return self._finish(st, ReplayResult(
                status=Status.FAILED, **self._ids(st), failure=Failure(
                    step_id=None, category=FC.invalid_input, expected="inputs satisfying the capability contract",
                    observed=str(e).strip("'\""))))
        rec.log("run_start", capability=st.cap.meta.id, version=st.cap.meta.version,
                variant=st.cap.meta.variant, inputs=self._loggable_inputs(st))
        st.deadline = time.monotonic() + self.policy.limits["run_timeout_s"]
        try:
            res = await self._execute(st)
        except Exception as e:  # noqa: BLE001  a bug must surface as a debuggable failure, never a crash
            res = await self._fail(st, None, FC.unexpected_state, "no internal error", f"{type(e).__name__}: {e}")
        return self._finish(st, res)

    # ───────────────────────── plumbing ─────────────────────────
    @staticmethod
    def _ids(st: _Run) -> dict:
        return dict(capability_id=st.cap.meta.id, capability_version=st.cap.meta.version,
                    variant=st.cap.meta.variant, run_id=st.run_id)

    def _loggable_inputs(self, st: _Run) -> dict:
        sensitive = {p.name for p in st.cap.inputs if p.sensitive}
        return {k: (MASK if k in sensitive else v) for k, v in st.params.items()}

    def _finish(self, st: _Run, res: ReplayResult) -> ReplayResult:
        res.duration_ms = int((time.monotonic() - st.t0) * 1000)
        res.steps_executed = st.steps_executed
        res.recoveries = st.recoveries
        res.evidence_dir = str(st.rec.dir)
        sensitive = {o.name for o in st.cap.outputs if o.sensitive}
        st.rec.write_json("result.json", res.for_log(sensitive))
        st.rec.log("run_end", status=res.status.value)
        return res

    def _result(self, st: _Run, status: Status, **kw) -> ReplayResult:
        return ReplayResult(status=status, **self._ids(st), **kw)

    async def _evidence(self, st: _Run, tag: str) -> str | None:
        ref = await st.rec.screenshot(tag, self.surface)
        await st.rec.snapshot(tag, self.surface)
        return ref

    async def _fail(self, st: _Run, step: Step | None, cat: FC, expected: str, observed: str) -> ReplayResult:
        ref = await self._evidence(st, f"failure-{step.id if step else 'run'}")
        st.rec.log("failure", step=step.id if step else None, category=cat.value, expected=expected, observed=observed)
        return self._result(st, Status.FAILED, failure=Failure(
            step_id=step.id if step else None, category=cat, expected=expected,
            observed=self.policy.redactor.text(observed), evidence_ref=ref))

    async def _needs_human(self, st: _Run, step: Step, reason: str, kind: str = "approval") -> ReplayResult:
        ref = await self._evidence(st, f"intervention-{step.id}")
        iv = self.interventions.create(run_id=st.run_id, capability_id=st.cap.meta.id, step_id=step.id, kind=kind,
                                       reason=reason, url=await self.surface.url(), screenshot=ref,
                                       evidence_dir=str(st.rec.dir))
        st.rec.log("needs_human", step=step.id, reason=reason, intervention=iv.id)
        return self._result(st, Status.NEEDS_HUMAN, intervention_id=iv.id)

    def _recover(self, st: _Run, kind: RK, step_id: str, detail: str, attempt: int = 1) -> None:
        st.recoveries.append(Recovery(kind=kind, step_id=step_id, detail=detail, attempt=attempt))
        st.rec.log("recovery", kind=kind.value, step=step_id, detail=detail)

    # ───────────────────────── execution ─────────────────────────
    async def _execute(self, st: _Run) -> ReplayResult:
        steps = st.cap.steps
        limit = self.policy.limits["max_steps"] * 4  # bound total work incl. recoveries
        for c in st.cap.preconditions:
            if not await holds(c, self.surface, st.params):
                return await self._fail(st, None, FC.precondition, describe(c, st.params), "precondition not met")
        i, budget = 0, limit
        while i < len(steps):
            budget -= 1
            if budget < 0:
                return await self._fail(st, None, FC.unexpected_state, "flow to finish within step budget", "step budget exhausted")
            if time.monotonic() > st.deadline:
                return await self._fail(st, steps[i], FC.timeout, f"run to finish within {self.policy.limits['run_timeout_s']}s "
                                        "(time waiting on a human excluded)", "run deadline passed")
            nxt = await self._step(st, steps, i)
            if isinstance(nxt, ReplayResult):
                if not self._escalatable(nxt):
                    return nxt
                nxt = await self._handoff(st, steps, i, nxt)
                if isinstance(nxt, ReplayResult):
                    return nxt
            i = nxt
        deadline = time.monotonic() + self.policy.limits["action_timeout_ms"] / 1000
        while not await holds(st.cap.success, self.surface, st.params):
            if time.monotonic() > deadline:
                return await self._fail(st, None, FC.checkpoint_mismatch, describe(st.cap.success, st.params),
                                        await self._observed())
            await asyncio.sleep(POLL_S)
        missing = [o.name for o in st.cap.outputs if o.name not in st.outputs]
        if missing:
            return await self._fail(st, None, FC.unexpected_state, f"outputs {missing}", "not produced")
        st.rec.log("success", outputs=sorted(st.outputs))
        return self._result(st, Status.SUCCESS, outputs=dict(st.outputs))

    async def _observed(self) -> str:
        text = " ".join((await self.surface.page_text()).split())
        return f"url={await self.surface.url()} text={text[:300]!r}"

    def _value(self, st: _Run, step: Step) -> str:
        v = step.value
        if v.literal is not None:
            return v.literal
        if v.param is not None:
            return st.params[v.param]
        secret = self.secrets.get(v.secret_env)
        if not secret:
            raise MissingSecret(f"secret env var '{v.secret_env}' is not set")
        self.policy.redactor.add_secret_values(secret)
        return secret

    def _shown(self, st: _Run, step: Step) -> str | None:
        """Value as it may appear in logs: never a secret, never a sensitive param."""
        v = step.value
        if v is None:
            return None
        if v.secret_env:
            return f"<secret:{v.secret_env}>"
        if v.param and any(p.name == v.param and p.sensitive for p in st.cap.inputs):
            return MASK
        return v.literal if v.literal is not None else st.params[v.param]

    async def _act(self, st: _Run, step: Step) -> None:
        s = self.surface
        s.reset_status()
        a = step.action
        if a == Action.navigate:
            await s.goto(self._value(st, step))
        elif a == Action.click:
            await s.click(step.target)
        elif a == Action.type:
            await s.fill(step.target, self._value(st, step))
        elif a == Action.select:
            await s.select(step.target, self._value(st, step))
        elif a == Action.read:
            st.outputs[step.output] = self._parse(step, await s.read(step.target))
        elif a == Action.wait:
            await asyncio.sleep(min((step.timeout_ms or 500) / 1000, 5))
        if step.target and s.last_locator_index > 0:
            self._recover(st, RK.locator_fallback, step.id,
                          f"primary locator missed; fallback #{s.last_locator_index} matched (possible UI drift)")
        s.last_locator_index = 0

    @staticmethod
    def _parse(step: Step, text: str) -> str:
        if step.parse == "money":
            try:
                return str(Decimal(re.sub(r"[^\d.\-]", "", text)))
            except InvalidOperation as e:
                raise SurfaceError(f"expected a money amount, read {text!r}") from e
        return text

    async def _classify(self, st: _Run, session_ok: bool):
        cap, s = st.cap, self.surface
        for o in cap.outcomes:
            if await holds(o.detected_by, s, st.params):
                return "outcome", o
        for it in cap.interstitials:
            if await holds(it.detected_by, s, st.params):
                return "interstitial", it
        if s.last_status and s.last_status >= 500:
            return "app_error", f"HTTP {s.last_status}"
        text = (await s.page_text()).lower()
        for m in self.policy.app_error_markers:
            if m in text:
                return "app_error", f"page shows error text matching '{m}'"
        if session_ok and cap.auth and await holds(cap.auth.session_expired_when, s, st.params):
            # A stale page can still be on screen while the next one loads. Only trust the signal if it
            # survives the page settling: otherwise a healthy session gets a needless re-login.
            limit = self.policy.limits["action_timeout_ms"]
            await s.settle(limit)
            await asyncio.sleep(0.25)
            await s.settle(limit)
            if await holds(cap.auth.session_expired_when, s, st.params):
                return "session", None
        return None

    async def _step(self, st: _Run, steps: list[Step], i: int) -> int | ReplayResult:
        step, s = steps[i], self.surface
        risk = self.policy.risk_of(step)
        st.rec.log("step_start", step=step.id, intent=step.intent, action=step.action.value,
                   risk=risk.value, value=self._shown(st, step))
        verdict = self.policy.check_step(step, await s.url())
        if verdict.blocked:
            return await self._fail(st, step, FC.policy_blocked, "action permitted by policy", verdict.reason)
        if verdict.needs_approval and step.id not in self.approvals and step.id not in st.approved:
            return await self._needs_human(st, step, verdict.reason)
        for c in step.pre:
            if not await holds(c, s, st.params):
                return await self._fail(st, step, FC.precondition, describe(c, st.params), await self._observed())

        t_start = time.monotonic()
        deadline = t_start + (step.timeout_ms or self.policy.limits["action_timeout_ms"]) / 1000
        missed = False
        while True:
            try:
                await self._act(st, step)
                break
            except TargetNotFound as e:
                if not missed:
                    missed = True
                    st.rec.log("target_miss", step=step.id, detail=str(e), url=await s.url())
                if sig := await self._classify(st, session_ok=time.monotonic() - t_start > SESSION_GRACE_S):
                    return await self._handle(st, steps, i, sig)
                if time.monotonic() > deadline:
                    return await self._fail(st, step, FC.target_not_found, f"a unique control for '{step.intent}'", str(e))
                await asyncio.sleep(POLL_S)
            except MissingSecret as e:
                return await self._fail(st, step, FC.precondition, "required secret available in env", str(e))
            except NavigationBlocked as e:
                return await self._fail(st, step, FC.policy_blocked, "request permitted by policy", str(e))
            except SurfaceError as e:
                return await self._fail(st, step, FC.unexpected_state, f"'{step.action.value}' to succeed", str(e))

        t_act = time.monotonic()
        await s.settle(self.policy.limits["action_timeout_ms"])
        st.steps_executed += 1
        if step.post:
            while True:
                if all([await holds(c, s, st.params) for c in step.post]):
                    break
                if sig := await self._classify(st, session_ok=time.monotonic() - t_start > SESSION_GRACE_S):
                    return await self._handle(st, steps, i, sig)
                if time.monotonic() > deadline:
                    return await self._fail(st, step, FC.checkpoint_mismatch,
                                            " AND ".join(describe(c, st.params) for c in step.post), await self._observed())
                await asyncio.sleep(POLL_S)
            waited = int((time.monotonic() - t_act) * 1000)
            if waited > TRANSIENT_WAIT_MS:
                self._recover(st, RK.retry_transient, step.id, f"page took {waited}ms to reach the expected state")
        elif sig := await self._classify(st, session_ok=False):
            return await self._handle(st, steps, i, sig)
        st.rec.log("step_ok", step=step.id)
        return i + 1

    async def _handle(self, st: _Run, steps: list[Step], i: int, sig) -> int | ReplayResult:
        kind, what = sig
        step = steps[i]
        if kind == "outcome":
            st.rec.log("business_outcome", code=what.code, step=step.id)
            return self._result(st, Status.BUSINESS_OUTCOME, outcome=OutcomeInfo(
                code=what.code, description=what.description, data=what.returns, step_id=step.id))
        if kind == "app_error":
            return await self._fail(st, step, FC.app_error, "a normal application response", what)
        if kind == "interstitial":
            if st.dismissals >= self.policy.limits["max_interstitials"]:
                return await self._fail(st, step, FC.unexpected_state, "interstitials to clear",
                                        f"'{what.name}' keeps reappearing")
            verdict = self.policy.check_step(what.dismiss, await self.surface.url())
            if verdict.blocked or verdict.needs_approval:
                return await self._needs_human(st, what.dismiss, verdict.reason or "dismissal needs approval")
            st.dismissals += 1
            try:
                await self._act(st, what.dismiss)
            except SurfaceError as e:
                return await self._fail(st, what.dismiss, FC.target_not_found, "dismiss control for known dialog", str(e))
            await self.surface.settle(self.policy.limits["action_timeout_ms"])
            self._recover(st, RK.interstitial_dismissed, step.id, f"dismissed '{what.name}'", st.dismissals)
            return next((k for k, s in enumerate(steps) if s.id == what.resume_from), i) if what.resume_from else i
        if kind == "session":
            return await self._reauth(st, step)
        raise AssertionError(kind)

    async def _reauth(self, st: _Run, step: Step) -> int | ReplayResult:
        auth = st.cap.auth
        if st.reauths >= self.policy.limits["max_reauths"]:
            return await self._fail(st, step, FC.session_expired_unrecoverable, "a live session",
                                    "session expired again after re-authenticating")
        st.reauths += 1
        for a in auth.steps:
            if (v := self.policy.check_step(a, await self.surface.url())).blocked:
                return await self._fail(st, a, FC.policy_blocked, "auth step permitted", v.reason)
            try:
                await self._act(st, a)
            except MissingSecret as e:
                return await self._fail(st, a, FC.session_expired_unrecoverable, "credentials available in env", str(e))
            except SurfaceError as e:
                return await self._fail(st, a, FC.session_expired_unrecoverable, "re-authentication to work", str(e))
        await self.surface.settle(self.policy.limits["action_timeout_ms"])
        deadline = time.monotonic() + self.policy.limits["action_timeout_ms"] / 1000
        while not await holds(auth.logged_in_when, self.surface, st.params):
            if time.monotonic() > deadline:
                if st.reauths < self.policy.limits["max_reauths"]:
                    # The session can die again right after sign-in (it did on the landing page). Restart
                    # the flow: the next unmet step re-detects expiry and spends the remaining budget.
                    st.rec.log("reauth_not_confirmed", step=step.id, attempt=st.reauths)
                    st.signed_in = True  # we did sign in; losing it again is a real recovery
                    return 0
                return await self._fail(st, step, FC.session_expired_unrecoverable,
                                        describe(auth.logged_in_when, st.params), await self._observed())
            await asyncio.sleep(POLL_S)
        if st.signed_in:
            self._recover(st, RK.reauthenticated, step.id, "session was lost; signed in again and restarted the flow")
        else:  # a cold session needing a sign-in is the normal start, not a recovery
            st.rec.log("signed_in", step=step.id)
        st.signed_in = True
        st.outputs.clear()
        if auth.resume_from:
            return next(k for k, s in enumerate(st.cap.steps) if s.id == auth.resume_from)
        return 0

    # ───────────────────────── human handoff ─────────────────────────
    # Failures a person can plausibly fix in the live session. Deliberately excluded: bad input (the caller's
    # problem), policy blocks (a human must not override the allowlist), app errors (retrying could
    # double-apply a write) and run timeouts.
    HUMAN_FIXABLE = {FC.target_not_found, FC.checkpoint_mismatch, FC.unexpected_state, FC.precondition,
                     FC.session_expired_unrecoverable}

    def _escalatable(self, res: ReplayResult) -> bool:
        if self.controller is None:
            return False
        if res.status == Status.NEEDS_HUMAN:
            return True
        return (res.status == Status.FAILED and res.failure.category in self.HUMAN_FIXABLE
                and res.failure.step_id is not None)

    async def _handoff(self, st: _Run, steps: list[Step], i: int, res: ReplayResult) -> int | ReplayResult:
        step = steps[i]
        if st.handoffs >= self.policy.limits["max_handoffs"]:
            st.rec.log("handoff_limit", step=step.id)
            return res
        st.handoffs += 1
        if res.status == Status.NEEDS_HUMAN:
            iv = self.interventions.load(res.intervention_id)
        else:
            f = res.failure
            iv = self.interventions.create(
                run_id=st.run_id, capability_id=st.cap.meta.id, step_id=f.step_id, kind="stuck",
                reason=f"{f.category.value}: expected {f.expected}; observed {f.observed}"[:500],
                url=await self.surface.url(), screenshot=f.evidence_ref, evidence_dir=str(st.rec.dir))
        st.rec.log("handoff_requested", intervention=iv.id, kind=iv.kind, step=step.id, reason=iv.reason)
        waited_from = time.monotonic()
        decision = await self.controller.escalate(iv, self.handoff_timeout_s)
        st.deadline += time.monotonic() - waited_from
        if decision is None:
            st.rec.log("handoff_expired", intervention=iv.id)
            if res.status == Status.FAILED:
                res.intervention_id = iv.id
            return res
        saved = self.interventions.load(iv.id)
        st.rec.log("handoff_resolved", intervention=iv.id, action=decision.action, operator=decision.operator,
                   human_actions=len(saved.human_actions))
        if decision.action == "session_lost":
            st.rec.log("failure", step=step.id, category=FC.unexpected_state.value, observed=decision.note)
            return self._result(st, Status.FAILED, intervention_id=iv.id, failure=Failure(
                step_id=step.id, category=FC.unexpected_state, expected="the live session to survive the handoff",
                observed=decision.note, evidence_ref=iv.screenshot))
        await self._evidence(st, f"handback-{step.id}")
        if decision.action == "abort":
            return await self._fail(st, step, FC.aborted_by_operator, "operator to hand control back",
                                    f"aborted by {decision.operator}: {decision.note or 'no reason given'}")
        self._recover(st, RK.human_intervention, step.id,
                      f"{decision.action} by {decision.operator}; {len(saved.human_actions)} human action(s) recorded")
        if decision.action == "approve":
            st.approved.add(step.id)
            return i
        if decision.action == "retry":
            return i
        # skip: the human says they did this step. Verify rather than trust.
        await self.surface.settle(self.policy.limits["action_timeout_ms"])
        deadline = time.monotonic() + self.policy.limits["action_timeout_ms"] / 1000
        while not all([await holds(c, self.surface, st.params) for c in step.post]):
            if time.monotonic() > deadline:
                return await self._fail(st, step, FC.checkpoint_mismatch,
                                        "after human handoff: " + " AND ".join(describe(c, st.params) for c in step.post),
                                        await self._observed())
            await asyncio.sleep(POLL_S)
        if step.output:  # the human did the step but the value is still needed
            try:
                await self._act(st, step)
            except SurfaceError as e:
                return await self._fail(st, step, FC.target_not_found, f"output '{step.output}' readable after handoff", str(e))
        st.rec.log("step_ok", step=step.id, by=decision.operator)
        return i + 1
