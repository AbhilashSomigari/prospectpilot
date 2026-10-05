"""Typer CLI: `prospectpilot ...`."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(help="ProspectPilot — a self-improving SDR agent", no_args_is_help=True)
db_app = typer.Typer(help="Database management")
app.add_typer(db_app, name="db")
console = Console()


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    from prospectpilot.config import REPO_ROOT

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    console.print("[green]database at head[/green]")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI server."""
    import uvicorn

    uvicorn.run("prospectpilot.api.app:app", host=host, port=port)


@app.command()
def worker() -> None:
    """Run the background worker (queued runs, due follow-ups)."""
    import asyncio

    from prospectpilot.worker import run_worker

    asyncio.run(run_worker())


if __name__ == "__main__":
    app()
