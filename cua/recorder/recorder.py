"""Turns a discovery run into a Capability.

The artifact is built from facts, not from the model's words:
  * Targets: candidate locators come from what the page says about the element the model picked (role +
    accessible name, the label beside it, its row label, its field name). Each candidate is kept only if it
    resolves to exactly that element on the live page at record time.
  * Values: text equal to a caller input becomes {param: ...}; {{secret:X}} becomes {secret_env: X}.
  * Checkpoints: after a click/navigation, the first new, stable, non-sensitive line of text that appeared.
  * Sign-in steps (the ones that typed secrets, plus the click that submitted them) become the AuthFlow.
The model transcript never enters the artifact; it stays in the run's evidence.
"""
import re

from cua.agent.observe import REF_ATTR, Element
from cua.policy.engine import Policy
from cua.schema.capability import (
    PLACEHOLDER, Action, AllOf, AuthFlow, Capability, Condition, Locator, Meta, Output, Param, ParamType, Risk,
    Step, Target, TextAbsent, TextPresent, ValueRef,
)

SECRET = re.compile(r"^\{\{secret:(\w+)\}\}$")
FORM_TAGS = {"input", "select", "textarea"}
ROLE_KINDS = {"button", "link", "textbox", "combobox", "heading", "checkbox", "radio"}


class RecordError(Exception):
    """The run cannot be turned into a replayable artifact (e.g. no locator verified)."""


def text_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in text.splitlines() if line.strip()] if text else []


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:32].strip("_")
    return s if s and s[0].isalpha() else f"step_{s}".strip("_")


class FlowRecorder:
    def __init__(self, surface, policy: Policy, inputs: dict[str, tuple[str, str]], outputs: dict[str, str]):
        """inputs: name -> (type, example value). outputs: name -> type."""
        self.surface, self.policy = surface, policy
        self.inputs, self.outputs = inputs, outputs
        self.steps: list[Step] = []
        self.auth_steps: list[Step] = []
        self.auth_logged_in: list[Condition] = []
        self.auth_expired: Condition | None = None
        self.auth_resume_from: str | None = None
        self._auth_open = False  # typed a secret; waiting for the click that submits it
        self._ids: set[str] = set()
        # Identifier-like example values (a member number) must never be baked into the artifact. Enum/money
        # values are screen vocabulary ("Savings") and may legitimately appear in labels and locators.
        self._data_values = {n: v for n, (t, v) in inputs.items() if t in ("string", "int") and v and len(v) >= 3}
        self.enum_options: dict[str, list[str]] = {}  # enum input -> options seen in the drop-down it filled
        self.read_values: set[str] = set()  # values read off screens during discovery: never artifact content
        self.notes: list[str] = []  # reviewer-facing remarks about what was inferred or dropped

    # ── building blocks ──
    def _id(self, intent: str) -> str:
        base = slug(intent) or "step"
        sid, n = base, 2
        while sid in self._ids:
            sid, n = f"{base}_{n}", n + 1
        self._ids.add(sid)
        return sid

    def _leaks_data(self, s: str) -> bool:
        """True if a string would bake caller data or sensitive text into the artifact."""
        if self.policy.redactor.text(s) != s:
            return True
        return any(v in s for v in self._data_values.values())

    def candidates(self, el: Element) -> list[Locator]:
        c: list[Locator] = []
        accessible = el.aria or el.label or el.value_attr or (el.text if el.role in ("link", "button", "heading") else "")
        if el.role in ROLE_KINDS and accessible:
            c.append(Locator(kind="role", params={"role": el.role, "name": accessible}, robustness="high",
                             why="ARIA role + accessible name: survives markup and layout changes."))
        if el.tag in FORM_TAGS and (el.label or el.adjacent):
            c.append(Locator(kind="label_text", params={"text": el.label or el.adjacent}, robustness="medium",
                             why="The label an operator reads beside the field (a <label> or the neighbouring cell)."))
        if el.row_text is not None:
            c.append(Locator(kind="table_cell", params={"row_text": el.row_text, "col": el.col}, robustness="medium",
                             why="Cell found by its row's label and column, so row order can change."))
        elif el.tag not in FORM_TAGS and el.text:
            c.append(Locator(kind="text", params={"text": el.text}, robustness="medium",
                             why="Visible text of the control: the only handle on non-semantic clickable markup."))
        if el.name_attr:
            c.append(Locator(kind="attr", params={"attr": "name", "value": el.name_attr}, robustness="medium",
                             why="Form field name: cryptic but stable in a slowly-changing app."))
        if el.value_attr:
            c.append(Locator(kind="attr", params={"attr": "value", "value": el.value_attr}, robustness="low",
                             why="Button caption attribute: breaks if the caption is reworded."))
        return [loc for loc in c if not any(self._leaks_data(str(v)) for v in loc.params.values())]

    async def target_for(self, el: Element) -> Target:
        """Keep only candidates proven to hit exactly this element on the live page, in rank order."""
        tag = el.tag if el.tag in FORM_TAGS else None
        kept, dropped = [], []
        for loc in self.candidates(el):
            probe = Target(frame_path=el.frame_path, locators=[loc], expect_tag=tag)
            if await self.surface.identify(probe, REF_ATTR) == el.ref:
                kept.append(loc)
            else:
                dropped.append(f"{loc.kind} {dict(loc.params)}")
        if dropped:
            self.notes.append(f"'{el.name}': dropped locators that did not uniquely match: {'; '.join(dropped)}")
        if not kept:
            raise RecordError(f"no stable locator found for '{el.name}' ({el.role}) in frame {el.frame_path}")
        return Target(frame_path=el.frame_path, locators=kept, expect_tag=tag)

    def value_for(self, raw: str) -> tuple[ValueRef, bool]:
        """Map what the model typed to a ValueRef. Returns (ref, is_secret)."""
        if m := SECRET.match(raw):
            return ValueRef(secret_env=m.group(1)), True
        if (m := PLACEHOLDER.fullmatch(raw)) and m.group(1) in self.inputs:
            return ValueRef(param=m.group(1)), False
        for name, (_, example) in self.inputs.items():
            if raw == example:
                return ValueRef(param=name), False
        return ValueRef(literal=raw), False

    def template(self, line: str) -> str:
        for name, example in self._data_values.items():
            line = line.replace(example, "{{" + name + "}}")
        return line

    def checkpoint(self, before: dict[str, str], after: dict[str, str], prefer: str | None) -> list[Condition]:
        """First new, short, non-tabular, non-sensitive line of text that appeared after an action."""
        frames = [f for f in after if text_lines(after[f]) != text_lines(before.get(f, ""))]
        frames.sort(key=lambda f: f != (prefer or ""))
        for f in frames:
            old = set(text_lines(before.get(f, "")))
            for raw in after[f].splitlines():
                line = " ".join(raw.split())
                # a tab means a table row: data ("Name  Jordan Rivera"), not a heading we can rely on
                if not line or line in old or "\t" in raw or not 3 <= len(line) <= 60 or line.replace(" ", "").isdigit():
                    continue
                if any(v in line for v in self.read_values):
                    continue
                if self.policy.redactor.text(line) != line:
                    continue  # looks like PII; never becomes part of an artifact
                t = self.template(line)
                if PLACEHOLDER.sub("", t) != t and not any(c.isalpha() for c in PLACEHOLDER.sub("", t)):
                    continue  # nothing but a caller value: not a meaningful checkpoint
                return [TextPresent(text=t, frame=f or None)]
        return []

    # ── recording ──
    def record(self, step: Step, secret: bool) -> Step:
        """Append a step, routing sign-in steps into the auth flow."""
        if secret:
            self._auth_open = True
            if self.auth_expired is None and step.target:
                label = next((loc.params["text"] for loc in step.target.locators if loc.kind == "label_text"), None)
                if label:
                    self.auth_expired = TextPresent(text=str(label), frame=step.target.frame_path[-1] if step.target.frame_path else None)
            self.auth_steps.append(step)
            return step
        if self._auth_open and step.action == Action.click:
            self._auth_open = False
            self.auth_logged_in = list(step.post)
            self.auth_steps.append(step.model_copy(update={"post": []}))
            return step
        if self.auth_steps and self.auth_resume_from is None:
            self.auth_resume_from = step.id
        self.steps.append(step)
        return step

    def new_step(self, **kw) -> Step:
        kw["id"] = self._id(kw.pop("intent_for_id", kw["intent"]))
        step = Step(**kw)
        return step.model_copy(update={"risk": self.policy.risk_of(step)})

    def _param(self, name: str, type_: str) -> Param:
        if type_ == "enum":
            if name in self.enum_options:
                return Param(name=name, type=ParamType.enum, enum=self.enum_options[name],
                             description="Supplied by the caller; one of the options the app offered.")
            self.notes.append(f"input '{name}' was declared enum but never filled a drop-down; recorded as string")
            type_ = "string"
        return Param(name=name, type=ParamType(type_), description="Supplied by the caller.")

    def build(self, *, cap_id: str, name: str, goal: str, app: str, variant: str, run_id: str) -> Capability:
        posts = [s for s in self.steps if s.post]
        if not posts:
            raise RecordError("no step produced a verifiable checkpoint; cannot define success")
        last = posts[-1].post
        success = last[0] if len(last) == 1 else AllOf(conditions=last)
        auth = None
        if self.auth_steps:
            expired = self.auth_expired or TextAbsent(text="__never__")
            logged_in = self.auth_logged_in or ([TextAbsent(text=expired.text, frame=expired.frame)]
                                                if isinstance(expired, TextPresent) else [])
            if not logged_in:
                raise RecordError("could not infer how to tell that sign-in worked")
            auth = AuthFlow(steps=self.auth_steps, session_expired_when=expired,
                            logged_in_when=logged_in[0] if len(logged_in) == 1 else AllOf(conditions=logged_in),
                            resume_from=self.auth_resume_from)
        cap = Capability(
            meta=Meta(id=cap_id, name=self.template(name), description=f"Discovered from goal: {self.template(goal)}",
                      version="0.1.0",
                      status="draft", app=app, variant=variant, created_by=run_id),
            inputs=[self._param(n, t) for n, (t, _) in self.inputs.items()],
            outputs=[Output(name=n, type=ParamType(t), description=f"Read during discovery ({n}).")
                     for n, t in self.outputs.items()],
            steps=self.steps, success=success, auth=auth)
        blob = cap.to_json()
        leaked = [n for n, v in self._data_values.items() if v in blob]
        leaked += ["a value read during discovery" for v in self.read_values if len(v) >= 3 and v in blob][:1]
        if leaked:
            raise RecordError(f"artifact would contain example values of {leaked}; refusing to save it")
        return cap
