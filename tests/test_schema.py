import pytest
from pydantic import ValidationError

from cua.schema.capability import (
    Capability, InputError, Risk, Step, ValueRef, Action, coerce_inputs, resolve_variant,
)
from cua.schema.examples import heritage_lookup_member
from cua.schema.result import Failure, FailureCategory, OutcomeInfo, ReplayResult, Status, REDACTED


@pytest.fixture
def cap():
    return heritage_lookup_member()


def test_roundtrip_json(cap):
    assert Capability.from_json(cap.to_json()) == cap


def test_json_schema_exports(cap):
    schema = Capability.model_json_schema()
    assert "steps" in schema["properties"] and "outcomes" in schema["properties"]


def test_artifact_holds_no_raw_input_or_secret(cap):
    text = cap.to_json()
    assert "teller1" not in text and "demo-only" not in text and "12345" not in text
    assert "HERITAGE_USER" in text  # only the env var *name* is stored


def test_derived_risk(cap):
    assert cap.max_risk == Risk.safe and cap.approval_required_steps == []


def mutate(cap, fn):
    d = cap.model_dump(mode="json")
    fn(d)
    return Capability.model_validate(d)


def test_rejects_undeclared_param(cap):
    with pytest.raises(ValidationError, match="undeclared param"):
        mutate(cap, lambda d: d["steps"][2]["value"].update(param="nope"))


def test_rejects_placeholder_without_param(cap):
    with pytest.raises(ValidationError, match="placeholder"):
        mutate(cap, lambda d: d["success"]["conditions"][0].update(text="Member {{ghost}}"))


def test_secrets_only_in_auth(cap):
    d = cap.model_dump(mode="json")
    d["steps"][2]["value"] = {"secret_env": "HERITAGE_PASS"}
    with pytest.raises(ValidationError, match="secrets may only"):
        Capability.model_validate(d)


def test_output_must_be_produced(cap):
    with pytest.raises(ValidationError, match="never produced"):
        mutate(cap, lambda d: d["steps"].pop())  # drops read_name


def test_locator_requires_its_fields(cap):
    with pytest.raises(ValidationError, match="requires params"):
        mutate(cap, lambda d: d["steps"][1]["target"]["locators"][0].update(params={}))


def test_step_shape_rules():
    with pytest.raises(ValidationError, match="needs a value"):
        Step(id="a", intent="x", action=Action.navigate)
    with pytest.raises(ValidationError, match="exactly one"):
        ValueRef(literal="a", param="b")


def test_variant_resolution_keeps_contract(cap):
    nova = resolve_variant(cap, "nova")
    assert nova.meta.variant == "nova"
    assert [i.name for i in nova.inputs] == [i.name for i in cap.inputs]
    assert [o.name for o in nova.outputs] == [o.name for o in cap.outputs]
    assert [o.code for o in nova.outcomes] == [o.code for o in cap.outcomes]
    assert "open_inquiry" not in [s.id for s in nova.steps]
    assert nova.steps[0].value.literal == "/nova/customers"
    with pytest.raises(KeyError):
        resolve_variant(cap, "unknown")


def test_variant_cannot_reference_unknown_step(cap):
    with pytest.raises(ValidationError, match="unknown step"):
        mutate(cap, lambda d: d["variants"][0]["skip_steps"].append("ghost"))


def test_coerce_inputs(cap):
    assert coerce_inputs(cap, {"member_id": " 12345 "}) == {"member_id": "12345"}
    with pytest.raises(InputError, match="missing required"):
        coerce_inputs(cap, {})
    with pytest.raises(InputError, match="does not match"):
        coerce_inputs(cap, {"member_id": "12 34; DROP"})
    with pytest.raises(InputError, match="unknown input"):
        coerce_inputs(cap, {"member_id": "1", "extra": "x"})


# ── result contract ──
def base(**kw):
    return dict(capability_id="c", capability_version="1.0.0", variant="heritage", run_id="r1", **kw)


def test_result_status_consistency():
    ReplayResult(status=Status.SUCCESS, outputs={"a": 1}, **base())
    ReplayResult(status=Status.BUSINESS_OUTCOME, outcome=OutcomeInfo(code="member_not_found"), **base())
    with pytest.raises(ValidationError):
        ReplayResult(status=Status.BUSINESS_OUTCOME, **base())
    with pytest.raises(ValidationError):
        ReplayResult(status=Status.FAILED, **base())
    with pytest.raises(ValidationError):
        ReplayResult(status=Status.NEEDS_HUMAN, **base())
    with pytest.raises(ValidationError, match="only returned on SUCCESS"):
        ReplayResult(status=Status.FAILED, outputs={"a": 1}, failure=Failure(
            step_id="s", category=FailureCategory.timeout, expected="e", observed="o"), **base())


def test_for_log_masks_sensitive_outputs():
    r = ReplayResult(status=Status.SUCCESS, outputs={"member_name": "Jordan Rivera", "savings_balance": "4821.37"}, **base())
    logged = r.for_log({"member_name"})
    assert logged["outputs"]["member_name"] == REDACTED and logged["outputs"]["savings_balance"] == "4821.37"
    assert r.outputs["member_name"] == "Jordan Rivera"  # the caller still gets the real value
