"""Minimal operator console, served in the same process as the run so it talks to the live controller.

Deliberately mocked: the human operates the real (headed) browser window directly. In production the
same endpoints would sit behind auth, and the live view would be a CDP screencast / VNC link to a
sandboxed remote browser. The control model below does not change with that transport.
"""
import asyncio
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from cua.handoff.controller import ControlError, SessionController


class OperatorAct(BaseModel):
    operator: str
    mode: str = "retry"
    note: str = ""


def build_operator_app(ctrl: SessionController) -> FastAPI:
    app = FastAPI(title="cua operator console")

    def act(fn, *args):
        try:
            fn(*args)
        except ControlError as e:
            raise HTTPException(409, str(e)) from e
        return ctrl.snapshot()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return OPERATOR_HTML

    @app.get("/api/state")
    def state():
        return ctrl.snapshot()

    @app.get("/api/live.png")
    async def live():
        if ctrl.surface is None or not ctrl.surface.alive():
            raise HTTPException(410, "the live session is closed")
        return Response(await ctrl.surface.screenshot(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/interventions/{iv_id}/screenshot")
    def paused_screenshot(iv_id: str):
        try:
            iv = ctrl.current if ctrl.current and ctrl.current.id == iv_id else ctrl.store.load(iv_id)
        except (KeyError, FileNotFoundError) as e:
            raise HTTPException(404, "unknown intervention") from e
        if not (iv.evidence_dir and iv.screenshot):
            raise HTTPException(404, "no screenshot")
        return FileResponse(Path(iv.evidence_dir) / Path(iv.screenshot).name, media_type="image/png")

    @app.post("/api/interventions/{iv_id}/claim")
    def claim(iv_id: str, body: OperatorAct):
        return act(ctrl.claim, iv_id, body.operator)

    @app.post("/api/interventions/{iv_id}/approve")
    def approve(iv_id: str, body: OperatorAct):
        return act(ctrl.approve, iv_id, body.operator, body.note)

    @app.post("/api/interventions/{iv_id}/resume")
    def resume(iv_id: str, body: OperatorAct):
        return act(ctrl.resume, iv_id, body.operator, body.mode, body.note)

    @app.post("/api/interventions/{iv_id}/abort")
    def abort(iv_id: str, body: OperatorAct):
        return act(ctrl.abort, iv_id, body.operator, body.note)

    return app


async def start_operator_server(ctrl: SessionController, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    server = uvicorn.Server(uvicorn.Config(build_operator_app(ctrl), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    return server, task


OPERATOR_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Operator Console</title>
<style>
:root{--bg:#0b1020;--panel:#121a2e;--line:#243150;--text:#e6ebf5;--muted:#8d9bb8;
--auto:#34d399;--paused:#fbbf24;--human:#60a5fa;--danger:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0;font-weight:600}.badge{padding:4px 12px;border-radius:999px;font-weight:700;font-size:12px;
letter-spacing:.06em}.AUTOMATION{background:#064e3b;color:var(--auto)}.PAUSED{background:#4a3503;color:var(--paused)}
.HUMAN{background:#0c2d57;color:var(--human)}main{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:16px 20px}
@media(max-width:900px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px}
img{width:100%;border-radius:8px;border:1px solid var(--line);background:#000}dl{margin:0;display:grid;grid-template-columns:90px 1fr;gap:4px 10px}
dt{color:var(--muted)}dd{margin:0;word-break:break-word}input{width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);
background:#0b1224;color:var(--text);margin:6px 0 10px}button{width:100%;padding:9px;margin:4px 0;border-radius:8px;border:1px solid var(--line);
background:#1b2745;color:var(--text);font-weight:600;cursor:pointer}button:disabled{opacity:.35;cursor:not-allowed}
button.primary{background:#1d4ed8;border-color:#1d4ed8}button.danger{color:var(--danger)}
ol{margin:0;padding-left:18px;max-height:220px;overflow:auto}li{margin:2px 0;color:var(--muted)}li b{color:var(--text)}
.muted{color:var(--muted)}#err{color:var(--danger);min-height:1.5em}
</style></head><body>
<header><h1>Operator Console</h1><span id="badge" class="badge">…</span><span id="holder" class="muted"></span></header>
<main><div>
<section><h2>Live session</h2><img id="live" alt="Live view of the automated browser session"></section>
</div><div>
<section id="req"><h2>Intervention</h2><div id="none" class="muted">No open request. Automation is running on its own.</div>
<div id="iv" hidden><dl><dt>Kind</dt><dd id="kind"></dd><dt>Capability</dt><dd id="cap"></dd><dt>Step</dt><dd id="step"></dd>
<dt>Why</dt><dd id="reason"></dd><dt>URL</dt><dd id="url"></dd></dl>
<p class="muted">Screenshot at pause:</p><img id="shot" alt="Screenshot taken when automation paused">
<label>Operator name<input id="op" autocomplete="name" placeholder="e.g. alice"></label>
<button id="claim" class="primary">Take control of the live session</button>
<button id="approve">Approve — automation performs this step</button>
<button id="retry">Hand back — automation retries the step</button>
<button id="skip">Hand back — I completed this step</button>
<button id="abort" class="danger">Abort run</button><div id="err" role="alert"></div>
<h2>Your recorded actions</h2><ol id="acts"></ol><h2>Control log</h2><ol id="log"></ol></div></section>
</div></main>
<script>
const $=id=>document.getElementById(id);let cur=null,shotFor=null;
const op=$('op');try{op.value=localStorage.getItem('op')||''}catch(e){}
op.addEventListener('input',()=>{try{localStorage.setItem('op',op.value)}catch(e){}});
async function post(path,extra){$('err').textContent='';
 const r=await fetch(`/api/interventions/${cur.id}/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({operator:op.value,...extra})});if(!r.ok){$('err').textContent=(await r.json()).detail}refresh()}
$('claim').onclick=()=>post('claim');$('approve').onclick=()=>post('approve');
$('retry').onclick=()=>post('resume',{mode:'retry'});$('skip').onclick=()=>post('resume',{mode:'skip'});
$('abort').onclick=()=>{if(confirm('Abort this run?'))post('abort',{note:'aborted from console'})};
function list(el,items,fmt){el.replaceChildren(...items.map(x=>{const li=document.createElement('li');fmt(li,x);return li}))}
async function refresh(){const s=await (await fetch('/api/state')).json();cur=s.intervention;
 $('badge').textContent=s.state;$('badge').className='badge '+s.state;$('holder').textContent='held by '+s.holder;
 $('none').hidden=!!cur;$('iv').hidden=!cur;if(!cur)return;
 $('kind').textContent=cur.kind==='approval'?'Approval needed (irreversible step)':'Automation is stuck';
 $('cap').textContent=cur.capability_id;$('step').textContent=cur.step_id;$('reason').textContent=cur.reason;$('url').textContent=cur.url;
 if(shotFor!==cur.id){$('shot').src=`/api/interventions/${cur.id}/screenshot`;shotFor=cur.id}
 const mine=s.state==='HUMAN';$('claim').disabled=s.state!=='PAUSED';$('approve').disabled=cur.kind!=='approval';
 $('skip').disabled=!mine;list($('acts'),cur.human_actions,(li,a)=>{const b=document.createElement('b');b.textContent=a.kind;
  li.append(b,` ${a.text||a.name||''}${a.value!==undefined?' = '+a.value:''}`)});
 list($('log'),cur.control_log,(li,e)=>{const b=document.createElement('b');b.textContent=e.to;li.append(b,` — ${e.holder}: ${e.why}`)})}
setInterval(()=>{refresh().catch(()=>{});$('live').src='/api/live.png?t='+Date.now()},1500);refresh();$('live').src='/api/live.png';
</script></body></html>"""
