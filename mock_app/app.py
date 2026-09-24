"""Mock bank back-office. Two tenant skins over one 'vendor product':
   /heritage  legacy: frameset, nested tables, cryptic field names, no ids/test-ids
   /nova      modern: semantic, labelled, different wording and routes
Faults are switched via /_admin/fault?mode=... to exercise replay error handling."""
import asyncio
import secrets
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mock_app import data

app = FastAPI(title="mock-bank")
STATE = {"fault": "none", "sessions": set(), "notice_seen": set(), "submitted": []}


def page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status)


def sid_of(req: Request) -> str | None:
    s = req.cookies.get("sid")
    return s if s in STATE["sessions"] else None


@app.get("/_admin/fault")
def set_fault(mode: str = "none"):
    if mode not in data.FAULTS:
        return {"error": f"mode must be one of {sorted(data.FAULTS)}"}
    STATE["fault"] = mode
    STATE["notice_seen"].clear()
    return {"fault": mode}


@app.get("/_admin/reset")
def reset():
    STATE.update(fault="none", sessions=set(), notice_seen=set(), submitted=[])
    return {"ok": True}


@app.get("/_admin/submitted")
def submitted():
    """Lets tests and evidence prove an irreversible action never fired."""
    return {"submitted": STATE["submitted"]}


async def gate(req: Request, login_url: str):
    """Auth + fault handling shared by authenticated pages. Returns a Response to short-circuit."""
    sid = sid_of(req)
    if not sid:
        return RedirectResponse(f"{login_url}?msg=expired", status_code=303)
    fault = STATE["fault"]
    if fault == "expire":
        STATE["sessions"].discard(sid)
        STATE["fault"] = "none"
        return RedirectResponse(f"{login_url}?msg=expired", status_code=303)
    if fault == "slow":
        await asyncio.sleep(3)
    return None


def try_login(user: str, pw: str) -> str | None:
    if user == data.DEMO_USER and pw == data.DEMO_PASS:
        s = secrets.token_hex(8)
        STATE["sessions"].add(s)
        return s
    return None


# ───────────────────────── Heritage CU (legacy) ─────────────────────────
H = "/heritage"
STYLE_H = ("<style>body{font-family:Verdana;font-size:11px;background:#d8d8c8}"
           "td{font-size:11px}.e{color:#c00000;font-weight:bold}</style>")


def h_wrap(inner: str) -> str:
    return (f"<html><head>{STYLE_H}</head><body><table width=100% cellpadding=6 border=1><tr><td>"
            f"<table width=100%><tr><td><font size=2>{inner}</font></td></tr></table>"
            "</td></tr></table></body></html>")


@app.get(f"{H}/")
def h_frames():
    return page(f"""<html><head><title>Heritage Credit Union - MemberServ 4.2</title></head>
<frameset rows="60,*" border=0><frame src="{H}/top" name="t">
<frameset cols="170,*"><frame src="{H}/nav" name="n"><frame src="{H}/login" name="m"></frameset>
</frameset></html>""")


@app.get(f"{H}/top")
def h_top():
    return page("<html><body bgcolor=#003366><font color=white size=4><b>&nbsp;HERITAGE CREDIT UNION</b>"
                "</font><font color=#99ccff size=1> MemberServ v4.2</font></body></html>")


@app.get(f"{H}/nav")
def h_nav():
    return page(f"""<html><head>{STYLE_H}</head><body bgcolor=#e8e8d8><table cellpadding=4>
<tr><td><a href="{H}/search" target="m">Member Inquiry</a></td></tr>
<tr><td><a href="{H}/login" target="m">Sign Off</a></td></tr></table></body></html>""")


@app.get(f"{H}/login")
def h_login(msg: str = ""):
    note = {"expired": "Your session has expired. Please sign on again.",
            "bad": "Invalid sign-on. Try again."}.get(msg, "")
    return page(h_wrap(f"""<span class=e>{note}</span>
<form method=post action="{H}/login"><table>
<tr><td>Operator ID</td><td><input name="u1"></td></tr>
<tr><td>Passcode</td><td><input type=password name="u2"></td></tr>
<tr><td></td><td><input type=submit value="Sign On"></td></tr></table></form>"""))


@app.post(f"{H}/login")
def h_login_post(u1: str = Form(""), u2: str = Form("")):
    s = try_login(u1, u2)
    if not s:
        return RedirectResponse(f"{H}/login?msg=bad", status_code=303)
    r = RedirectResponse(f"{H}/search", status_code=303)
    r.set_cookie("sid", s, httponly=True)
    return r


@app.get(f"{H}/search")
async def h_search(req: Request):
    if (r := await gate(req, f"{H}/login")):
        return r
    return page(h_wrap(f"""<b>Member Inquiry</b><br><form method=post action="{H}/member">
<table><tr><td>Member Number</td><td><input name="f1" size=12></td>
<td><input type=submit value="Go"></td></tr></table></form>"""))


def h_notice(sid: str) -> str | None:
    if STATE["fault"] == "dialog" and sid not in STATE["notice_seen"]:
        return h_wrap(f"""<table border=1 cellpadding=8 bgcolor=#ffffcc><tr><td>
<b>NOTICE:</b> Rate sheet updated 09/01. Please review before continuing.<br><br>
<a href="{H}/ack">Continue</a></td></tr></table>""")
    return None


@app.get(f"{H}/ack")
def h_ack(req: Request):
    if sid := sid_of(req):
        STATE["notice_seen"].add(sid)
    return RedirectResponse(f"{H}/search", status_code=303)


@app.post(f"{H}/member")
async def h_member(req: Request, f1: str = Form("")):
    if (r := await gate(req, f"{H}/login")):
        return r
    if (n := h_notice(sid_of(req))):
        return page(n)
    mid = f1.strip()
    if STATE["fault"] == "error500":
        return page(h_wrap("<span class=e>An unexpected error occurred. Ref: ERR-4471</span>"), 500)
    if not mid.isdigit():
        return page(h_wrap(f"<span class=e>Member Number must be numeric.</span><br><a href='{H}/search'>Back</a>"))
    if mid in data.DENIED_MEMBERS:
        return page(h_wrap("<span class=e>Access denied - insufficient privileges for this record.</span>"))
    m = data.MEMBERS.get(mid)
    if not m:
        return page(h_wrap("<span class=e>No records match your request.</span>"
                           f"<br><a href='{H}/search'>Back</a>"))
    rows = "".join(f"<tr><td>{p}</td><td>{a}</td><td align=right>${bal}</td></tr>" for p, a, bal in m["accounts"])
    return page(h_wrap(f"""<b>Member {escape(mid)}</b>
<table border=1 cellpadding=3><tr><td>Name</td><td>{m['name']}</td></tr>
<tr><td>SSN</td><td>{m['ssn']}</td></tr><tr><td>E-mail</td><td>{m['email']}</td></tr>
<tr><td>Phone</td><td>{m['phone']}</td></tr></table><br>
<table border=1 cellpadding=3><tr bgcolor=#cccccc><td>Product</td><td>Account</td><td>Balance</td></tr>{rows}</table><br>
<span onclick="location.href='{H}/newacct?m={escape(mid)}'" style="cursor:pointer;color:blue;text-decoration:underline">
New Sub-Account</span>"""))


@app.get(f"{H}/newacct")
async def h_newacct(req: Request, m: str = "", err: str = ""):
    if (r := await gate(req, f"{H}/login")):
        return r
    opts = "".join(f"<option>{p}</option>" for p in data.PRODUCTS)
    return page(h_wrap(f"""<b>Open Sub-Account</b> for member {escape(m)}<br><span class=e>{escape(err)}</span>
<form method=post action="{H}/newacct/review"><input type=hidden name="m" value="{escape(m)}"><table>
<tr><td>Product Type</td><td><select name="p1">{opts}</select></td></tr>
<tr><td>Opening Deposit</td><td><input name="p2" size=10></td></tr>
<tr><td>Nickname</td><td><input name="p3"></td></tr>
<tr><td></td><td><input type=submit value="Continue"></td></tr></table></form>"""))


@app.post(f"{H}/newacct/review")
async def h_review(req: Request, m: str = Form(""), p1: str = Form(""), p2: str = Form(""), p3: str = Form("")):
    if (r := await gate(req, f"{H}/login")):
        return r
    try:
        amt = float(p2.replace(",", "").replace("$", ""))
    except ValueError:
        return RedirectResponse(f"{H}/newacct?m={m}&err=Opening+deposit+must+be+a+number", status_code=303)
    if amt < data.MIN_DEPOSIT:
        return RedirectResponse(f"{H}/newacct?m={m}&err=Minimum+opening+deposit+is+$25.00", status_code=303)
    return page(h_wrap(f"""<b>Review Sub-Account Application</b><table border=1 cellpadding=3>
<tr><td>Member</td><td>{escape(m)}</td></tr><tr><td>Product</td><td>{escape(p1)}</td></tr>
<tr><td>Deposit</td><td>${amt:,.2f}</td></tr><tr><td>Nickname</td><td>{escape(p3)}</td></tr></table><br>
<form method=post action="{H}/newacct/submit"><input type=hidden name="m" value="{escape(m)}">
<input type=submit value="Submit Application"></form>"""))


@app.post(f"{H}/newacct/submit")
async def h_submit(req: Request, m: str = Form("")):
    """Irreversible: the agent must never reach this without human approval."""
    if (r := await gate(req, f"{H}/login")):
        return r
    STATE["submitted"].append(m)
    ref = f"SA-{90210 + len(STATE['submitted'])}"
    return page(h_wrap(f"<b>Application submitted.</b><br><table border=1 cellpadding=3>"
                       f"<tr><td>Reference</td><td>{ref}</td></tr></table>"))


# ───────────────────────── Nova Bank (modern skin, same product) ─────────────────────────
N = "/nova"
STYLE_N = """<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;max-width:640px;margin:40px auto}
input,select,button{padding:8px;margin:4px 0;border-radius:6px;border:1px solid #475569;background:#1e293b;color:#e2e8f0}
button{background:#2563eb;cursor:pointer}.err{color:#f87171}</style>"""


def n_wrap(title: str, inner: str) -> str:
    return f"<html><head><title>{title} - Nova Bank</title>{STYLE_N}</head><body><h1>{title}</h1>{inner}</body></html>"


@app.get(f"{N}/login")
def n_login(msg: str = ""):
    note = {"expired": "Session timed out. Sign in again.", "bad": "Incorrect credentials."}.get(msg, "")
    return page(n_wrap("Sign in", f"""<p class=err role=alert>{note}</p><form method=post action="{N}/login">
<label>Username <input name="username"></label><br><label>Password <input type=password name="password"></label><br>
<button>Sign in</button></form>"""))


@app.post(f"{N}/login")
def n_login_post(username: str = Form(""), password: str = Form("")):
    s = try_login(username, password)
    if not s:
        return RedirectResponse(f"{N}/login?msg=bad", status_code=303)
    r = RedirectResponse(f"{N}/customers", status_code=303)
    r.set_cookie("sid", s, httponly=True)
    return r


@app.get(f"{N}/customers")
async def n_customers(req: Request, q: str = ""):
    if (r := await gate(req, f"{N}/login")):
        return r
    if not q:
        return page(n_wrap("Customers", f"""<form method=get action="{N}/customers">
<label>Customer ID <input name="q"></label> <button>Look up</button></form>"""))
    if STATE["fault"] == "error500":
        return page(n_wrap("Error", "<p class=err role=alert>Something went wrong. Code ERR-4471</p>"), 500)
    if q in data.DENIED_MEMBERS:
        return page(n_wrap("Customers", "<p class=err role=alert>You do not have permission to view this customer.</p>"))
    if q not in data.MEMBERS:
        return page(n_wrap("Customers", "<p class=err role=alert>Customer not found.</p>"))
    return RedirectResponse(f"{N}/customers/{q}", status_code=303)


@app.get(N + "/customers/{cid}")
async def n_detail(req: Request, cid: str):
    if (r := await gate(req, f"{N}/login")):
        return r
    m = data.MEMBERS.get(cid)
    if not m:
        return RedirectResponse(f"{N}/customers?q={cid}", status_code=303)
    rows = "".join(f"<tr><td>{p}</td><td>{a}</td><td>${b}</td></tr>" for p, a, b in m["accounts"])
    return page(n_wrap(m["name"], f"""<p>SSN {m['ssn']} · {m['email']}</p>
<table><caption>Accounts</caption><tr><th>Type</th><th>Number</th><th>Current balance</th></tr>{rows}</table>
<p><a href="{N}/customers/{cid}/accounts/new">Add account</a></p>"""))


@app.get(N + "/customers/{cid}/accounts/new")
async def n_new(req: Request, cid: str, err: str = ""):
    if (r := await gate(req, f"{N}/login")):
        return r
    opts = "".join(f"<option>{p}</option>" for p in data.PRODUCTS)
    return page(n_wrap("Add account", f"""<p class=err role=alert>{escape(err)}</p>
<form method=post action="{N}/customers/{cid}/accounts/review">
<label>Account type <select name="product">{opts}</select></label><br>
<label>Initial deposit <input name="deposit"></label><br>
<label>Label <input name="label"></label><br><button>Review</button></form>"""))


@app.post(N + "/customers/{cid}/accounts/review")
async def n_review(req: Request, cid: str, product: str = Form(""), deposit: str = Form(""), label: str = Form("")):
    if (r := await gate(req, f"{N}/login")):
        return r
    try:
        amt = float(deposit.replace(",", "").replace("$", ""))
    except ValueError:
        return RedirectResponse(f"{N}/customers/{cid}/accounts/new?err=Deposit+must+be+a+number", status_code=303)
    if amt < data.MIN_DEPOSIT:
        return RedirectResponse(f"{N}/customers/{cid}/accounts/new?err=Minimum+deposit+is+$25.00", status_code=303)
    return page(n_wrap("Confirm new account", f"""<dl><dt>Customer</dt><dd>{cid}</dd><dt>Type</dt><dd>{escape(product)}</dd>
<dt>Deposit</dt><dd>${amt:,.2f}</dd><dt>Label</dt><dd>{escape(label)}</dd></dl>
<form method=post action="{N}/customers/{cid}/accounts/create"><button>Create account</button></form>"""))


@app.post(N + "/customers/{cid}/accounts/create")
async def n_create(cid: str):
    STATE["submitted"].append(cid)
    return page(n_wrap("Done", "<p>Account created. Ref SA-90212.</p>"))


@app.get("/")
def index():
    return page(f"<h2>mock-bank</h2><a href='{H}/'>Heritage CU (legacy)</a> · <a href='{N}/login'>Nova Bank</a>")
