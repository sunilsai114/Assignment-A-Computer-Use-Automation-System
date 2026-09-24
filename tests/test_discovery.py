"""Discovery loop + recorder, driven by a scripted model against the live mock bank.

The scripted model picks elements by what a person would call them, exactly as the real model must; refs
change every turn. These tests prove the loop/recorder/guardrails; the real LLM run is evidence, not a test.
"""
import json
from pathlib import Path

import pytest

from cua.agent.loop import DiscoveryAgent
from cua.agent.model import ScriptedClient
from cua.schema.capability import Action
from cua.schema.result import FailureCategory as FC, Status
from cua.surface.web import WebSurface

pytestmark = pytest.mark.asyncio(loop_scope="function")

SIGN_IN = [
    ("type_text", {"name": "Operator ID"}, {"text": "{{secret:HERITAGE_USER}}", "intent": "Enter operator id"}),
    ("type_text", {"name": "Passcode"}, {"text": "{{secret:HERITAGE_PASS}}", "intent": "Enter passcode"}),
    ("click", {"name": "Sign On"}, {"intent": "Sign on"}),
]
LOOKUP = [
    *SIGN_IN,
    ("type_text", {"name": "Member Number"}, {"text": "{{member_id}}", "intent": "Enter member number"}),
    ("click", {"name": "Go"}, {"intent": "Search for the member"}),
    ("read_output", {"row_text": "Savings", "col": 2}, {"output": "savings_balance", "intent": "Read savings balance"}),
    ("done", {}, {"summary": "Read the savings balance"}),
]
SUBACCOUNT = [
    *SIGN_IN,
    ("type_text", {"name": "Member Number"}, {"text": "{{member_id}}", "intent": "Enter member number"}),
    ("click", {"name": "Go"}, {"intent": "Search for the member"}),
    ("click", {"name": "New Sub-Account"}, {"intent": "Open the new sub-account form"}),
    ("select_option", {"name": "Product Type"}, {"option": "{{product}}", "intent": "Choose product"}),
    ("type_text", {"name": "Opening Deposit"}, {"text": "{{opening_deposit}}", "intent": "Enter opening deposit"}),
    ("type_text", {"name": "Nickname"}, {"text": "{{nickname}}", "intent": "Enter nickname"}),
    ("click", {"name": "Continue"}, {"intent": "Continue to review"}),
    ("click", {"name": "Submit Application"}, {"intent": "Submit the application"}),  # must be refused
    ("done", {}, {"summary": "Reached the review screen"}),
]
SUB_INPUTS = {"member_id": ("string", "12345"), "product": ("enum", "Savings"),
              "opening_deposit": ("money", "50"), "nickname": ("string", "Rainy day")}


@pytest.fixture
def discover(server, browser, policy, tmp_path):
    async def run(script, *, inputs, outputs, cap_id="memberserv.discovered", max_turns=None):
        surface = await WebSurface.open(browser, server, policy)
        agent = DiscoveryAgent(surface, ScriptedClient(script), policy, runs_dir=tmp_path, max_turns=max_turns)
        # a realistic goal: it names the example value, which must not end up in the artifact
        return await agent.run(goal="Look up member 12345 on Heritage MemberServ", start="/heritage/", inputs=inputs, outputs=outputs, cap_id=cap_id,
                               app="memberserv", variant="heritage", secret_names=["HERITAGE_USER", "HERITAGE_PASS"])
    return run


def evidence_text(result) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in Path(result.evidence_dir).glob("*.json*"))


async def test_discovery_records_a_capability_that_replays(discover, replay):
    r = await discover(LOOKUP, inputs={"member_id": ("string", "12345")}, outputs={"savings_balance": "money"})
    assert r.status == "completed", r.reason
    cap = r.capability
    assert cap.meta.status == "draft" and cap.meta.created_by == r.run_id

    # sign-in became the auth flow, by env-var name only, and replay resumes after it
    assert [s.value.secret_env for s in cap.auth.steps if s.value] == ["HERITAGE_USER", "HERITAGE_PASS"]
    assert cap.auth.resume_from == cap.steps[1].id
    typed = next(s for s in cap.steps if s.action == Action.type)
    assert typed.value.param == "member_id"
    # the search's checkpoint was templated, not baked
    go = next(s for s in cap.steps if s.intent == "Search for the member")
    assert go.post[0].text == "Member {{member_id}}"
    assert go.target.locators[0].kind == "role" and go.target.locators[0].robustness == "high"

    blob = cap.to_json()
    for leaked in ("teller1", "demo-only", "12345", "4,821.37"):
        assert leaked not in blob

    # the production path: deterministic replay of what the model discovered, with a DIFFERENT input
    for member, balance in (("12345", "4821.37"), ("20001", "15300.00")):
        res = await replay(cap, {"member_id": member})
        assert res.status == Status.SUCCESS, res.failure
        assert res.outputs == {"savings_balance": balance}


async def test_draft_has_no_business_outcomes_until_reviewed(discover, replay):
    """One happy-path run cannot know what 'not found' looks like. The draft fails loudly instead of guessing."""
    r = await discover(LOOKUP, inputs={"member_id": ("string", "12345")}, outputs={"savings_balance": "money"})
    res = await replay(r.capability, {"member_id": "00000"})
    assert res.status == Status.FAILED and res.failure.category == FC.checkpoint_mismatch
    assert "Member 00000" in res.failure.expected


async def test_irreversible_step_is_refused_and_not_recorded(discover, replay, submitted):
    r = await discover(SUBACCOUNT, inputs=SUB_INPUTS, outputs={}, cap_id="memberserv.open_subaccount_discovered")
    assert r.status == "completed", r.reason
    assert submitted() == []
    events = [json.loads(line) for line in (Path(r.evidence_dir) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "policy_refused" for e in events)
    cap = r.capability
    assert "Submit" not in cap.to_json() and cap.success.text == "Review Sub-Account Application"
    product = next(p for p in cap.inputs if p.name == "product")
    assert product.type.value == "enum" and product.enum == ["Savings", "Checking", "Money Market"]
    res = await replay(cap, {"member_id": "20001", "product": "Checking", "opening_deposit": "75", "nickname": "Car"})
    assert res.status == Status.SUCCESS, res.failure
    assert submitted() == []


async def test_repeating_without_progress_escalates(discover):
    loop = [("click", {"name": "Member Inquiry"}, {"intent": "Open member inquiry"})] * 6  # never signs in
    r = await discover(loop, inputs={}, outputs={})
    assert r.status == "needs_human" and "repeating" in r.reason and r.intervention_id


async def test_model_cannot_use_undeclared_credentials_or_invent_inputs(discover):
    bad = [("type_text", {"name": "Operator ID"}, {"text": "{{secret:ROOT_PASSWORD}}"}),
           ("type_text", {"name": "Operator ID"}, {"text": "{{ssn}}"}),
           ("escalate", {}, {"reason": "cannot sign in"})]
    r = await discover(bad, inputs={}, outputs={})
    assert r.status == "needs_human"
    events = evidence_text(r)
    assert "not available" in events and "unknown placeholder" in events


async def test_evidence_keeps_screen_data_out(discover):
    script = [*LOOKUP[:5],
              ("read_output", {"row_text": "Name", "col": 1}, {"output": "member_name", "intent": "Read the name"}),
              ("done", {}, {"summary": "done"})]
    r = await discover(script, inputs={"member_id": ("string", "12345")}, outputs={"member_name": "string"})
    assert r.status == "completed", r.reason
    text = evidence_text(r)
    for leaked in ("Jordan Rivera", "123-45-6789", "j.rivera@example.com", "teller1", "demo-only"):
        assert leaked not in text
    assert r.outputs["member_name"] == "Jordan Rivera"  # the caller still gets it
    assert "Jordan Rivera" not in r.capability.to_json()
