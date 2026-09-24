"""Guardrails: default-deny allowlist, irreversible-step gating, redaction."""
import copy

import pytest
from playwright.async_api import Error as PlaywrightError

from cua.policy.engine import Policy, Verdict
from cua.schema.capability import (
    Action, Capability, Locator, Meta, Risk, Step, Target, TextPresent, ValueRef,
)
from cua.schema.result import FailureCategory as FC, Status
from cua.surface.web import WebSurface
from mock_app.app import STATE


def button(name: str) -> Target:
    return Target(locators=[Locator(kind="role", params={"role": "button", "name": name}, robustness="high", why="w")])


def one_step(step: Step) -> Capability:
    return Capability(meta=Meta(id="t.one", name="t", description="t", version="0.0.1", app="memberserv",
                                variant="heritage"), steps=[step], success=TextPresent(text="never"))


def test_url_allowlist_is_default_deny(policy):
    assert not policy.check_url("http://127.0.0.1:8010/heritage/newacct/review").blocked
    for bad in ["http://evil.example/heritage/", "http://127.0.0.1:8010/_admin/fault?mode=expire",
                "file:///etc/passwd", "javascript:alert(1)", "http://127.0.0.1:8010/other"]:
        assert policy.check_url(bad).blocked, bad


def test_action_types_are_allowlisted():
    cfg = copy.deepcopy(Policy.from_yaml().cfg)
    cfg["allowed_actions"].remove("type")
    p = Policy(cfg)
    step = Step(id="x", intent="Type", action=Action.type, target=button("x"), value=ValueRef(literal="v"))
    assert p.check_step(step).blocked


def test_mislabelled_irreversible_step_is_upgraded(policy):
    """A discovery run could label the final submit 'safe'. The control's wording overrides the label."""
    step = Step(id="finish", intent="Finish up", action=Action.click, target=button("Submit Application"), risk=Risk.safe)
    assert policy.risk_of(step) == Risk.irreversible
    assert policy.check_step(step).verdict == Verdict.needs_approval


def test_irreversible_can_be_configured_to_hard_block():
    cfg = copy.deepcopy(Policy.from_yaml().cfg)
    cfg["irreversible_policy"] = "block"
    step = Step(id="s", intent="Submit", action=Action.click, target=button("Submit Application"))
    assert Policy(cfg).check_step(step).blocked


def test_redaction(policy):
    r = policy.redactor
    text = "SSN 123-45-6789, j.rivera@example.com, 555-201-3344, acct 10023451, run-12345678"
    out = r.text(text)
    for leaked in ("123-45-6789", "j.rivera@example.com", "555-201-3344", "10023451"):
        assert leaked not in out
    assert "run-12345678" in out  # ids are not account numbers
    r.add_secret_values("hunter2")
    assert "hunter2" not in r.text("pw=hunter2")
    assert r.obj({"Password": "x", "nested": [{"passcode": "y"}]}) == {"Password": "●●●●", "nested": [{"passcode": "●●●●"}]}


async def test_step_check_blocks_navigation_outside_allowlist(replay):
    cap = one_step(Step(id="go", intent="Open admin", action=Action.navigate,
                        value=ValueRef(literal="/_admin/fault?mode=error500")))
    r = await replay(cap, {})
    assert r.status == Status.FAILED and r.failure.category == FC.policy_blocked
    assert STATE["fault"] == "none"  # the request never reached the app


async def test_network_guard_blocks_what_the_step_check_never_saw(server, browser, policy):
    """Second, independent layer: every request the browser makes (links, redirects, scripts) is checked."""
    surface = await WebSurface.open(browser, server, policy)
    with pytest.raises(PlaywrightError):
        await surface.page.goto(f"{server}/_admin/fault?mode=expire")
    assert surface.blocked and STATE["fault"] == "none"
