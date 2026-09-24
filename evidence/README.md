# Evidence

Everything here was produced by the code in this repo against the local mock bank (`cua mock-app`).
All member data is synthetic. Before publishing, the folder was scanned for credentials, the API key,
member PII and local file paths (none found). Screenshots blank table values and typed fields, but keep row labels.

## Discovery: real LLM runs (`discovery/`)

Model: `gemini-3.5-flash-lite` (Google AI Studio free tier), text-only observation (no screenshots sent).

| Folder | Result | Turns | Tokens in / out | What it shows |
|---|---|---|---|---|
| `01-lookup-member/` | completed | 7 | 10,412 / 397 | Goal → signed in with credential placeholders, searched, read the balance. The draft artifact is `capability.json`, and `verification-replay/` replays it with no LLM: `SUCCESS`. |
| `02-open-subaccount-to-review/` | completed | 11 | 17,475 / 606 | Multi-field form. The model stopped on the review screen and did not submit; the bank recorded no submission. The draft took the product options from the real drop-down. |
| `00a-early-attempt-stuck/` | needs_human | 4 | 5,505 / 213 | First real attempt. The model kept re-typing a password it could not see was filled; stuck detection escalated after the third repeat (`intervention.json`). This led to showing `(password, filled)`. |
| `00b-early-attempt-leak-guard/` | failed | 7 | 10,412 / 388 | The goal was reached, but the model's step description quoted the member number. The recorder refused to save an artifact containing caller data. This led to templating intents. |

Commands (run with `cua mock-app` running):

```
cua discover "Look up member 12345 in Heritage MemberServ and read their current savings balance" \
  --input member_id=12345 --output savings_balance:money --id memberserv.lookup_member_discovered
cua discover "Open a new Savings sub-account for member 12345 with an opening deposit of 50 and nickname Rainy day, and reach the review screen" \
  -i member_id=12345 -i product:enum=Savings -i opening_deposit:money=50 -i "nickname=Rainy day" \
  --id memberserv.open_subaccount_discovered
```

Per run: `events.jsonl` has one line per observe / decide (tool, args, the model's reasoning, tokens) / result.
Screens are logged as structure only (controls and row labels, not values). `turn-NN.png` is the screen the model
saw at that turn. `discovery.json` is the summary, and `capability.json` is the recorded artifact.

## Replay: the production path, no LLM (`replays/`)

Regenerate with `python scripts/record_evidence.py`. Each folder has `result.json` (the structured result contract)
and `events.jsonl`. Failures and pauses also have a screenshot and a DOM snapshot.

| Folder | Status | Detail | Shows |
|---|---|---|---|
| `01-success` | SUCCESS | | Typed output returned (`savings_balance`); the sensitive `member_name` is masked in logs |
| `02-discovered-draft-other-member` | SUCCESS | | The LLM-discovered artifact, replayed for a different member |
| `03-business-outcome-not-found` | BUSINESS_OUTCOME | `member_not_found` | A legitimate answer, not a crash |
| `04-business-outcome-permission-denied` | BUSINESS_OUTCOME | `permission_denied` | Declared outcome |
| `05-invalid-input` | FAILED | `invalid_input` | Rejected before the app is touched (0 steps) |
| `06-recovered-slow-load` | SUCCESS | recovery `retry_transient` | Injected 3 s latency, waited out and recorded |
| `07-recovered-interstitial` | SUCCESS | recovery `interstitial_dismissed` | Injected notice dialog dismissed; the search is redone |
| `08-recovered-session-expiry` | SUCCESS | recovery `reauthenticated` | Injected session expiry; signed in again from env credentials |
| `09-failed-app-error` | FAILED | `app_error` | Injected HTTP 500: step, expected vs observed, screenshot, DOM. Never retried |
| `10-draft-cannot-classify-not-found` | FAILED | `checkpoint_mismatch` | Why drafts need review: without declared outcomes, "not found" is a failure |
| `11-tenant-variant-nova` | SUCCESS | | Same artifact on a second tenant's skin via its variant override |
| `12-irreversible-unattended` | NEEDS_HUMAN | | The irreversible submit does not run unattended |
| `13-human-approval-attended` | SUCCESS | recovery `human_intervention` | Paused on the live session; `ops.reviewer` approved via the operator API. `intervention.json` has the full control log |

After the replay suite, the bank's submitted list contained exactly one application: the one a human approved.

The human take-over path (an operator acts in the live browser, then hands back) is exercised by
`tests/test_handoff.py` rather than recorded here, because it needs a person clicking in the window.
