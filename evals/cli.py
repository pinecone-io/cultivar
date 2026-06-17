"""Entry point for the skill-eval CLI."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer
from dotenv import load_dotenv

# Pin to cwd: find_dotenv() walks from __file__, which under `uv tool install`
# sits in the tool install dir and never reaches the user's cwd. Must run before
# the imports below so subcommand modules see env vars at import time.
load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)

from evals.framework.grader import main as grade_main  # noqa: E402
from evals.framework.report import main as report_main  # noqa: E402
from evals.hello import main as hello_main  # noqa: E402
from evals.init import main as init_main  # noqa: E402
from evals.run import main as run_main  # noqa: E402
from evals.show import main as show_main  # noqa: E402


def _version_callback(value: bool) -> None:
    if value:
        try:
            v = version("skill-eval")
        except PackageNotFoundError:
            v = "unknown (not installed as a package)"
        typer.echo(f"skill-eval {v}")
        raise typer.Exit()


app = typer.Typer(
    help=(
        "Skill eval framework — test whether agent skills improve behavior "
        "across Claude / Copilot / Gemini CLIs.\n\n"
        "Typical loop:\n"
        "  1. `hello` — verify your install and credentials with a packaged smoke task.\n"
        "  2. `init <skill>` — scaffold tasks/<skill>.yaml + .claude/skills/<skill>/SKILL.md.\n"
        "  3. Edit those files, then `run --skill <skill> --grade` to evaluate.\n"
        "  4. `report` for the summary table; `show latest -t <task>` to read a single run.\n\n"
        "See README.md and docs/ for the full picture."
    ),
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    epilog="Run 'skill-eval <command> --help' for details on a specific subcommand.",
)


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print the installed skill-eval version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Root callback exists only to host the --version flag."""


app.command("init")(init_main)
app.command("hello")(hello_main)
app.command("run")(run_main)
app.command("grade")(grade_main)
app.command("report")(report_main)
app.command("show")(show_main)


if __name__ == "__main__":
    app()
