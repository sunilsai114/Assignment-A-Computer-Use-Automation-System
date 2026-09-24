"""Mission Control: agent-facing tool API, draft gate, masking, and the attended operator flow over HTTP."""
import threading
import time

import httpx
import pytest
import uvicorn

from cua.console.server import build_app

PORT = 8031
MC = f"http://127.0.0.1:{PORT}"
SUB = {"member_id": "12345", "product": "Savings", "opening_deposit": "50", "nickname": "Rainy day"}


@pytest.fixture(scope="module")
def mc(server, tmp_path_factory):
    app = build_app(base_url=server, runs_dir=tmp_path_factory.mktemp("mc-runs"), handoff_timeout_s=60)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    with httpx.Client(base_url=MC, timeout=90) as c:
        yield c
    srv.should_exit = True


def wait_for(c, run_id, predicate, timeout=60):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        d = c.get(f"/api/runs/{run_id}").json()
        if predicate(d):
            return d
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} never reached the expected state; last: {d['status']}")


def test_tools_expose_only_approved_capabilities_with_typed_contracts(mc):
    tools = {t["name"]: t for t in mc.get("/api/tools").json()}
    assert set(tools) == {"memberserv__lookup_member", "memberserv__open_subaccount"}  # drafts are never offered
    look = tools["memberserv__lookup_member"]
    assert look["parameters"]["required"] == ["member_id"] and look["returns"]["outputs"]["savings_balance"] == "money"
    assert "member_not_found" in look["description"] and "nova" in look["variants"]
    sub = tools["memberserv__open_subaccount"]
    assert sub["parameters"]["properties"]["product"]["enum"] == ["Savings", "Checking", "Money Market"]
    assert sub["requires_human_approval"] == ["submit_application"]


def test_agent_invokes_by_name_and_gets_the_result_contract(mc):
    r = mc.post("/api/tools/memberserv__lookup_member", json={"member_id": "12345"}).json()
    assert r["status"] == "SUCCESS" and r["outputs"] == {"savings_balance": "4821.37", "member_name": "Jordan Rivera"}
    # the same run, seen through the UI API, has the sensitive output masked
    ui = mc.get(f"/api/runs/{r['run_id']}").json()
    assert ui["result"]["outputs"]["member_name"] == "●●●●"
    nf = mc.post("/api/tools/memberserv__lookup_member", json={"member_id": "00000"}).json()
    assert nf["status"] == "BUSINESS_OUTCOME" and nf["outcome"]["code"] == "member_not_found"
    nova = mc.post("/api/tools/memberserv__lookup_member", json={"member_id": "20001", "_variant": "nova"}).json()
    assert nova["status"] == "SUCCESS" and nova["variant"] == "nova"


def test_drafts_cannot_run_in_production(mc):
    assert mc.post("/api/capabilities/memberserv.lookup_member_discovered/invoke", json={}).status_code == 403
    assert mc.post("/api/tools/memberserv__lookup_member_discovered", json={}).status_code == 404
    ok = mc.post("/api/capabilities/memberserv.lookup_member_discovered/invoke",
                 json={"inputs": {"member_id": "12345"}, "allow_draft": True, "wait": True}).json()
    assert ok["status"] == "SUCCESS"


def test_attended_run_operator_flow_over_http(mc, submitted):
    run_id = mc.post("/api/capabilities/memberserv.open_subaccount/invoke", json={"inputs": SUB, "attended": True}).json()["run_id"]
    d = wait_for(mc, run_id, lambda d: d["status"] == "PAUSED")
    assert d["control"]["intervention"]["kind"] == "approval"
    assert [i["run_id"] for i in mc.get("/api/interventions").json()] == [run_id]
    assert mc.get(f"/api/runs/{run_id}/live.png").headers["content-type"] == "image/png"

    human = f"/api/runs/{run_id}/human"
    assert mc.post(human, json={"operator": "alice", "key": "Tab"}).status_code == 409   # must take control first
    assert mc.post(f"/api/runs/{run_id}/operator/claim", json={"operator": "alice"}).json()["state"] == "HUMAN"
    assert mc.post(human, json={"operator": "bob", "key": "Tab"}).status_code == 409     # bob does not hold it
    assert mc.post(human, json={"operator": "alice", "key": "Tab"}).status_code == 200
    assert mc.post(human, json={"operator": "alice", "key": "F12"}).status_code == 422   # keys are allowlisted
    assert mc.post(f"/api/runs/{run_id}/operator/approve", json={"operator": "bob"}).status_code == 409
    assert submitted() == []
    assert mc.post(f"/api/runs/{run_id}/operator/approve", json={"operator": "alice"}).status_code == 200

    d = wait_for(mc, run_id, lambda d: not d["live"])
    assert d["status"] == "SUCCESS" and submitted() == ["12345"]
    assert any(r["kind"] == "human_intervention" for r in d["result"]["recoveries"])


def test_run_and_file_paths_are_validated(mc):
    assert mc.get("/api/runs/..%2F..%2Fetc").status_code in (400, 404)
    assert mc.get("/api/runs/run-00000000/files/..%2F.env").status_code == 404
    assert mc.get("/api/runs/run-00000000/files/result.json").status_code == 404  # only png/html are served
