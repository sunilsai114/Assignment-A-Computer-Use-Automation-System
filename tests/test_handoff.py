"""Human-in-the-loop: irreversible gating, pause on the live session, operator control, hand-back."""
import asyncio
import json
import time

import httpx
import pytest

from cua.handoff.controller import Control, ControlError, SessionController
from cua.handoff.intervention import InterventionStore
from cua.handoff.operator import build_operator_app
from cua.schema.capability import Capability, Risk
from cua.schema.examples import heritage_open_subaccount
from cua.schema.result import FailureCategory as FC, RecoveryKind as RK, Status

pytestmark = pytest.mark.asyncio(loop_scope="function")
INPUTS = {"member_id": "12345", "product": "Savings", "opening_deposit": "50", "nickname": "Rainy day"}


@pytest.fixture
def cap():
    return heritage_open_subaccount()


async def wait_paused(ctrl: SessionController, timeout: float = 30):
    t = time.monotonic()
    while not (ctrl.state == Control.PAUSED and ctrl.current):
        assert time.monotonic() - t < timeout, "replay never paused for a human"
        await asyncio.sleep(0.05)
    return ctrl.current


async def frame_text(surface, needle: str, timeout: float = 10):
    t = time.monotonic()
    while needle not in await surface.page_text("m"):
        assert time.monotonic() - t < timeout, f"'{needle}' never appeared"
        await asyncio.sleep(0.1)


def events(res) -> list[dict]:
    from pathlib import Path
    return [json.loads(line) for line in (Path(res.evidence_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_capability_declares_its_risk(cap):
    assert cap.max_risk == Risk.irreversible and cap.approval_required_steps == ["submit_application"]


# ── unattended: the irreversible step never runs ──
async def test_unattended_run_stops_at_review_screen(replay, cap, submitted):
    r = await replay(cap, INPUTS)
    assert r.status == Status.NEEDS_HUMAN and r.intervention_id
    ok = [e["step"] for e in events(r) if e["event"] == "step_ok"]
    assert "continue_to_review" in ok and "submit_application" not in ok
    assert submitted() == []


async def test_below_minimum_deposit_is_a_business_outcome(replay, cap, submitted):
    r = await replay(cap, {**INPUTS, "opening_deposit": "5"})
    assert r.status == Status.BUSINESS_OUTCOME and r.outcome.code == "deposit_below_minimum"
    assert submitted() == []


# ── state machine rules (no browser) ──
async def test_control_state_machine(tmp_path, policy):
    ctrl = SessionController(InterventionStore(tmp_path), policy.redactor)
    iv = ctrl.store.create(run_id="r", capability_id="c", step_id="s", reason="stuck", url="u",
                           screenshot=None, kind="stuck")
    waiting = asyncio.create_task(ctrl.escalate(iv, 5))
    await asyncio.sleep(0.05)
    assert ctrl.state == Control.PAUSED
    with pytest.raises(ControlError):
        ctrl.assert_automation()                 # automation is locked out while paused
    with pytest.raises(ControlError):
        ctrl.approve(iv.id, "alice")             # only approval requests can be approved
    with pytest.raises(ControlError):
        ctrl.resume(iv.id, "alice", "skip")      # 'I did it' requires having taken control
    with pytest.raises(ControlError):
        ctrl.claim(iv.id, "  ")                  # every decision is attributed
    ctrl.claim(iv.id, "alice")
    with pytest.raises(ControlError):
        ctrl.claim(iv.id, "bob")                 # one holder at a time
    with pytest.raises(ControlError):
        ctrl.resume(iv.id, "bob", "retry")       # only the holder hands back
    ctrl.record_human({"kind": "input", "text": "SSN", "value": "123-45-6789"}, frame="m")
    ctrl.resume(iv.id, "alice", "skip")
    d = await waiting
    assert (d.action, d.operator, ctrl.state) == ("skip", "alice", Control.AUTOMATION)
    saved = ctrl.store.load(iv.id)
    assert "123-45-6789" not in json.dumps(saved.human_actions)  # human input is redacted too
    assert [e["to"] for e in saved.control_log] == ["PAUSED", "HUMAN", "AUTOMATION"]


# ── attended: live session, real operator paths ──
async def test_operator_approves_through_console_api(attended, cap, submitted):
    task = attended.run(cap, INPUTS)
    iv = await wait_paused(attended.ctrl)
    assert iv.kind == "approval" and iv.step_id == "submit_application"
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_operator_app(attended.ctrl)), base_url="http://op")
    async with api:
        assert (await api.get("/api/state")).json()["state"] == "PAUSED"
        assert (await api.get(f"/api/interventions/{iv.id}/screenshot")).status_code == 200
        assert (await api.post(f"/api/interventions/{iv.id}/approve", json={"operator": ""})).status_code == 409
        assert (await api.post(f"/api/interventions/{iv.id}/approve", json={"operator": "alice"})).status_code == 200
    r = await task
    assert r.status == Status.SUCCESS, r.failure
    assert r.outputs["review_deposit"] == "50.00" and r.outputs["reference"].startswith("SA-")
    assert submitted() == ["12345"]
    assert RK.human_intervention in [x.kind for x in r.recoveries]
    assert attended.ctrl.store.load(iv.id).decision["operator"] == "alice"


async def test_operator_takes_over_the_same_session_and_hands_back(attended, cap, submitted):
    task = attended.run(cap, INPUTS)
    iv = await wait_paused(attended.ctrl)
    attended.ctrl.claim(iv.id, "bob")
    submit = next(s for s in cap.steps if s.id == "submit_application")
    with pytest.raises(ControlError):
        await attended.surface.click(submit.target)  # automation cannot touch the session bob holds

    # bob works in the live page directly: same cookies, same frames, not a fresh session
    m = attended.surface.page.frame(name="m")
    await m.get_by_role("button", name="Submit Application").click()
    await frame_text(attended.surface, "Application submitted")
    attended.ctrl.resume(iv.id, "bob", "skip")

    r = await task
    assert r.status == Status.SUCCESS, r.failure
    assert r.outputs["reference"].startswith("SA-") and submitted() == ["12345"]
    saved = attended.ctrl.store.load(iv.id)
    assert any(a["kind"] == "click" and a["text"] == "Submit Application" for a in saved.human_actions)
    assert all(a["operator"] == "bob" for a in saved.human_actions)
    assert not any(a["text"] == "Continue" for a in saved.human_actions)  # automation's own clicks are not "human"
    assert [e["to"] for e in saved.control_log] == ["PAUSED", "HUMAN", "AUTOMATION"]


async def test_human_hand_back_is_verified_not_trusted(attended, cap, submitted):
    task = attended.run(cap, INPUTS)
    iv = await wait_paused(attended.ctrl)
    attended.ctrl.claim(iv.id, "bob")
    attended.ctrl.resume(iv.id, "bob", "skip")  # claims it's done, but never submitted
    r = await task
    assert r.status == Status.FAILED and r.failure.category == FC.checkpoint_mismatch
    assert "after human handoff" in r.failure.expected and submitted() == []


async def test_stuck_replay_escalates_and_the_human_unblocks_it(attended, cap, submitted):
    d = cap.model_dump(mode="json")
    step = next(s for s in d["steps"] if s["id"] == "open_new_subaccount")
    step["target"]["locators"][0]["params"]["text"] = "Open New Account"  # the app no longer says this
    step["timeout_ms"] = 1500
    task = attended.run(Capability.model_validate(d), INPUTS, approvals={"submit_application"})
    iv = await wait_paused(attended.ctrl)
    assert iv.kind == "stuck" and iv.step_id == "open_new_subaccount" and "target_not_found" in iv.reason
    attended.ctrl.claim(iv.id, "carol")
    await attended.surface.page.frame(name="m").get_by_text("New Sub-Account", exact=True).click()
    await frame_text(attended.surface, "Open Sub-Account")
    attended.ctrl.resume(iv.id, "carol", "skip")
    r = await task
    assert r.status == Status.SUCCESS, r.failure
    saved = attended.ctrl.store.load(iv.id)
    assert any(a["text"] == "New Sub-Account" for a in saved.human_actions)  # what the fix looked like


async def test_unanswered_request_times_out_as_needs_human(attended, cap, submitted):
    r = await attended.run(cap, INPUTS, timeout=1.0)
    assert r.status == Status.NEEDS_HUMAN
    assert attended.ctrl.store.load(r.intervention_id).state == "expired"
    assert attended.ctrl.state == Control.AUTOMATION and submitted() == []


async def test_session_closed_during_handoff_fails_fast(attended, cap, submitted):
    task = attended.run(cap, INPUTS, timeout=60)
    iv = await wait_paused(attended.ctrl)
    t = time.monotonic()
    await attended.surface.page.close()  # e.g. someone closes the browser window
    r = await task
    assert time.monotonic() - t < 5, "must not wait out the handoff timeout"
    assert r.status == Status.FAILED and "closed" in r.failure.observed
    assert attended.ctrl.store.load(iv.id).state == "session_lost" and submitted() == []


async def test_operator_abort(attended, cap, submitted):
    task = attended.run(cap, INPUTS)
    iv = await wait_paused(attended.ctrl)
    attended.ctrl.abort(iv.id, "dana", "member changed their mind")
    r = await task
    assert r.status == Status.FAILED and r.failure.category == FC.aborted_by_operator
    assert "dana" in r.failure.observed and submitted() == []
