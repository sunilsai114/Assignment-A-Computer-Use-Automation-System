"""Regenerate evidence/replays/ by running every replay scenario through the real CLI.

Needs the mock bank running (`cua mock-app`). No LLM is involved: these are the production-path runs.
Discovery runs are not regenerated here (they need the model); see evidence/README.md for their commands.

    python scripts/record_evidence.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evidence" / "replays"
BANK = "http://127.0.0.1:8010"
OPERATOR_PORT = 8021

LOOKUP, SUB, DRAFT = "memberserv.lookup_member", "memberserv.open_subaccount", "memberserv.lookup_member_discovered"
SUB_INPUTS = ["member_id=12345", "product=Savings", "opening_deposit=50", "nickname=Rainy day"]

# (folder, capability, inputs, extra CLI flags, injected fault, what it demonstrates)
SCENARIOS = [
    ("01-success", LOOKUP, ["member_id=12345"], [], None, "happy path: typed output returned"),
    ("02-discovered-draft-other-member", DRAFT, ["member_id=20001"], ["--allow-draft"], None,
     "the LLM-discovered artifact replays with a different input, no LLM"),
    ("03-business-outcome-not-found", LOOKUP, ["member_id=00000"], [], None, "'no such member' is an answer, not a crash"),
    ("04-business-outcome-permission-denied", LOOKUP, ["member_id=99999"], [], None, "declared outcome: permission denied"),
    ("05-invalid-input", LOOKUP, ["member_id=12 34; DROP"], [], None, "caller input rejected before touching the app"),
    ("06-recovered-slow-load", LOOKUP, ["member_id=12345"], [], "slow", "transient slowness waited out, recorded"),
    ("07-recovered-interstitial", LOOKUP, ["member_id=12345"], [], "dialog", "unexpected notice dismissed, flow resumed"),
    ("08-recovered-session-expiry", LOOKUP, ["member_id=12345"], [], "expire", "session expired: signed in again"),
    ("09-failed-app-error", LOOKUP, ["member_id=12345"], [], "error500", "hard failure with step, expected vs observed, screenshot, DOM"),
    ("10-draft-cannot-classify-not-found", DRAFT, ["member_id=00000"], ["--allow-draft"], None,
     "why drafts are reviewed: without declared outcomes, 'not found' is a checkpoint failure"),
    ("11-tenant-variant-nova", LOOKUP, ["member_id=12345"], ["--variant", "nova"], None,
     "same artifact, second tenant skin, via the variant override"),
    ("12-irreversible-unattended", SUB, SUB_INPUTS, [], None, "irreversible submit never runs unattended: NEEDS_HUMAN"),
]


def cli(*args: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "cua.cli", *args], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", **kw)


def result_of(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=300)
    start = out.find("{")
    if start < 0:
        raise RuntimeError(f"no result from CLI:\n{out}\n{err}")
    return json.loads(out[start:])


def file_run(result: dict, runs: Path, folder: str, note: str) -> None:
    dest = OUT / folder
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(result["evidence_dir"], dest)
    if result.get("intervention_id"):
        shutil.copy(runs / "interventions" / f"{result['intervention_id']}.json", dest / "intervention.json")
    for f in dest.glob("*.json*"):  # local absolute paths (they include the user's name) -> relative
        text = f.read_text(encoding="utf-8")
        for root in (str(runs), str(ROOT)):
            text = text.replace(json.dumps(root)[1:-1], ".").replace(root, ".")
        f.write_text(text, encoding="utf-8")
    code = (result.get("outcome") or {}).get("code") or (result.get("failure") or {}).get("category") or ""
    print(f"{folder:42} {result['status']:17} {code:24} {note}")


def main() -> None:
    httpx.get(f"{BANK}/_admin/reset").raise_for_status()
    OUT.mkdir(parents=True, exist_ok=True)
    runs = Path(tempfile.mkdtemp(prefix="cua-evidence-"))
    for folder, cap, inputs, flags, fault, note in SCENARIOS:
        httpx.get(f"{BANK}/_admin/reset")
        if fault:
            httpx.get(f"{BANK}/_admin/fault", params={"mode": fault})
        args = ["replay", cap, *[x for i in inputs for x in ("-i", i)], *flags, "--runs-dir", str(runs)]
        file_run(result_of(cli(*args)), runs, folder, note)

    # 13: attended run. An operator approves the irreversible step through the operator HTTP API.
    httpx.get(f"{BANK}/_admin/reset")
    proc = cli("replay", SUB, *[x for i in SUB_INPUTS for x in ("-i", i)], "--attended", "--no-headed",
               "--operator-port", str(OPERATOR_PORT), "--handoff-timeout", "120", "--runs-dir", str(runs))
    api = f"http://127.0.0.1:{OPERATOR_PORT}/api"
    for _ in range(300):
        try:
            state = httpx.get(f"{api}/state").json()
            if state["state"] == "PAUSED":
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("attended run never paused")
    iv = state["intervention"]["id"]
    httpx.post(f"{api}/interventions/{iv}/approve",
               json={"operator": "ops.reviewer", "note": "member confirmed the application by phone"}).raise_for_status()
    result = result_of(proc)
    result["intervention_id"] = iv
    file_run(result, runs, "13-human-approval-attended", "paused on the live session; approved via operator API")
    print("submitted to the bank (only the approved run):", httpx.get(f"{BANK}/_admin/submitted").json()["submitted"])
    shutil.rmtree(runs, ignore_errors=True)


if __name__ == "__main__":
    main()
