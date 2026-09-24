import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright

from cua.handoff.controller import SessionController
from cua.handoff.intervention import InterventionStore
from cua.policy.engine import Policy
from cua.replay.engine import Replayer
from cua.surface.web import WebSurface
from mock_app.app import app as mock_bank

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="session")
def server():
    srv = uvicorn.Server(uvicorn.Config(mock_bank, host="127.0.0.1", port=PORT, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield BASE
    srv.should_exit = True


@pytest.fixture(autouse=True)
def fresh_bank(server, monkeypatch):
    httpx.get(f"{server}/_admin/reset")
    monkeypatch.setenv("HERITAGE_USER", "teller1")
    monkeypatch.setenv("HERITAGE_PASS", "demo-only")


@pytest.fixture
def fault(server):
    return lambda mode: httpx.get(f"{server}/_admin/fault", params={"mode": mode})


@pytest.fixture
def submitted(server):
    return lambda: httpx.get(f"{server}/_admin/submitted").json()["submitted"]


@pytest.fixture
def policy():
    return Policy.from_yaml()


@pytest.fixture
async def browser():
    pw = await async_playwright().start()
    b = await pw.chromium.launch()
    yield b
    await b.close()
    await pw.stop()


@pytest.fixture
async def replay(server, policy, tmp_path, browser):
    """Unattended replay: run(cap, inputs, ...) on a fresh browser session each call."""
    async def run(cap, inputs, variant=None, approvals=(), secrets=None):
        surface = await WebSurface.open(browser, server, policy)
        r = Replayer(surface, policy, runs_dir=tmp_path, approvals=frozenset(approvals),
                     **({"secrets": secrets} if secrets is not None else {}))
        return await r.run(cap, inputs, variant)

    return run


@pytest.fixture
async def attended(server, policy, tmp_path, browser):
    """Attended replay on one live session with a SessionController. `run` returns a Task so the test can
    play the operator while the replay is paused."""
    surface = await WebSurface.open(browser, server, policy)
    ctrl = SessionController(InterventionStore(tmp_path), policy.redactor)
    await ctrl.attach(surface)

    def run(cap, inputs, timeout=15.0, approvals=()):
        r = Replayer(surface, policy, runs_dir=tmp_path, controller=ctrl, handoff_timeout_s=timeout,
                     approvals=frozenset(approvals))
        return asyncio.create_task(r.run(cap, inputs))

    return SimpleNamespace(surface=surface, ctrl=ctrl, run=run)
