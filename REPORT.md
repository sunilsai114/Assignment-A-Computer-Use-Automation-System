# Design report

The model discovers, the artifact becomes the capability, and deterministic replay is how an agent uses it in
production. Everything below serves that sequence.

## 1. Architecture

```
goal ─► DiscoveryAgent: observe → decide (LLM) → policy → act ─► FlowRecorder ─► Capability (draft) ─review─► approved
                              │                                                                           │
                        Surface (Protocol) ◄──── Replayer (no LLM) ◄──── agent API / CLI / Mission Control ┘
                              ▲                        │
                     SessionController ◄───────────────┘ pause / resume on the same live session
```

- **Single async process, no queues.** One Playwright browser, and each run gets its own browser context. A
  queue-backed worker would slot in at `Replayer.run()`, which returns a self-contained result.
- **Four seams:**
  - **`Surface`:** how to perceive and act on an app.
  - **`ModelClient`:** one method, `decide()`.
  - **`Capability`:** the recorded flow, independent of surface, model and tenant.
  - **`ReplayResult`:** the contract with callers.
- **Observation is an accessibility-style element list** (role, name, label, frame), not HTML or coordinates. It works
  on framesets and nested tables, and it has the shape a desktop accessibility tree gives. Screenshots are opt-in
  (`--vision`), because each one sends customer data to a third-party model. The element list was enough for both
  real runs.
- **The model is `gemini-3.5-flash-lite`** (free tier), using plain function calling over seven tools of our own, so no
  provider's computer-use model is baked in. The two successful runs used about 28k input and 1k output tokens.
- **The target is a local mock bank.** Not-found, permission denied, a surprise dialog, session expiry, a 500 and an
  irreversible submit all have to be reproducible on demand.

## 2. Artifact schema

A `Capability` (`cua/schema/capability.py`, also exported as JSON Schema) is a contract first and a step list second:
- **`meta`:** id, semver, `status` (draft/approved), `app` (vendor product), `variant` (tenant), originating run.
- **`inputs` / `outputs`:** typed (`string/int/money/enum/bool`), with patterns and enums, and `sensitive` flags that
  drive masking.
- **`steps`:** `intent` (for reviewers), action, target, value, risk, and `pre`/`post` checkpoints. Reads name the output
  they fill.
- **`success`:** the flow checkpoint. **`outcomes`:** *declared* business results and how each is detected.
- **`interstitials`:** known dialogs and where to resume. **`auth`:** sign-in by env-var name, and how to tell signed-in
  from expired.
- **`variants`:** per-tenant overrides (section 4).

The decisions behind it:
- **Values are references, never data** (`param` / `literal` / `secret_env`). The validator rejects secrets outside
  `auth`, unknown `{{placeholders}}` and outputs no step produces. The recorder refuses to save an artifact containing
  an example input or a value read off screen.
- **Targets are ranked locators, each with a robustness grade and a one-line `why`.** Roughly from most to least stable on
  legacy UIs: `role`+name, `label_text` (a `<label>` *or the neighbouring table cell*), `table_cell` (row label + column),
  `text`, `attr` (a cryptic but stable `name=f1`), `structural`, and `visual` for screenshot-only surfaces. Discovery
  keeps only locators **proven to hit the exact element the model acted on**. Nothing is guessed.
- **Outcomes are declared, not inferred.** That is how "no such member" becomes an answer rather than a crash. One
  happy-path run can't know them, so discovered artifacts are `draft`, which replay and the agent API refuse. The
  approved `memberserv.*` artifacts (hand-authored, same schema) stand for the reviewed versions of the
  `*_discovered.json` drafts.

## 3. Determinism & error handling

**Determinism:**
- Waits are "requests idle" followed by polling the declared checkpoint to a deadline, never fixed sleeps.
- Locators are tried in rank order, and only a *unique* match wins.
- Every screen-changing click has a `post` checkpoint.
- Frames are resolved by name on every step, because Playwright keeps listing detached frames after a reload (a real bug).

**Four statuses, validated for consistency:**
- `SUCCESS` returns typed outputs.
- `BUSINESS_OUTCOME` gives the declared code and the step that detected it.
- `NEEDS_HUMAN` gives an intervention id.
- `FAILED` gives the step, category, expected vs observed, and a screenshot plus DOM snapshot.

**Recoveries are a list on every result, not a status.** A `SUCCESS` that dismissed a dialog and signed in again says so.

**When a step's expectation isn't met, it is classified in this order:**
1. **Declared outcome:** `BUSINESS_OUTCOME`.
2. **Known interstitial:** dismiss it, resume where declared (bounded).
3. **App error:** `FAILED`, **never retried**, because resubmitting could apply a write twice.
4. **Session expired:** sign in again from env credentials (bounded), resume.
5. **Otherwise:** poll to the deadline, then `FAILED`.

Two lessons from the mock:
- Expiry is suspected only after a grace period, because the sign-on form flashes during normal navigation.
- `resume_from` exists because some recoveries destroy state: the notice replaces the search results.

**UI drift** is detected, not just survived. A fallback-locator hit succeeds and records `locator_fallback`, which is the
signal to fix the artifact before the last fallback breaks.

`evidence/replays/` covers every status and recovery, including an injected 500.

## 4. Heterogeneity & multi-tenant

**Surfaces.** The engine only calls the `Surface` protocol (`click/fill/select/read/exists/page_text/settle/screenshot`,
each addressed by a `Target`).
- **Legacy web** is built.
- **A desktop surface** would implement it over UI Automation/AX: `role`, `label_text` and `text` locators map directly
  to the accessibility tree, `frame_path` becomes the window/pane path, and `settle` becomes "UI idle".
- **A surface with no semantics** (Citrix, a mainframe) implements the `visual` locator and OCR text. Kinds a surface
  can't honour simply don't match, and the next locator is tried.

**Tenants.** An artifact belongs to a vendor product (`meta.app`), is recorded on one tenant, and carries per-tenant
`variants`.
- **An override may change *how*:** targets, routes, checkpoints, outcome detectors, sign-in, skipped steps.
- **It may never change *what*:** inputs, outputs, outcome codes and the meaning of success. The validator enforces this.
- **Demo:** Nova Bank (a modern skin with different wording and routes) runs the Heritage artifact through a small
  override, without re-recording.
- **Drift at scale:**
  - **Signals:** `locator_fallback` recoveries, a version-pinning checkpoint (the recorder itself chose "MemberServ v4.2"
    as the app-loaded check), and failure-category rates per `(app, variant, version)`.
  - **Response:** scheduled canary replays. On drift, run discovery on the drifted tenant and diff the draft against the
    base; the diff *is* the proposed override, for a reviewer to approve. A fix to the base reaches every tenant that
    doesn't override that step.

## 5. Escalation & handoff

**What counts as stuck:**
- **Replay:** an irreversible step, or a failure a person could fix (target not found, checkpoint mismatch, unexpected
  state, sign-in failed).
- **Not escalated:** bad input (the caller's problem), policy blocks (humans must not override the allowlist) and app
  errors (a retry could apply a write twice).
- **Discovery:** the same action on the same screen three times, four failures in a row, step or time limits, or the
  model calling `escalate`.

**Control transfer.** Each live session has one `SessionController` with exactly one owner:
`AUTOMATION → PAUSED → HUMAN → AUTOMATION`.
- **Enforcement is structural.** The surface calls `assert_automation()` before every mutating action, so automation
  cannot click while a person holds the session. Human input uses a separate channel, allowed only for the named holder.
- **Every decision is attributed.** Approve, retry, "I did it" and abort each need an operator name.
- **The intervention record** holds the context (step, reason, URL, screenshot), a timestamped control log, and the
  human's recorded clicks and inputs (redacted; password values are never captured).

**Handing back:**
- **Retry:** automation redoes the step.
- **Approve:** automation performs the risky step, for this run only.
- **"I did it":** the engine **verifies the step's checkpoint rather than trusting it**. A false claim fails.
- **Timeouts** run only while nobody has claimed the request, and waiting on a human doesn't count against the run.
- **A closed session** ends the run immediately.

**Two real operator surfaces share the controller:**
- the CLI attended mode, where the human uses the visible browser
- Mission Control, which streams the session and forwards the operator's clicks and keys to it

Mocked: authentication, and a production transport (a CDP screencast or VNC to a sandboxed browser).

## 6. Safety

- **Allowlist, default-deny, in two independent layers:** the step check (action type, current page) and a network guard
  on every browser request, which covers links and redirects too. Tests show neither lets the mock's admin routes through.
- **Irreversible steps never run unattended.** A step is irreversible if declared so *or* if its wording matches a marker
  ("submit application", "close account", …), so a mislabelled discovery can't sneak one in.
  - The policy is *require approval* rather than *block*, because opening accounts is legitimate work that needs a
    person's yes. It is configurable.
  - Discovery never performs one. The real model stopped at review as instructed; a scripted test proves the refusal
    when a model tries.
- **Drafts are gated** from replay and the agent API.
- **Credentials:** the model types `{{secret:NAME}}` and never sees values. Artifacts hold env-var names, and runtime
  secrets are scrubbed from logs by exact match.
- **Redaction:** pattern rules (SSN, email, phone, account number, secret field names) cover every log, result and
  evidence file. Sensitive outputs are masked in logs and the UI but returned to the caller. Evidence screenshots blank
  table values.
  - Names can't be pattern-matched, so they are handled structurally: evidence stores screens as structure only, logged
    reasoning is scrubbed of screen values, and artifacts containing them are refused. The last check fired on a real
    run, when the model wrote "search for member 12345" into a step description.

**Limits I'd name in a review:**
- The model does see screen text, including names, so production needs a model covered by the bank's data agreements,
  or a self-hosted one.
- Failure DOM snapshots are only pattern-redacted, so the evidence store needs access control and retention limits.
- Risk markers are a keyword backstop, not a classifier.
- The consoles have no authentication and bind to localhost.

## 7. Cuts

**Deliberately cut:**
- A real desktop surface (the seam is designed, not built).
- Automatic outcome discovery: a probe run with a known-bad input could propose `outcomes`.
- Promotion tooling: draft to approved is a manual edit today.
- Console authentication and a remote streaming transport.
- Multi-tenant storage, scheduling and queues.
- Turning a human's recorded actions into artifact steps (their descriptors are already locator-shaped).

**Next, in order:**
1. `cua review` with outcome probes, so promotion is evidenced.
2. Per-tenant canary replays and drift diffs.
3. A bounded, policy-checked LLM fallback for a single failed step, recorded as a recovery.
4. A UIA desktop surface to prove the seam.

**Rough edge:** discovery is only as good as flash-lite. The first real attempt looped until the observation showed
filled password fields (`evidence/discovery/00a-*`).
