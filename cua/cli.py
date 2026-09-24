import typer

from cua.config import Settings

app = typer.Typer(help="Computer-use automation: discover, replay, escalate.")


@app.command()
def doctor() -> None:
    """Check the environment."""
    s = Settings()
    typer.echo(f"gemini key: {'set' if s.gemini_api_key else 'MISSING (needed only for discover)'}")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        typer.echo("browser: ok")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"browser: FAILED ({e}); run `playwright install chromium`")


for name in ("discover", "replay", "operator", "list", "mock-app"):
    app.command(name)(lambda: typer.echo("not implemented yet"))  # filled in later steps

if __name__ == "__main__":
    app()
