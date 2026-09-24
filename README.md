# Computer-Use Automation: discover once, replay forever

An LLM works out how to do a task in a legacy back-office app **once**. That run is recorded as a typed,
versioned **capability**, which is then replayed **deterministically, with no LLM**, whenever an AI agent needs
it. Replay tells business outcomes apart from recoverable conditions and hard failures. It pauses for a human on
the same live session when it must, and enforces an allowlist and redaction throughout.

- **Target:** a local mock bank (`mock_app/`) built to be hostile to automation. It has a frameset, nested tables, no ids or
  test ids, cryptic field names and a clickable `<span>`. A second tenant, "Nova Bank", runs the same product with a
  modern skin, and fault injection covers slow loads, a surprise notice, session expiry and HTTP 500.
- **Stack:** Python 3.11+, Playwright, Pydantic, FastAPI. The model is Gemini (`gemini-3.5-flash-lite`), called through plain
  function calling behind a one-method `ModelClient` interface.
- **Design write-up:** [`REPORT.md`](REPORT.md). **Evidence of real runs:** [`evidence/`](evidence/README.md).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.lock
pip install -e . --no-deps
playwright install chromium
cp .env.example .env                 # Windows: copy .env.example .env
cua doctor
```

`.env` holds two kinds of values:

| Variable | Needed for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | `cua discover` only | Free key from Google AI Studio. Replay, the tests and Mission Control never call a model. |
| `GEMINI_MODEL` | `cua discover` | Default `gemini-3.5-flash-lite` (`gemini-2.5-flash` is no longer offered to new keys). |
| `HERITAGE_USER`, `HERITAGE_PASS` | replay and discovery sign-in | Synthetic credentials for the **local mock bank** (pre-filled in `.env.example`). Read at run time, never written into artifacts or logs. |

`.env` is git-ignored. No real credentials or real customer data are used anywhere.

## Demo path

Start the mock bank in one terminal. Everything else talks to it:

```bash
cua mock-app                         # http://127.0.0.1:8010
```

**1. Discovery: the LLM completes a goal and records a draft capability.** It then replays the draft once, with no
LLM, to prove it works.

```bash
cua discover "Look up member 12345 in Heritage MemberServ and read their current savings balance" \
  --input member_id=12345 --output savings_balance:money --id memberserv.lookup_member_discovered
```

The draft is saved to `capabilities/memberserv.lookup_member_discovered.json`, with evidence in `runs/disc-*/`.

**2. Replay: the production path, no LLM.** Unattended replay runs only `approved` capabilities. Drafts need `--allow-draft`.

```bash
cua replay memberserv.lookup_member_discovered -i member_id=20001 --allow-draft   # the discovered draft, new input
cua replay memberserv.lookup_member -i member_id=12345                            # SUCCESS + outputs
cua replay memberserv.lookup_member -i member_id=00000                            # BUSINESS_OUTCOME member_not_found
cua replay memberserv.lookup_member -i member_id=12345 --variant nova             # same artifact, second tenant
```

The result is structured JSON, and the exit code lets a script branch without parsing it:
`0` SUCCESS, `2` BUSINESS_OUTCOME, `3` NEEDS_HUMAN, `1` FAILED.

**3. Error handling.** Inject a fault into the mock bank, then replay:

```bash
curl "http://127.0.0.1:8010/_admin/fault?mode=error500"   # also: slow | dialog | expire | none
cua replay memberserv.lookup_member -i member_id=12345                            # FAILED app_error + screenshot + DOM
```

**4. Human escalation on the live session.** Open-sub-account ends in an irreversible submit:

```bash
cua replay memberserv.open_subaccount -i member_id=12345 -i product=Savings -i opening_deposit=50 -i "nickname=Rainy day"
#   unattended: stops before submitting -> NEEDS_HUMAN
cua replay memberserv.open_subaccount -i member_id=12345 -i product=Savings -i opening_deposit=50 -i "nickname=Rainy day" --attended
#   pauses; open the operator console at http://127.0.0.1:8020 to approve, take over the visible browser, hand back or abort
```

**5. Mission Control** is the UI and the agent-facing API in one place:

```bash
cua serve                            # http://127.0.0.1:8030
```

It has:
- a capability catalog showing each contract, and every step's locators with the reason for each
- run capabilities from a form, and browse runs with their evidence and timeline
- an inbox of interventions where an operator takes control **in the browser tab** (clicks and keys go to the same session)
- the guardrails in force
- `GET /api/tools`: approved capabilities as function-calling tools, and `POST /api/tools/{name}` to invoke one
  by name with typed arguments

```bash
curl http://127.0.0.1:8030/api/tools
curl -X POST http://127.0.0.1:8030/api/tools/memberserv__lookup_member -H "Content-Type: application/json" -d '{"member_id":"12345"}'
```

## Running without live services

- **Tests** (`pytest`, about 4 minutes, 65 tests) start their own mock bank and use a scripted model. They need no API key
  and no network beyond localhost. They cover the schema, replay against every fault, guardrails, handoff (including
  a human taking over), discovery and the recorder, and Mission Control.
- **Replay and Mission Control** only need `cua mock-app`.
- **Evidence** replays can be regenerated with `python scripts/record_evidence.py` (the mock bank must be running).

## Layout

```
cua/schema      Capability artifact + replay result contract (the core types)
cua/replay      deterministic replay engine and condition checks
cua/surface     the surface seam (Protocol) + Playwright web implementation
cua/agent       discovery loop, observation, model clients (Gemini, scripted)
cua/recorder    turns a discovery run into a Capability
cua/policy      allowlist, risk classification, redaction
cua/handoff     control state machine, interventions, CLI operator console
cua/console     Mission Control server + UI
mock_app        the mock bank (two tenant skins, fault injection)
capabilities    saved artifacts (+ capability.schema.json)
evidence        real discovery runs and replay runs
```

**Windows note:** on some machines Windows won't let the bundled Playwright Chromium open a *visible* window (headless
works). Visible runs (`--attended`, `--headed`) then fall back to the installed Edge or Chrome automatically, or
you can pass `--channel msedge`.
