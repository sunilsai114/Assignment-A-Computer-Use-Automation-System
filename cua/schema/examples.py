"""Hand-authored reference capabilities. Used to develop and test replay before any LLM run exists.
The real discovery run will emit an artifact of the same shape into /evidence and /capabilities."""
from cua.schema.capability import (
    AllOf, AuthFlow, Capability, Interstitial, KnownOutcome, Locator, Meta, Output, Param,
    Step, Target, TextPresent, ValueRef, VariantOverride,
)
from cua.schema.capability import Action as A, ParamType as T, Risk


def loc(kind: str, why: str, robustness: str = "medium", **params) -> Locator:
    return Locator(kind=kind, params=params, robustness=robustness, why=why)


def tgt(*locators: Locator, frame: tuple[str, ...] = (), tag: str | None = None) -> Target:
    return Target(frame_path=list(frame), locators=list(locators), expect_tag=tag)


# ───────────── pieces shared by every MemberServ capability on the Heritage skin ─────────────
def member_id_param() -> Param:
    return Param(name="member_id", type=T.string, description="Member number to look up.",
                 pattern=r"[0-9A-Za-z-]{1,20}")


def member_search_steps() -> list[Step]:
    member_field = tgt(
        loc("label_text", "Label sits in the neighbouring <td>; survives markup reshuffles.", "medium", text="Member Number"),
        loc("attr", "Cryptic but stable field name in a slowly-changing app.", "medium", attr="name", value="f1"),
        frame=("m",), tag="input")
    return [
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
    ]


def member_outcomes() -> list[KnownOutcome]:
    return [
        KnownOutcome(code="member_not_found", description="No member matches that number.",
                     detected_by=TextPresent(text="No records match your request", frame="m"),
                     returns={"found": False}),
        KnownOutcome(code="permission_denied", description="Operator may not view this record.",
                     detected_by=TextPresent(text="Access denied", frame="m")),
        KnownOutcome(code="invalid_member_number", description="The app rejected the number's format.",
                     detected_by=TextPresent(text="must be numeric", frame="m")),
    ]


def rate_notice() -> Interstitial:
    return Interstitial(
        name="rate_notice", detected_by=TextPresent(text="Rate sheet updated", frame="m"),
        resume_from="enter_member",  # the notice replaces the search results, so the search must be redone
        dismiss=Step(id="dismiss.rate_notice", intent="Acknowledge the rate notice", action=A.click,
                     target=tgt(loc("text", "Visible link text.", "medium", text="Continue"), frame=("m",))))


def heritage_auth() -> AuthFlow:
    return AuthFlow(
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
        session_expired_when=TextPresent(text="Operator ID", frame="m"))


# ───────────────────────────── capabilities ─────────────────────────────
def heritage_lookup_member() -> Capability:
    return Capability(
        meta=Meta(id="memberserv.lookup_member", name="Look up a member and read their savings balance",
                  description="Signs in if needed, searches a member number, returns the savings balance.",
                  version="1.0.0", status="approved", app="memberserv", variant="heritage", created_by="hand-authored"),
        inputs=[member_id_param()],
        outputs=[Output(name="savings_balance", type=T.money, description="Current savings balance in USD."),
                 Output(name="member_name", type=T.string, description="Member's full name.", sensitive=True)],
        steps=[
            *member_search_steps(),
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
        outcomes=member_outcomes(),
        interstitials=[rate_notice()],
        auth=heritage_auth(),
        variants=[nova_override()],
    )


def heritage_open_subaccount() -> Capability:
    """Multi-field form + review + an irreversible submit: the step that must never run unattended."""
    return Capability(
        meta=Meta(id="memberserv.open_subaccount", name="Open a sub-account for a member",
                  description="Finds the member, fills the new sub-account form, checks the review screen, then "
                              "submits. Submitting is irreversible and needs a human's approval on every run.",
                  version="1.0.0", status="approved", app="memberserv", variant="heritage", created_by="hand-authored"),
        inputs=[member_id_param(),
                Param(name="product", type=T.enum, enum=["Savings", "Checking", "Money Market"],
                      description="Type of sub-account to open."),
                Param(name="opening_deposit", type=T.money, description="Opening deposit in USD (minimum $25)."),
                Param(name="nickname", type=T.string, required=False, default="", pattern=r"[\w .'-]{0,30}",
                      description="Optional label shown to the member.")],
        outputs=[Output(name="review_deposit", type=T.money, description="Deposit amount as confirmed on the review screen."),
                 Output(name="reference", type=T.string, description="Application reference from the confirmation.")],
        steps=[
            *member_search_steps(),
            Step(id="open_new_subaccount", intent="Open the New Sub-Account form", action=A.click,
                 target=tgt(loc("text", "A clickable <span>, not a link: visible text is the only handle.", "medium",
                                text="New Sub-Account"), frame=("m",)),
                 post=[TextPresent(text="Open Sub-Account", frame="m")]),
            Step(id="choose_product", intent="Choose the product type", action=A.select,
                 target=tgt(loc("label_text", "Adjacent label.", "medium", text="Product Type"),
                            loc("attr", "Stable field name.", "medium", attr="name", value="p1"), frame=("m",), tag="select"),
                 value=ValueRef(param="product")),
            Step(id="enter_deposit", intent="Enter the opening deposit", action=A.type,
                 target=tgt(loc("label_text", "Adjacent label.", "medium", text="Opening Deposit"),
                            loc("attr", "Stable field name.", "medium", attr="name", value="p2"), frame=("m",), tag="input"),
                 value=ValueRef(param="opening_deposit")),
            Step(id="enter_nickname", intent="Enter the nickname", action=A.type,
                 target=tgt(loc("label_text", "Adjacent label.", "medium", text="Nickname"),
                            loc("attr", "Stable field name.", "medium", attr="name", value="p3"), frame=("m",), tag="input"),
                 value=ValueRef(param="nickname")),
            Step(id="continue_to_review", intent="Continue to the review screen", action=A.click, risk=Risk.risky,
                 target=tgt(loc("role", "Button by name.", "high", role="button", name="Continue"), frame=("m",)),
                 post=[TextPresent(text="Review Sub-Account Application", frame="m")]),
            Step(id="read_review_deposit", intent="Read the deposit shown for review", action=A.read,
                 output="review_deposit", parse="money",
                 target=tgt(loc("table_cell", "Row found by its label.", "medium", row_text="Deposit", col=1), frame=("m",))),
            Step(id="submit_application", intent="Submit the application", action=A.click, risk=Risk.irreversible,
                 target=tgt(loc("role", "Button by name.", "high", role="button", name="Submit Application"), frame=("m",)),
                 post=[TextPresent(text="Application submitted", frame="m")]),
            Step(id="read_reference", intent="Read the application reference", action=A.read, output="reference",
                 target=tgt(loc("table_cell", "Row found by its label.", "medium", row_text="Reference", col=1), frame=("m",))),
        ],
        success=TextPresent(text="Application submitted", frame="m"),
        outcomes=[
            *member_outcomes(),
            KnownOutcome(code="deposit_below_minimum", description="The opening deposit is under the $25 minimum.",
                         detected_by=TextPresent(text="Minimum opening deposit", frame="m")),
            KnownOutcome(code="deposit_not_a_number", description="The app could not read the deposit amount.",
                         detected_by=TextPresent(text="must be a number", frame="m")),
        ],
        interstitials=[rate_notice()],
        auth=heritage_auth(),
    )


def nova_override() -> VariantOverride:
    """Nova Bank runs the same product with a different skin: only targeting/detection differ."""
    return VariantOverride(
        variant="nova",
        note="Modern skin: semantic roles, route-based navigation, different wording.",
        values={"open_app": ValueRef(literal="/nova/customers")},
        skip_steps=["open_inquiry"],
        posts={"submit_search": [TextPresent(text="Current balance")]},
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


ALL = [heritage_lookup_member, heritage_open_subaccount]

if __name__ == "__main__":
    import sys
    from pathlib import Path
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "capabilities")
    for build in ALL:
        cap = build()
        path = out / f"{cap.meta.id}.json"
        path.write_text(cap.to_json(), encoding="utf-8")
        print("wrote", path)
