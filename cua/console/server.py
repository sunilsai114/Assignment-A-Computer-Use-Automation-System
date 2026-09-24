"""Mission Control: one local process serving
  * the reviewer/operator UI (catalog, runs, evidence, interventions, guardrails), and
  * the agent-facing capability API: approved capabilities as typed tools an AI agent can discover and call.

Every run gets its own browser context from one shared headless browser. Attended runs pause on their live
session; an operator takes control from the UI, and their clicks/keys are sent to that same session over the
`/human` channel (allowed only while they hold control). Bound to 127.0.0.1 and unauthenticated by design
for the take-home; production would put SSO in front and give the operator role its own permission.
"""
import asyncio
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from playwright.async_api import async_playwright
from pydantic import BaseModel

from cua.config import ROOT
from cua.handoff.controller import Control, ControlError, SessionController
from cua.handoff.intervention import InterventionStore
from cua.policy.engine import Policy
from cua.replay.engine import Replayer
from cua.schema.capability import Capability, Param, ParamType
from cua.schema.result import ReplayResult
from cua.surface.web import WebSurface

RUN_ID = re.compile(r"^(run|disc)-[0-9a-f]{8}$")
FILE_NAME = re.compile(r"^[\w.-]+\.(png|html)$")
STATIC = Path(__file__).parent / "static"


@dataclass
class Job:
    run_id: str
    cap: Capability
    variant: str
    attended: bool
    surface: WebSurface
    controller: SessionController | None
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    task: asyncio.Task | None = None
    result: ReplayResult | None = None
    error: str | None = None

    @property
    def status(self) -> str:
        if self.result:
            return self.result.status.value
        if self.error:
            return "FAILED"
        if self.controller and self.controller.state != Control.AUTOMATION:
            return "PAUSED"
        return "RUNNING"

    @property
    def sensitive(self) -> set[str]:
        return {o.name for o in self.cap.outputs if o.sensitive}


class Invoke(BaseModel):
    inputs: dict[str, str | int | float | bool] = {}
    variant: str | None = None
    attended: bool = False
    allow_draft: bool = False
    wait: bool = False


class OperatorAct(BaseModel):
    operator: str
    mode: str = "retry"
    note: str = ""


class HumanInput(BaseModel):
    operator: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None


# ───────────────────────── capability -> tool contract ─────────────────────────
def tool_name(cap_id: str) -> str:
    return cap_id.replace(".", "__")  # function-calling names allow [A-Za-z0-9_-]


def param_schema(p: Param) -> dict:
    s: dict = {"description": p.description}
    if p.type == ParamType.int:
        s["type"] = "integer"
    elif p.type == ParamType.bool:
        s["type"] = "boolean"
    elif p.type == ParamType.enum:
        s.update(type="string", enum=p.enum)
    elif p.type == ParamType.money:
        s.update(type="string", pattern=r"^\$?[0-9][0-9,]*(\.[0-9]{1,2})?$", description=f"{p.description} (USD)")
    else:
        s["type"] = "string"
        if p.pattern:
            s["pattern"] = f"^(?:{p.pattern})$"
    return s


def tool_def(cap: Capability) -> dict:
    outcomes = "; ".join(f"{o.code} ({o.description.rstrip('.')})" for o in cap.outcomes) or "none declared"
    return {
        "name": tool_name(cap.meta.id), "capability_id": cap.meta.id, "version": cap.meta.version,
        "description": f"{cap.meta.name.rstrip('.')}. {cap.meta.description.rstrip('.')}. "
                       f"Business outcomes it can return: {outcomes}.",
        "parameters": {"type": "object", "properties": {p.name: param_schema(p) for p in cap.inputs},
                       "required": [p.name for p in cap.inputs if p.required], "additionalProperties": False},
        "returns": {"status": "SUCCESS | BUSINESS_OUTCOME | NEEDS_HUMAN | FAILED",
                    "outputs": {o.name: o.type.value for o in cap.outputs}},
        "requires_human_approval": cap.approval_required_steps,
        "variants": [cap.meta.variant, *(v.variant for v in cap.variants)],
    }


def summary(cap: Capability) -> dict:
    return {"id": cap.meta.id, "name": cap.meta.name, "description": cap.meta.description, "version": cap.meta.version,
            "status": cap.meta.status, "app": cap.meta.app, "created_by": cap.meta.created_by,
            "variants": [cap.meta.variant, *(v.variant for v in cap.variants)], "max_risk": cap.max_risk.value,
            "approval_steps": cap.approval_required_steps, "steps": len(cap.steps),
            "inputs": [{"name": p.name, "type": p.type.value, "required": p.required, "enum": p.enum,
                        "pattern": p.pattern, "description": p.description, "default": p.default} for p in cap.inputs],
            "outputs": [{"name": o.name, "type": o.type.value, "sensitive": o.sensitive, "description": o.description}
                        for o in cap.outputs],
            "outcomes": [{"code": o.code, "description": o.description} for o in cap.outcomes]}


# ───────────────────────── app ─────────────────────────
def build_app(base_url: str = "http://127.0.0.1:8010", runs_dir: Path = ROOT / "runs",
              cap_dir: Path = ROOT / "capabilities", policy: Policy | None = None,
              handoff_timeout_s: float = 900) -> FastAPI:
    policy = policy or Policy.from_yaml()
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    store = InterventionStore(runs_dir)
    jobs: dict[str, Job] = {}
    rt: dict = {}

    @asynccontextmanager
    async def lifespan(_app):
        rt["pw"] = await async_playwright().start()
        rt["browser"] = await rt["pw"].chromium.launch()
        yield
        for j in jobs.values():
            if j.task and not j.task.done():
                j.task.cancel()
        await rt["browser"].close()
        await rt["pw"].stop()

    app = FastAPI(title="cua mission control", lifespan=lifespan)

    def capabilities() -> tuple[dict[str, Capability], dict[str, str]]:
        ok, bad = {}, {}
        for path in sorted(Path(cap_dir).glob("*.json")):
            if path.name.endswith(".schema.json"):
                continue
            try:
                c = Capability.from_json(path.read_text(encoding="utf-8"))
                ok[c.meta.id] = c
            except Exception as e:  # noqa: BLE001  an invalid artifact is shown, never executed
                bad[path.name] = str(e).splitlines()[0]
        return ok, bad

    def get_cap(cap_id: str) -> Capability:
        cap = capabilities()[0].get(cap_id)
        if cap is None:
            raise HTTPException(404, f"no capability '{cap_id}'")
        return cap

    def get_job(run_id: str) -> Job:
        if run_id not in jobs:
            raise HTTPException(404, "not a live run")
        return jobs[run_id]

    def run_dir(run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise HTTPException(400, "bad run id")
        return runs_dir / run_id

    async def invoke(cap: Capability, body: Invoke) -> Job:
        if cap.meta.status != "approved" and not body.allow_draft:
            raise HTTPException(403, f"'{cap.meta.id}' is a {cap.meta.status}; approve it before production use")
        variants = [cap.meta.variant, *(v.variant for v in cap.variants)]
        variant = body.variant or cap.meta.variant
        if variant not in variants:
            raise HTTPException(422, f"unknown variant '{variant}'; available: {variants}")
        surface = await WebSurface.open(rt["browser"], base_url, policy)
        ctrl = None
        if body.attended:
            ctrl = SessionController(store, policy.redactor)
            await ctrl.attach(surface)
        job = Job(run_id=f"run-{uuid.uuid4().hex[:8]}", cap=cap, variant=variant, attended=body.attended,
                  surface=surface, controller=ctrl)
        inputs = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in body.inputs.items()}
        replayer = Replayer(surface, policy, runs_dir=runs_dir, controller=ctrl, handoff_timeout_s=handoff_timeout_s)

        async def go():
            try:
                job.result = await replayer.run(cap, inputs, None if variant == cap.meta.variant else variant,
                                                run_id=job.run_id)
            except Exception as e:  # noqa: BLE001
                job.error = f"{type(e).__name__}: {e}"
            finally:
                await surface.close()

        jobs[job.run_id] = job
        job.task = asyncio.create_task(go())
        return job

    # ── agent-facing API ──
    @app.get("/api/tools")
    def tools():
        """Approved capabilities as function-calling tool definitions. Drafts are never offered to agents."""
        return [tool_def(c) for c in capabilities()[0].values() if c.meta.status == "approved"]

    @app.post("/api/tools/{name}")
    async def call_tool(name: str, args: dict):
        """An agent invokes a capability by tool name with typed args and gets the full result contract back
        (unmasked outputs: the caller is entitled to them; logs and the UI see them masked)."""
        cap = next((c for c in capabilities()[0].values() if tool_name(c.meta.id) == name), None)
        if cap is None or cap.meta.status != "approved":
            raise HTTPException(404, f"no approved capability exposed as tool '{name}'")
        variant = args.pop("_variant", None)
        job = await invoke(cap, Invoke(inputs=args, variant=variant))
        await job.task
        if job.result is None:
            raise HTTPException(500, job.error or "run did not produce a result")
        return job.result.model_dump(mode="json")

    # ── catalog ──
    @app.get("/api/capabilities")
    def list_capabilities():
        ok, bad = capabilities()
        return {"capabilities": [summary(c) for c in ok.values()], "invalid": bad}

    @app.get("/api/capabilities/{cap_id}")
    def capability(cap_id: str):
        cap = get_cap(cap_id)
        return {"summary": summary(cap), "tool": tool_def(cap), "artifact": cap.model_dump(mode="json", exclude_none=True)}

    @app.post("/api/capabilities/{cap_id}/invoke")
    async def invoke_capability(cap_id: str, body: Invoke):
        job = await invoke(get_cap(cap_id), body)
        if body.wait:
            await job.task
        return {"run_id": job.run_id, "status": job.status}

    # ── runs & evidence ──
    def record_of(d: Path) -> dict | None:
        try:
            if (d / "result.json").exists():
                r = json.loads((d / "result.json").read_text(encoding="utf-8"))
                return {"id": d.name, "kind": "replay", "status": r["status"], "capability": r["capability_id"],
                        "version": r["capability_version"], "variant": r["variant"], "started": r["started_at"],
                        "duration_ms": r["duration_ms"],
                        "detail": (r.get("outcome") or {}).get("code") or (r.get("failure") or {}).get("category") or ""}
            if (d / "discovery.json").exists():
                r = json.loads((d / "discovery.json").read_text(encoding="utf-8"))
                first = (d / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
                return {"id": d.name, "kind": "discovery", "status": r["status"], "capability": r.get("goal", ""),
                        "version": r.get("model", ""), "variant": "", "started": json.loads(first)["ts"],
                        "duration_ms": None, "detail": f"{r['turns']} turns"}
        except (OSError, ValueError, KeyError, IndexError):
            return None
        return None

    def live_record(j: Job) -> dict:
        return {"id": j.run_id, "kind": "replay", "status": j.status, "capability": j.cap.meta.id,
                "version": j.cap.meta.version, "variant": j.variant, "started": j.started, "duration_ms": None,
                "detail": "attended" if j.attended else ""}

    @app.get("/api/runs")
    def runs():
        live = [live_record(j) for j in jobs.values() if not j.result and not j.error]
        done = [record_of(d) for d in runs_dir.iterdir() if d.is_dir() and RUN_ID.match(d.name)]
        done = [r for r in done if r and r["id"] not in {x["id"] for x in live}]
        done.sort(key=lambda r: r["started"] or "", reverse=True)
        return live + done[:150]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        d = run_dir(run_id)
        j = jobs.get(run_id)
        events = []
        if (d / "events.jsonl").exists():
            events = [json.loads(x) for x in (d / "events.jsonl").read_text(encoding="utf-8").splitlines()[-400:]]
        result = None
        if j and j.result:
            result = j.result.for_log(j.sensitive)
        elif (d / "result.json").exists():
            result = json.loads((d / "result.json").read_text(encoding="utf-8"))
        discovery = json.loads((d / "discovery.json").read_text(encoding="utf-8")) if (d / "discovery.json").exists() else None
        if not (j or events or result or discovery):
            raise HTTPException(404, "unknown run")
        control = j.controller.snapshot() if j and j.controller else None
        return {"id": run_id, "status": j.status if j else (result or {}).get("status") or (discovery or {}).get("status"),
                "live": bool(j and not j.result and not j.error), "attended": bool(j and j.attended),
                "error": j.error if j else None, "capability": (j.cap.meta.id if j else (result or {}).get("capability_id")),
                "result": result, "discovery": discovery, "events": events, "control": control,
                "files": sorted(p.name for p in d.glob("*") if FILE_NAME.match(p.name)) if d.exists() else []}

    @app.get("/api/runs/{run_id}/files/{name}")
    def run_file(run_id: str, name: str):
        path = run_dir(run_id) / name
        if not FILE_NAME.match(name) or not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path, media_type="image/png" if name.endswith(".png") else "text/plain")

    @app.get("/api/runs/{run_id}/live.png")
    async def live(run_id: str):
        j = get_job(run_id)
        if not j.surface.alive():
            raise HTTPException(410, "session closed")
        return Response(await j.surface.screenshot(), media_type="image/png", headers={"Cache-Control": "no-store"})

    # ── operator ──
    def operate(run_id: str, fn: str, *args):
        j = get_job(run_id)
        ctrl = j.controller
        if ctrl is None or ctrl.current is None:
            raise HTTPException(409, "this run is not waiting for a human")
        try:
            getattr(ctrl, fn)(ctrl.current.id, *args)
        except ControlError as e:
            raise HTTPException(409, str(e)) from e
        return ctrl.snapshot()

    @app.post("/api/runs/{run_id}/operator/{action}")
    def operator(run_id: str, action: str, body: OperatorAct):
        if action == "claim":
            return operate(run_id, "claim", body.operator)
        if action == "approve":
            return operate(run_id, "approve", body.operator, body.note)
        if action == "resume":
            return operate(run_id, "resume", body.operator, body.mode, body.note)
        if action == "abort":
            return operate(run_id, "abort", body.operator, body.note)
        raise HTTPException(404, "unknown action")

    @app.post("/api/runs/{run_id}/human")
    async def human(run_id: str, body: HumanInput):
        j = get_job(run_id)
        if j.controller is None:
            raise HTTPException(409, "not an attended run")
        try:
            j.controller.assert_human(body.operator)
            if body.x is not None and body.y is not None:
                await j.surface.human_click(body.x, body.y)
            elif body.text:
                await j.surface.human_type(body.text)
            elif body.key:
                await j.surface.human_key(body.key)
            else:
                raise HTTPException(422, "send x/y, text or key")
        except ControlError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        await j.surface.settle(3000)
        return j.controller.snapshot()

    @app.get("/api/interventions")
    def interventions():
        out = []
        for j in jobs.values():
            c = j.controller
            if c and c.current:
                iv = asdict(c.current)
                out.append({"run_id": j.run_id, "capability": j.cap.meta.id, "state": c.state.value, "holder": c.holder,
                            **{k: iv[k] for k in ("id", "kind", "reason", "step_id", "created_at")}})
        return out

    # ── overview & guardrails ──
    @app.get("/api/overview")
    async def overview():
        ok, bad = capabilities()
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                bank = (await c.get(base_url)).status_code < 500
        except httpx.HTTPError:
            bank = False
        return {"base_url": base_url, "bank_reachable": bank, "browser": bool(rt.get("browser")),
                "capabilities": {"approved": sum(c.meta.status == "approved" for c in ok.values()),
                                 "draft": sum(c.meta.status == "draft" for c in ok.values()), "invalid": len(bad)},
                "interventions": len(interventions()), "live_runs": sum(1 for j in jobs.values() if not j.result),
                "time": time.time()}

    @app.get("/api/policy")
    def guardrails():
        c = policy.cfg
        return {"allowed_hosts": c["allowed_hosts"], "allowed_paths": c["allowed_paths"],
                "allowed_actions": c["allowed_actions"], "irreversible_markers": c["irreversible_markers"],
                "irreversible_policy": c.get("irreversible_policy"), "app_error_markers": c.get("app_error_markers", []),
                "redaction": {k: v for k, v in c["redact"].items()}, "limits": c["limits"]}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    return app
