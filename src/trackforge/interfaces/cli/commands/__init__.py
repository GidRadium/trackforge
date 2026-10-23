"""Register all local-MVP CLI commands on the root Typer app."""

from __future__ import annotations

import typer

from trackforge.interfaces.cli.commands.add import add_command
from trackforge.interfaces.cli.commands.doctor import doctor_command
from trackforge.interfaces.cli.commands.explain import explain_command
from trackforge.interfaces.cli.commands.import_path import import_command
from trackforge.interfaces.cli.commands.plan import plan_command
from trackforge.interfaces.cli.commands.publish import publish_command
from trackforge.interfaces.cli.commands.render import render_command
from trackforge.interfaces.cli.commands.resolve import resolve_command
from trackforge.interfaces.cli.commands.resume import resume_command
from trackforge.interfaces.cli.commands.review import review_command
from trackforge.interfaces.cli.commands.status import status_command


def register_commands(app: typer.Typer) -> None:
    app.command("add")(add_command)
    app.command("import")(import_command)
    app.command("plan")(plan_command)
    app.command("resolve")(resolve_command)
    app.command("render")(render_command)
    app.command("publish")(publish_command)
    app.command("review")(review_command)
    app.command("explain")(explain_command)
    app.command("doctor")(doctor_command)
    app.command("status")(status_command)
    app.command("resume")(resume_command)


__all__ = ["register_commands"]
