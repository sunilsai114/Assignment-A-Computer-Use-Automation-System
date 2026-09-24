import asyncio
import json
from pathlib import Path

import typer
from dotenv import load_dotenv

from cua.config import ROOT, Settings

app = typer.Typer(help="Computer-use automation: discover, replay, escalate.", no_args_is_help=True)

# Exit codes let a calling agent/script branch without parsing JSON.
EXIT = {"SUCCESS": 0, "FAILED": 1, "BUSINESS_OUTCOME": 2, "NEEDS_HUMAN": 3}


def load_capability(ref: str):
    from cua.schema.capability import Capability
    path = Path(ref) if Path(ref).suffix == ".json" else ROOT / "capabilities" / f"{ref}.json"
    if not path.exists():
        raise typer.BadParameter(f"no capability at {path}")
    return Capability.from_json(path.read_text(encoding="utf-8"))


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"input '{p}' must look like name=value")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


@app.command()
def doctor() -> None:
    """Check the environment."""
    load_dotenv(ROOT / ".env")
    import os
    s = Settings()
    typer.echo(f"gemini key: {'set' if s.gemini_api_key else 'MISSING (needed only for discover)'}")
    creds = all(os.environ.get(k) for k in ("HERITAGE_USER", "HERITAGE_PASS"))
    typer.echo(f"mock-app credentials: {'set' if creds else 'MISSING (copy .env.example to .env)'}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        typer.echo("browser: ok")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"browser: FAILED ({e}); run `playwright install chromium`")


@app.command("mock-app")
def mock_app(port: int = 8010) -> None:
    """Serve the mock bank (Heritage CU legacy + Nova Bank) on localhost."""
    import uvicorn
    uvicorn.run("mock_app.app:app", host="127.0.0.1", port=port, log_level="warning")


@app.command("list")
def list_capabilities() -> None:
    """List saved capabilities and their contracts."""
    from cua.schema.capability import Capability
    for path in sorted((ROOT / "capabilities").glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        try:
            c = Capability.from_json(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            typer.echo(f"{path.name}: INVALID ({str(e).splitlines()[0]})")
            continue
        variants = ", ".join([c.meta.variant, *(v.variant for v in c.variants)])
        typer.echo(f"{c.meta.id} v{c.meta.version} [{c.meta.status}] variants: {variants}  max risk: {c.max_risk.value}")
        typer.echo(f"    inputs:   {', '.join(f'{p.name}:{p.type.value}' for p in c.inputs) or '-'}")
        typer.echo(f"    outputs:  {', '.join(f'{o.name}:{o.type.value}' for o in c.outputs) or '-'}")
        typer.echo(f"    outcomes: {', '.join(o.code for o in c.outcomes) or '-'}")
        if c.approval_required_steps:
            typer.echo(f"    needs human approval: {', '.join(c.approval_required_steps)}")


@app.command()
def replay(
    capability: str = typer.Argument(..., help="Capability id (from capabilities/) or a path to its JSON."),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Input as name=value; repeat for each input."),
    variant: str | None = typer.Option(None, help="Tenant variant to run, e.g. 'nova'."),
    base_url: str = typer.Option("http://127.0.0.1:8010", help="Where the target app is served."),
    attended: bool = typer.Option(False, help="Pause for an operator instead of stopping on NEEDS_HUMAN."),
    headed: bool | None = typer.Option(None, "--headed/--no-headed",
                                       help="Show the browser window (default: shown when --attended)."),
    channel: str | None = typer.Option(None, help="Use an installed browser, e.g. 'msedge' or 'chrome'."),
    operator_port: int = typer.Option(8020, help="Operator console port (attended mode)."),
    handoff_timeout: float = typer.Option(300, help="Seconds to wait for an operator to respond."),
    runs_dir: Path = typer.Option(ROOT / "runs", help="Where evidence is written."),
    allow_draft: bool = typer.Option(False, help="Run a draft capability (review/testing only)."),
) -> None:
    """Replay a capability deterministically (no LLM). Prints the structured result."""
    load_dotenv(ROOT / ".env")
    cap = load_capability(capability)
    if cap.meta.status != "approved" and not allow_draft:
        typer.echo(f"'{cap.meta.id}' is a {cap.meta.status}: review and approve it before production use, "
                   "or pass --allow-draft to test it.", err=True)
        raise typer.Exit(EXIT["FAILED"])
    result = asyncio.run(_replay(cap, parse_inputs(inputs), variant, base_url, attended,
                                 attended if headed is None else headed,
                                 channel, operator_port, handoff_timeout, runs_dir))
    sensitive = {o.name for o in cap.outputs if o.sensitive}
    typer.echo(json.dumps(result.for_log(sensitive), indent=2))
    raise typer.Exit(EXIT[result.status.value])


async def launch_browser(pw, headed: bool, channel: str | None):
    """Bundled Chromium by default. Some Windows setups block the bundled full-browser binary from running
    (headless still works), so a visible window falls back to an installed Edge/Chrome."""
    if channel:
        return await pw.chromium.launch(headless=not headed, channel=channel)
    try:
        return await pw.chromium.launch(headless=not headed)
    except Exception as first:  # noqa: BLE001
        if not headed:
            raise
        for ch in ("msedge", "chrome"):
            try:
                b = await pw.chromium.launch(headless=False, channel=ch)
                typer.echo(f"note: bundled Chromium could not open a window ({str(first).splitlines()[0]}); using {ch}", err=True)
                return b
            except Exception:  # noqa: BLE001
                continue
        raise


async def _replay(cap, inputs, variant, base_url, attended, headed, channel, operator_port, handoff_timeout, runs_dir):
    from playwright.async_api import async_playwright

    from cua.handoff.controller import SessionController
    from cua.handoff.intervention import InterventionStore
    from cua.handoff.operator import start_operator_server
    from cua.policy.engine import Policy
    from cua.replay.engine import Replayer
    from cua.surface.web import WebSurface

    policy = Policy.from_yaml()
    async with async_playwright() as pw:
        browser = await launch_browser(pw, headed, channel)
        surface = await WebSurface.open(browser, base_url, policy)
        ctrl = server = task = None
        if attended:
            ctrl = SessionController(InterventionStore(runs_dir), policy.redactor)
            await ctrl.attach(surface)
            server, task = await start_operator_server(ctrl, operator_port)
            typer.echo(f"operator console: http://127.0.0.1:{operator_port}", err=True)
        try:
            return await Replayer(surface, policy, runs_dir=runs_dir, controller=ctrl,
                                  handoff_timeout_s=handoff_timeout).run(cap, inputs, variant)
        finally:
            if server:
                server.should_exit = True
                await task
            await browser.close()


@app.command()
def serve(port: int = typer.Option(8030), base_url: str = typer.Option("http://127.0.0.1:8010"),
          runs_dir: Path = typer.Option(ROOT / "runs")) -> None:
    """Mission Control: capability catalog, runs and evidence, operator inbox, and the agent-facing tool API."""
    load_dotenv(ROOT / ".env")
    import uvicorn

    from cua.console.server import build_app
    typer.echo(f"Mission Control: http://127.0.0.1:{port}   (target app: {base_url})", err=True)
    uvicorn.run(build_app(base_url=base_url, runs_dir=runs_dir), host="127.0.0.1", port=port, log_level="warning")


def parse_typed(pairs: list[str], with_value: bool) -> dict:
    """'name:type=value' (inputs) or 'name:type' (outputs); type defaults to string."""
    out = {}
    for p in pairs:
        head, _, value = p.partition("=") if with_value else (p, "", "")
        name, _, type_ = head.partition(":")
        if not name.strip() or (with_value and "=" not in p):
            raise typer.BadParameter(f"'{p}' must look like name:type{'=value' if with_value else ''}")
        type_ = (type_ or "string").strip()
        if type_ not in ("string", "int", "money", "enum", "bool"):
            raise typer.BadParameter(f"unknown type '{type_}' in '{p}'")
        out[name.strip()] = (type_, value) if with_value else type_
    return out


@app.command()
def discover(
    goal: str = typer.Argument(..., help="What to accomplish, in plain language."),
    start: str = typer.Option("/heritage/", help="Entry path of the target app."),
    inputs: list[str] = typer.Option([], "--input", "-i", help="Caller input as name:type=example, e.g. member_id=12345."),
    outputs: list[str] = typer.Option([], "--output", "-o", help="Value to read as name:type, e.g. savings_balance:money."),
    cap_id: str = typer.Option(..., "--id", help="Capability id to save, e.g. memberserv.lookup_member_discovered."),
    app_name: str = typer.Option("memberserv", "--app", help="Vendor product id shared across tenants."),
    variant: str = typer.Option("heritage", help="Tenant/variant the run is recorded on."),
    secrets: list[str] = typer.Option(["HERITAGE_USER", "HERITAGE_PASS"], "--secret", help="Env vars the agent may sign in with."),
    model: str | None = typer.Option(None, help="Gemini model (default: GEMINI_MODEL from .env)."),
    vision: bool = typer.Option(False, help="Also send a screenshot each turn (off: screens can hold customer data)."),
    base_url: str = typer.Option("http://127.0.0.1:8010"),
    attended: bool = typer.Option(False, help="On 'stuck', pause for an operator instead of stopping."),
    headed: bool = typer.Option(False),
    channel: str | None = typer.Option(None),
    operator_port: int = typer.Option(8020),
    verify: bool = typer.Option(True, help="Replay the recorded capability once (no LLM) to prove it works."),
    runs_dir: Path = typer.Option(ROOT / "runs"),
) -> None:
    """Let the LLM accomplish a goal on the live app, then save what it did as a draft capability."""
    load_dotenv(ROOT / ".env")
    settings = Settings()
    if settings.gemini_api_key is None:
        raise typer.BadParameter("GEMINI_API_KEY is not set (put it in .env)")
    from cua.agent.gemini import GeminiClient
    client = GeminiClient(settings.gemini_api_key.get_secret_value(), model or settings.gemini_model)
    ins, outs = parse_typed(inputs, True), parse_typed(outputs, False)
    result = asyncio.run(_discover(client, goal, start, ins, outs, cap_id, app_name, variant, secrets, vision,
                                   base_url, attended, headed or attended, channel, operator_port, runs_dir))
    typer.echo(f"discovery: {result.status} after {result.turns} turns ({result.reason})")
    typer.echo(f"evidence:  {result.evidence_dir}")
    for note in result.notes:
        typer.echo(f"note: {note}")
    if result.capability is None:
        raise typer.Exit(1)
    path = ROOT / "capabilities" / f"{cap_id}.json"
    path.write_text(result.capability.to_json(), encoding="utf-8")
    typer.echo(f"saved draft capability: {path}")
    if verify:
        replay_inputs = {n: v for n, (_, v) in ins.items()}
        res = asyncio.run(_replay(result.capability, replay_inputs, None, base_url, False, False, channel,
                                  operator_port, 60, runs_dir))
        typer.echo(f"verification replay (no LLM): {res.status.value}  evidence: {res.evidence_dir}")
        raise typer.Exit(0 if res.ok else 1)


async def _discover(client, goal, start, ins, outs, cap_id, app_name, variant, secrets, vision, base_url, attended,
                    headed, channel, operator_port, runs_dir):
    from playwright.async_api import async_playwright

    from cua.agent.loop import DiscoveryAgent
    from cua.handoff.controller import SessionController
    from cua.handoff.intervention import InterventionStore
    from cua.handoff.operator import start_operator_server
    from cua.policy.engine import Policy
    from cua.surface.web import WebSurface

    policy = Policy.from_yaml()
    async with async_playwright() as pw:
        browser = await launch_browser(pw, headed, channel)
        surface = await WebSurface.open(browser, base_url, policy)
        ctrl = server = task = None
        if attended:
            ctrl = SessionController(InterventionStore(runs_dir), policy.redactor)
            await ctrl.attach(surface)
            server, task = await start_operator_server(ctrl, operator_port)
            typer.echo(f"operator console: http://127.0.0.1:{operator_port}", err=True)
        try:
            agent = DiscoveryAgent(surface, client, policy, runs_dir=runs_dir, controller=ctrl, vision=vision)
            return await agent.run(goal=goal, start=start, inputs=ins, outputs=outs, cap_id=cap_id, app=app_name,
                                   variant=variant, secret_names=secrets)
        finally:
            if server:
                server.should_exit = True
                await task
            await browser.close()


if __name__ == "__main__":
    app()
