"""End-to-end replay against the live mock bank: every outcome class in the result contract."""
import json
from pathlib import Path

import pytest

from cua.schema.capability import Capability
from cua.schema.examples import heritage_lookup_member
from cua.schema.result import FailureCategory as FC, RecoveryKind as RK, Status

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture
def cap():
    return heritage_lookup_member()


def kinds(r):
    return [x.kind for x in r.recoveries]


async def test_success_returns_typed_outputs(replay, cap):
    r = await replay(cap, {"member_id": "12345"})
    assert r.status == Status.SUCCESS, r.failure
    assert r.outputs == {"savings_balance": "4821.37", "member_name": "Jordan Rivera"}
    assert r.recoveries == []  # a cold-session sign-in is the normal start, not a recovery


async def test_replay_is_deterministic(replay, cap):
    a = await replay(cap, {"member_id": "20001"})
    b = await replay(cap, {"member_id": "20001"})
    assert a.outputs == b.outputs == {"savings_balance": "15300.00", "member_name": "Priya Nair"}
    assert kinds(a) == kinds(b)


@pytest.mark.parametrize("member,code", [("00000", "member_not_found"), ("99999", "permission_denied"),
                                         ("abc", "invalid_member_number")])
async def test_business_outcomes_are_not_failures(replay, cap, member, code):
    r = await replay(cap, {"member_id": member})
    assert r.status == Status.BUSINESS_OUTCOME and r.failure is None
    assert r.outcome.code == code and r.outputs == {}


async def test_invalid_input_rejected_before_touching_app(replay, cap):
    r = await replay(cap, {"member_id": "12 34; DROP"})
    assert r.status == Status.FAILED and r.failure.category == FC.invalid_input
    assert r.steps_executed == 0


async def test_slow_load_is_waited_out_and_recorded(replay, cap, fault):
    fault("slow")
    r = await replay(cap, {"member_id": "12345"})
    assert r.status == Status.SUCCESS, r.failure
    assert RK.retry_transient in kinds(r)


async def test_interstitial_dismissed_and_flow_resumes(replay, cap, fault):
    fault("dialog")
    r = await replay(cap, {"member_id": "12345"})
    assert r.status == Status.SUCCESS, r.failure
    assert RK.interstitial_dismissed in kinds(r)
    assert r.outputs["savings_balance"] == "4821.37"


async def test_session_expiry_reauthenticates_and_restarts(replay, cap, fault):
    fault("expire")
    r = await replay(cap, {"member_id": "12345"})
    assert r.status == Status.SUCCESS, r.failure
    assert kinds(r).count(RK.reauthenticated) >= 1


async def test_app_error_is_a_hard_failure_with_evidence(replay, cap, fault):
    fault("error500")
    r = await replay(cap, {"member_id": "12345"})
    assert r.status == Status.FAILED
    f = r.failure
    assert f.category == FC.app_error and f.step_id == "submit_search"
    assert "500" in f.observed and f.expected
    ev = Path(r.evidence_dir)
    assert (ev / f.evidence_ref).exists() and list(ev.glob("failure-*.html"))


async def test_missing_credentials_fail_cleanly_without_leaking(replay, cap):
    r = await replay(cap, {"member_id": "12345"}, secrets={})
    assert r.status == Status.FAILED and r.failure.category == FC.session_expired_unrecoverable
    assert "HERITAGE_USER" in r.failure.observed


async def test_fallback_locator_is_reported_as_drift(replay, cap):
    d = cap.model_dump(mode="json")
    step = next(s for s in d["steps"] if s["id"] == "enter_member")
    step["target"]["locators"][0]["params"]["text"] = "Renamed Label"  # primary locator now misses
    r = await replay(Capability.model_validate(d), {"member_id": "12345"})
    assert r.status == Status.SUCCESS, r.failure
    assert RK.locator_fallback in kinds(r)


async def test_nova_variant_replays_same_contract(replay, cap):
    r = await replay(cap, {"member_id": "12345"}, variant="nova")
    assert r.status == Status.SUCCESS, r.failure
    assert r.outputs["savings_balance"] == "4821.37" and r.outputs["member_name"] == "Jordan Rivera"
    nf = await replay(cap, {"member_id": "00000"}, variant="nova")
    assert nf.outcome.code == "member_not_found"


async def test_logs_never_contain_pii_or_secrets(replay, cap):
    r = await replay(cap, {"member_id": "12345"})
    ev = Path(r.evidence_dir)
    blob = "".join(p.read_text(encoding="utf-8") for p in ev.glob("*") if p.suffix in (".jsonl", ".json"))
    for leaked in ("Jordan Rivera", "demo-only", "teller1", "123-45-6789"):
        assert leaked not in blob
    assert json.loads((ev / "result.json").read_text())["outputs"]["member_name"] == "●●●●"
    assert r.outputs["member_name"] == "Jordan Rivera"  # caller still receives the real value
