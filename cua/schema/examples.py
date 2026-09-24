"""Hand-authored reference capability. Used to develop and test replay before any LLM run exists.
The real discovery run will emit an artifact of the same shape into /evidence and /capabilities."""
from cua.schema.capability import (
    AllOf, AuthFlow, Capability, ElementPresent, Interstitial, KnownOutcome, Locator, Meta, Output, Param,
    Step, Target, TextPresent, ValueRef, VariantOverride,
)
from cua.schema.capability import Action as A, ParamType as T


def loc(kind: str, why: str, robustness: str = "medium", **params) -> Locator:
    return Locator(kind=kind, params=params, robustness=robustness, why=why)


def tgt(*locators: Locator, frame: tuple[str, ...] = (), tag: str | None = None) -> Target:
    return Target(frame_path=list(frame), locators=list(locators), expect_tag=tag)


def heritage_lookup_member() -> Capability:
    member_field = tgt(
        loc("label_text", "Label sits in the neighbouring <td>; survives markup reshuffles.", "medium", text="Member Number"),
        loc("attr", "Cryptic but stable field name in a slowly-changing app.", "medium", attr="name", value="f1"),
        frame=("m",), tag="input")
    return Capability(
        meta=Meta(id="memberserv.lookup_member", name="Look up a member and read their savings balance",
                  description="Signs in if needed, searches a member number, returns the savings balance.",
                  version="1.0.0", status="approved", app="memberserv", variant="heritage", created_by="hand-authored"),
        inputs=[Param(name="member_id", type=T.string, description="Member number to look up.",
                      pattern=r"[0-9A-Za-z-]{1,20}")],
        outputs=[Output(name="savings_balance", type=T.money, description="Current savings balance in USD."),
                 Output(name="member_name", type=T.string, description="Member's full name.", sensitive=True)],
        steps=[
            Step(id="open_app", intent="Open the MemberServ home frameset", action=A.navigate,
                 value=ValueRef(literal="/heritage/")),
            Step(id="open_inquiry", intent="Open Member Inquiry from the left nav", action=A.click,
                 target=tgt(loc("text", "Visible link text.", "medium", text="Member Inquiry"), frame=("n",)),
                 post=[TextPresent(text="Member Inquiry", frame="m")]),
            Step(id="enter_member", intent="Type the member number", action=A.type, target=member_field,
                 value=ValueRef(param="member_id")),
            Step(id="submit_search", intent="Submit the search", action=A.click,
                 target=tgt(loc("role", "Submit button by accessible name.", "high", role="button", name="Go"),
                            loc("attr", "Fallback on button value.", "medium", attr="value", value="Go"), frame=("m",)),
                 post=[TextPresent(text="Member {{member_id}}", frame="m")]),
            Step(id="read_balance", intent="Read the Savings row balance", action=A.read, output="savings_balance",
                 parse="money",
                 target=tgt(loc("table_cell", "Row is found by its product label, so row order can change.", "medium",
                                row_text="Savings", col=2), frame=("m",))),
            Step(id="read_name", intent="Read the member's name", action=A.read, output="member_name",
                 target=tgt(loc("table_cell", "Row found by its 'Name' label.", "medium", row_text="Name", col=1),
                            frame=("m",))),
        ],
        success=AllOf(conditions=[TextPresent(text="Member {{member_id}}", frame="m"),
                                  TextPresent(text="Savings", frame="m")]),
        outcomes=[
            KnownOutcome(code="member_not_found", description="No member matches that number.",
                         detected_by=TextPresent(text="No records match your request", frame="m"),
                         returns={"found": False}),
            KnownOutcome(code="permission_denied", description="Operator may not view this record.",
                         detected_by=TextPresent(text="Access denied", frame="m")),
            KnownOutcome(code="invalid_member_number", description="The app rejected the number's format.",
                         detected_by=TextPresent(text="must be numeric", frame="m")),
        ],
        interstitials=[Interstitial(
            name="rate_notice", detected_by=TextPresent(text="Rate sheet updated", frame="m"),
            dismiss=Step(id="dismiss.rate_notice", intent="Acknowledge the rate notice", action=A.click,
                         target=tgt(loc("text", "Visible link text.", "medium", text="Continue"), frame=("m",))))],
        auth=AuthFlow(
            steps=[
                Step(id="auth.user", intent="Enter operator id", action=A.type,
                     target=tgt(loc("label_text", "Adjacent label.", "medium", text="Operator ID"), frame=("m",), tag="input"),
                     value=ValueRef(secret_env="HERITAGE_USER")),
                Step(id="auth.pass", intent="Enter passcode", action=A.type,
                     target=tgt(loc("label_text", "Adjacent label.", "medium", text="Passcode"), frame=("m",), tag="input"),
                     value=ValueRef(secret_env="HERITAGE_PASS")),
                Step(id="auth.submit", intent="Sign on", action=A.click,
                     target=tgt(loc("role", "Button by name.", "high", role="button", name="Sign On"), frame=("m",))),
            ],
            logged_in_when=TextPresent(text="Member Inquiry", frame="m"),
            session_expired_when=TextPresent(text="Operator ID", frame="m")),
        variants=[nova_override()],
    )


def nova_override() -> VariantOverride:
    """Nova Bank runs the same product with a different skin: only targeting/detection differ."""
    return VariantOverride(
        variant="nova",
        note="Modern skin: semantic roles, route-based navigation, different wording.",
        values={"open_app": ValueRef(literal="/nova/customers")},
        skip_steps=["open_inquiry"],
        targets={
            "enter_member": tgt(loc("role", "Labelled textbox.", "high", role="textbox", name="Customer ID"), tag="input"),
            "submit_search": tgt(loc("role", "Button by name.", "high", role="button", name="Look up")),
            "read_balance": tgt(loc("table_cell", "Row found by product label.", "medium", row_text="Savings", col=2)),
            "read_name": tgt(loc("role", "Page heading holds the name.", "high", role="heading", name=".+")),
        },
        outcome_detectors={
            "member_not_found": TextPresent(text="Customer not found"),
            "permission_denied": TextPresent(text="do not have permission"),
            "invalid_member_number": TextPresent(text="Enter a numeric Customer ID"),
        },
        success=AllOf(conditions=[TextPresent(text="Current balance"), TextPresent(text="Savings")]),
        auth=AuthFlow(
            steps=[
                Step(id="auth.user", intent="Enter username", action=A.type,
                     target=tgt(loc("role", "Labelled textbox.", "high", role="textbox", name="Username"), tag="input"),
                     value=ValueRef(secret_env="HERITAGE_USER")),
                Step(id="auth.pass", intent="Enter password", action=A.type,
                     target=tgt(loc("label_text", "Adjacent label.", "high", text="Password"), tag="input"),
                     value=ValueRef(secret_env="HERITAGE_PASS")),
                Step(id="auth.submit", intent="Sign in", action=A.click,
                     target=tgt(loc("role", "Button by name.", "high", role="button", name="Sign in"))),
            ],
            logged_in_when=TextPresent(text="Customer ID"),
            session_expired_when=TextPresent(text="Sign in")),
    )


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "capabilities/memberserv.lookup_member.json"
    open(path, "w", encoding="utf-8").write(heritage_lookup_member().to_json())
    print("wrote", path)
