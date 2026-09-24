"""The Capability artifact: a typed, versioned, tenant-neutral description of a reusable flow.

Design rules enforced here (not just documented):
  * No raw values in steps: inputs are referenced via ValueRef(param=...), never baked in.
  * Credentials may only be referenced (secret_env) inside the auth sub-flow, never in main steps.
  * Every locator kind declares the fields it needs, so a reviewer can trust what they read.
  * Placeholders ({{name}}) anywhere in the artifact must resolve to a declared input.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Risk(str, Enum):
    safe = "safe"
    risky = "risky"
    irreversible = "irreversible"


RISK_ORDER = {Risk.safe: 0, Risk.risky: 1, Risk.irreversible: 2}


class Action(str, Enum):
    navigate = "navigate"
    click = "click"
    type = "type"
    select = "select"
    read = "read"
    wait = "wait"


class ParamType(str, Enum):
    string = "string"
    int = "int"
    money = "money"
    enum = "enum"
    bool = "bool"


# ───────────────────────────── Targeting ─────────────────────────────
# Kind -> required keys. Ordered by typical stability on legacy UIs (see Target.locators).
LOCATOR_KINDS: dict[str, set[str]] = {
    "role": {"role", "name"},            # ARIA role + accessible name: best when the app has semantics
    "label_text": {"text"},              # control next to/labelled by this text (works for <td>Label</td><td><input>)
    "attr": {"attr", "value"},           # stable attribute, e.g. name=f1: cryptic but stable in a slowly changing app
    "table_cell": {"row_text", "col"},   # cell in the row containing row_text: for data extraction in nested tables
    "text": {"text"},                    # visible text of a link/button/span
    "structural": {"path"},              # frame/table/row/cell path: brittle, last DOM resort
    "visual": {"anchor_text"},           # screenshot/region fallback: the only option on non-DOM surfaces
}


class Locator(Strict):
    kind: Literal["role", "label_text", "attr", "table_cell", "text", "structural", "visual"]
    params: dict[str, str | int]
    robustness: Literal["high", "medium", "low"]
    why: str = Field(description="One-line reasoning a reviewer can challenge.")

    @model_validator(mode="after")
    def _has_required(self) -> "Locator":
        missing = LOCATOR_KINDS[self.kind] - set(self.params)
        if missing:
            raise ValueError(f"locator kind '{self.kind}' requires params {sorted(missing)}")
        return self


class Target(Strict):
    """How to find one control. `locators` are tried in order; the first unique match wins."""
    frame_path: list[str] = Field(default_factory=list, description="Nested frame names, outermost first.")
    locators: list[Locator] = Field(min_length=1)
    expect_tag: str | None = Field(default=None, description="e.g. 'input': guards against matching the wrong control.")


# ───────────────────────────── Conditions ─────────────────────────────
class TextPresent(Strict):
    kind: Literal["text_present"] = "text_present"
    text: str
    frame: str | None = None


class TextAbsent(Strict):
    kind: Literal["text_absent"] = "text_absent"
    text: str
    frame: str | None = None


class UrlMatches(Strict):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class ElementPresent(Strict):
    kind: Literal["element_present"] = "element_present"
    target: Target


class ElementTextMatches(Strict):
    kind: Literal["element_text_matches"] = "element_text_matches"
    target: Target
    pattern: str


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list["Condition"] = Field(min_length=1)


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list["Condition"] = Field(min_length=1)


Condition = Annotated[
    Union[TextPresent, TextAbsent, UrlMatches, ElementPresent, ElementTextMatches, AllOf, AnyOf],
    Field(discriminator="kind"),
]
AllOf.model_rebuild()
AnyOf.model_rebuild()


# ───────────────────────────── Values, params, outputs ─────────────────────────────
class ValueRef(Strict):
    """Exactly one of: a fixed literal, a caller-supplied param, or a runtime secret (auth flow only)."""
    literal: str | None = None
    param: str | None = None
    secret_env: str | None = Field(default=None, description="Name of an env var; the value is never stored.")

    @model_validator(mode="after")
    def _exactly_one(self) -> "ValueRef":
        if sum(v is not None for v in (self.literal, self.param, self.secret_env)) != 1:
            raise ValueError("ValueRef needs exactly one of literal / param / secret_env")
        return self


class Param(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str
    required: bool = True
    default: str | None = None
    pattern: str | None = Field(default=None, description="Regex the value must fully match.")
    enum: list[str] | None = None
    sensitive: bool = Field(default=False, description="Redacted in every log and evidence file.")

    @model_validator(mode="after")
    def _enum_ok(self) -> "Param":
        if (self.type == ParamType.enum) != (self.enum is not None):
            raise ValueError("enum params need an `enum` list (and only they may have one)")
        return self


class Output(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: ParamType
    description: str
    sensitive: bool = False


# ───────────────────────────── Steps ─────────────────────────────
class Step(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    intent: str = Field(description="Plain-language purpose; what a reviewer reads.")
    action: Action
    target: Target | None = None
    value: ValueRef | None = None
    output: str | None = Field(default=None, description="For `read` steps: the declared Output this fills.")
    parse: Literal["text", "money"] = "text"
    risk: Risk = Risk.safe
    pre: list[Condition] = Field(default_factory=list)
    post: list[Condition] = Field(default_factory=list)
    timeout_ms: int | None = Field(default=None, ge=100, le=60000)

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        a = self.action
        if a in (Action.click, Action.type, Action.select, Action.read) and self.target is None:
            raise ValueError(f"step {self.id}: '{a.value}' needs a target")
        if a in (Action.navigate, Action.type, Action.select) and self.value is None:
            raise ValueError(f"step {self.id}: '{a.value}' needs a value")
        if a == Action.read and not self.output:
            raise ValueError(f"step {self.id}: 'read' must name the output it fills")
        if a != Action.read and self.output:
            raise ValueError(f"step {self.id}: only 'read' steps may set output")
        return self


class KnownOutcome(Strict):
    """An expected business result. Detecting one ends the run as BUSINESS_OUTCOME, not a failure."""
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    detected_by: Condition
    returns: dict[str, Any] = Field(default_factory=dict)


class Interstitial(Strict):
    """A known, safe-to-dismiss dialog. Replay may click through it and retry the step (bounded)."""
    name: str
    detected_by: Condition
    dismiss: Step
    resume_from: str | None = Field(
        default=None, description="Main-step id to continue from after dismissing (default: retry the current step). "
        "Needed when dismissing loses page state, e.g. the dialog replaces the results page.")


class AuthFlow(Strict):
    """Re-establishes a session after expiry. Credentials come from env at runtime, never the artifact."""
    steps: list[Step] = Field(min_length=1)
    logged_in_when: Condition
    session_expired_when: Condition


class VariantOverride(Strict):
    """Per-tenant specialisation of a shared capability. Only targeting/detection may differ; the
    contract (inputs, outputs, outcome codes, success meaning) stays identical across variants."""
    variant: str
    targets: dict[str, Target] = Field(default_factory=dict, description="step id -> replacement target")
    values: dict[str, ValueRef] = Field(default_factory=dict, description="step id -> replacement value (e.g. route)")
    posts: dict[str, list[Condition]] = Field(default_factory=dict, description="step id -> replacement checkpoints")
    skip_steps: list[str] = Field(default_factory=list)
    outcome_detectors: dict[str, Condition] = Field(default_factory=dict, description="outcome code -> condition")
    success: Condition | None = None
    auth: "AuthFlow | None" = None
    note: str = ""


class Meta(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    name: str
    description: str
    schema_version: Literal["1"] = SCHEMA_VERSION
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Literal["draft", "approved"] = "draft"
    app: str = Field(description="Vendor product id shared across tenants, e.g. 'memberserv'.")
    variant: str = Field(description="Tenant/version this was recorded on, e.g. 'heritage'.")
    created_by: str = Field(default="", description="Discovery run id, or 'hand-authored'.")


class Capability(Strict):
    meta: Meta
    inputs: list[Param] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    preconditions: list[Condition] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Condition
    outcomes: list[KnownOutcome] = Field(default_factory=list)
    interstitials: list[Interstitial] = Field(default_factory=list)
    auth: AuthFlow | None = None
    variants: list[VariantOverride] = Field(default_factory=list)

    # ── derived, reviewable facts ──
    @property
    def max_risk(self) -> Risk:
        return max((s.risk for s in self.steps), key=RISK_ORDER.get, default=Risk.safe)

    @property
    def approval_required_steps(self) -> list[str]:
        return [s.id for s in self.steps if s.risk == Risk.irreversible]

    # ── cross-field integrity ──
    @model_validator(mode="after")
    def _integrity(self) -> "Capability":
        params = {p.name for p in self.inputs}
        outs = {o.name for o in self.outputs}
        all_steps = [*self.steps, *(self.auth.steps if self.auth else []),
                     *(i.dismiss for i in self.interstitials)]
        ids = [s.id for s in all_steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique across steps, auth and interstitials")
        if len(params) != len(self.inputs) or len(outs) != len(self.outputs):
            raise ValueError("input/output names must be unique")

        for it in self.interstitials:
            if it.resume_from and it.resume_from not in {s.id for s in self.steps}:
                raise ValueError(f"interstitial '{it.name}': unknown resume_from '{it.resume_from}'")
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("outcome codes must be unique")

        main_ids = {s.id for s in self.steps}
        for s in all_steps:
            if s.value and s.value.param and s.value.param not in params:
                raise ValueError(f"step {s.id}: undeclared param '{s.value.param}'")
            if s.value and s.value.secret_env and s not in (self.auth.steps if self.auth else []):
                raise ValueError(f"step {s.id}: secrets may only be referenced inside the auth flow")
            if s.output and s.output not in outs:
                raise ValueError(f"step {s.id}: undeclared output '{s.output}'")
        produced = {s.output for s in self.steps if s.output}
        if outs - produced:
            raise ValueError(f"outputs never produced by a read step: {sorted(outs - produced)}")

        for m in re.findall(PLACEHOLDER, self.model_dump_json()):
            if m not in params:
                raise ValueError(f"placeholder {{{{{m}}}}} does not match a declared input")

        for v in self.variants:
            for sid in [*v.targets, *v.values, *v.posts, *v.skip_steps]:
                if sid not in main_ids:
                    raise ValueError(f"variant '{v.variant}': unknown step '{sid}'")
            for code in v.outcome_detectors:
                if code not in codes:
                    raise ValueError(f"variant '{v.variant}': unknown outcome '{code}'")
        if len({v.variant for v in self.variants}) != len(self.variants):
            raise ValueError("duplicate variant names")
        return self

    # ── serialisation ──
    def to_json(self) -> str:
        return self.model_dump_json(indent=2, exclude_none=True)

    @classmethod
    def from_json(cls, text: str) -> "Capability":
        return cls.model_validate(json.loads(text))


def resolve_variant(cap: Capability, variant: str) -> Capability:
    """Return the effective capability for a tenant: base flow + that tenant's overrides."""
    if variant == cap.meta.variant:
        return cap
    ov = next((v for v in cap.variants if v.variant == variant), None)
    if ov is None:
        raise KeyError(f"capability '{cap.meta.id}' has no variant '{variant}'")
    data = cap.model_copy(deep=True)
    steps = []
    for s in data.steps:
        if s.id in ov.skip_steps:
            continue
        upd = {}
        if s.id in ov.targets:
            upd["target"] = ov.targets[s.id]
        if s.id in ov.values:
            upd["value"] = ov.values[s.id]
        if s.id in ov.posts:
            upd["post"] = ov.posts[s.id]
        steps.append(s.model_copy(update=upd))
    outcomes = [o.model_copy(update={"detected_by": ov.outcome_detectors[o.code]}) if o.code in ov.outcome_detectors
                else o for o in data.outcomes]
    return data.model_copy(update={
        "steps": steps, "outcomes": outcomes,
        "success": ov.success or data.success,
        "auth": ov.auth or data.auth,
        "meta": data.meta.model_copy(update={"variant": variant}),
        "variants": [],
    })


# ───────────────────────────── Input validation for callers ─────────────────────────────
class InputError(ValueError):
    """The caller's inputs don't satisfy the capability contract."""


def coerce_inputs(cap: Capability, raw: dict[str, Any]) -> dict[str, str]:
    """Validate and normalise caller inputs to strings. Raises InputError listing every problem."""
    errs: list[str] = []
    out: dict[str, str] = {}
    known = {p.name for p in cap.inputs}
    errs += [f"unknown input '{k}'" for k in raw if k not in known]
    for p in cap.inputs:
        v = raw.get(p.name, p.default)
        if v is None:
            if p.required:
                errs.append(f"missing required input '{p.name}'")
            continue
        s = str(v).strip()
        try:
            if p.type == ParamType.int:
                int(s)
            elif p.type == ParamType.money:
                Decimal(s.replace(",", "").replace("$", ""))
            elif p.type == ParamType.bool and s.lower() not in ("true", "false"):
                raise ValueError
            elif p.type == ParamType.enum and s not in (p.enum or []):
                raise ValueError
        except (ValueError, InvalidOperation):
            errs.append(f"input '{p.name}' is not a valid {p.type.value}")
            continue
        if p.pattern and not re.fullmatch(p.pattern, s):
            errs.append(f"input '{p.name}' does not match {p.pattern}")
            continue
        out[p.name] = s
    if errs:
        raise InputError("; ".join(errs))
    return out
