"""`cultivar hello` — end-to-end smoke test using a packaged task + skill.

Exists so a fresh `uv tool install` can be verified in one command without
cloning this repo: the smoke YAML and SKILL.md ship inside the wheel.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from importlib.resources import as_file, files
from pathlib import Path

import typer
import yaml
from rich.table import Table

from evals.framework.reporting import console
from evals.run import RUNNER_CLASSES, run_local, run_remote

app = typer.Typer(pretty_exceptions_enable=False)

SMOKE_PACKAGE = "evals._resources"
SMOKE_TASK_FILE = "workdir-smoke.yaml"
SMOKE_SKILL_NAME = "workdir-smoke"

_RUNNER_INSTALL_HINTS = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "gemini": "npm install -g @google/gemini-cli",
    "copilot": "npm install -g @github/copilot  (needs a fine-grained PAT with 'Copilot Requests')",
}


def _materialize_smoke(dest: Path) -> tuple[list[dict], Path]:
    """Copy packaged smoke task + skill into dest. Return (tasks, skill_dir)."""
    skill_dir = dest / ".claude" / "skills" / SMOKE_SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)

    pkg = files(SMOKE_PACKAGE) / "smoke"
    with as_file(pkg / SMOKE_TASK_FILE) as task_src:
        tasks_data = yaml.safe_load(Path(task_src).read_text())
    with as_file(pkg / "skill" / "SKILL.md") as skill_src:
        shutil.copy(Path(skill_src), skill_dir / "SKILL.md")

    return tasks_data["tasks"], skill_dir


def _preflight(runner: str, grade: bool, remote: bool) -> list[dict]:
    """Aggregate setup checks. Each row is {name, ok, detail, hint}.

    Pure: only reads filesystem (shutil.which, ~/.modal.toml) and os.environ —
    no network, no subprocess. Lets `cultivar hello` give a doctor-style
    summary before the smoke runs.
    """
    rows: list[dict] = []

    binary = shutil.which(runner)
    rows.append(
        {
            "name": f"{runner} CLI on PATH",
            "ok": bool(binary),
            "detail": binary or "not found",
            "hint": _RUNNER_INSTALL_HINTS.get(runner, f"install the {runner} CLI") if not binary else "",
        }
    )

    if grade:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        rows.append(
            {
                "name": "ANTHROPIC_API_KEY (grader)",
                "ok": has_key,
                "detail": "set" if has_key else "missing",
                "hint": "" if has_key else "Add to .env in cwd or export in shell — or pass --no-grade.",
            }
        )

    if remote:
        try:
            import modal  # noqa: F401

            modal_importable = True
        except ImportError:
            modal_importable = False
        rows.append(
            {
                "name": "modal SDK importable",
                "ok": modal_importable,
                "detail": "ok" if modal_importable else "ImportError",
                "hint": "" if modal_importable else "pip install modal  (it's a pinned dep — reinstall the tool)",
            }
        )

        modal_auth = (Path.home() / ".modal.toml").exists() or bool(os.environ.get("MODAL_TOKEN_ID"))
        rows.append(
            {
                "name": "Modal auth",
                "ok": modal_auth,
                "detail": "~/.modal.toml or MODAL_TOKEN_ID present" if modal_auth else "not configured",
                "hint": "" if modal_auth else "modal token new",
            }
        )
        rows.append(
            {
                "name": "Modal sandbox secret",
                "ok": True,
                "detail": "verified at sandbox creation (no offline check)",
                "hint": "If the run fails: modal secret create eval-sandbox-secrets ANTHROPIC_API_KEY=sk-ant-...",
            }
        )

    return rows


def _print_preflight(rows: list[dict]) -> None:
    table = Table(title="Preflight", show_header=False, padding=(0, 1))
    table.add_column("", width=2)
    table.add_column("Check", style="bold")
    table.add_column("Detail", style="dim", overflow="fold")
    for r in rows:
        mark = "[green]✓[/green]" if r["ok"] else "[red]✗[/red]"
        table.add_row(mark, r["name"], r["detail"])
    console.print(table)


@app.command()
def main(
    runner: str = typer.Option("claude", "--runner", "-r", help="Agent CLI to smoke-test: claude, copilot, or gemini."),
    remote: bool = typer.Option(False, "--remote", help="Run the smoke in a Modal sandbox instead of locally."),
    grade: bool = typer.Option(
        True, "--grade/--no-grade", help="Grade the smoke after it runs. Requires ANTHROPIC_API_KEY."
    ),
    variant: str = typer.Option(
        "with-skill",
        "--variant",
        "-v",
        help=(
            "Limit to one variant of the smoke (with-skill / without-skill). "
            "Default: with-skill (faster). Pass empty string to run both. "
            "The packaged smoke task has no context_refs, so with-docs is never run here."
        ),
    ),
    max_turns: int = typer.Option(6, "--max-turns", help="Max agentic turns for the smoke."),
):
    """End-to-end smoke test of a fresh cultivar install (no clone required).

    Runs a tiny packaged task ("write hello.py") against the chosen runner,
    then grades the result. Verifies that runner CLI auth, workdir capture,
    and (with --remote) Modal sandboxing + the eval-sandbox-secrets secret
    are all wired up. Exits 0 on PASS, non-zero on FAIL.

    Examples:
      cultivar hello                              # claude, local, with-skill, graded
      cultivar hello --runner claude --remote     # confirm Modal + secret are set up
      cultivar hello --no-grade                   # skip grading (no ANTHROPIC_API_KEY)
      cultivar hello -v ""                        # both smoke variants: with-skill + without-skill (slower)
    """
    runner_cls = RUNNER_CLASSES.get(runner)
    if not runner_cls:
        console.print(f"[red]Unknown runner: {runner}.[/red] Available: {list(RUNNER_CLASSES.keys())}")
        raise typer.Exit(2)

    rows = _preflight(runner, grade, remote)
    _print_preflight(rows)
    blocking = [r for r in rows if not r["ok"]]
    if blocking:
        console.print("\n[bold red]Preflight failed.[/bold red] Resolve the items above and rerun:")
        for r in blocking:
            console.print(f"  • [bold]{r['name']}[/bold] — {r['hint'] or r['detail']}")
        raise typer.Exit(2)

    smoke_root = Path(tempfile.mkdtemp(prefix="cultivar-hello-"))
    try:
        tasks, skill_dir = _materialize_smoke(smoke_root)

        valid_variants = runner_cls(skill_dir=str(skill_dir)).variants()
        if variant:
            if variant not in valid_variants:
                console.print(f"[red]Unknown variant '{variant}'.[/red] Available: {valid_variants}")
                raise typer.Exit(2)
            variants = [variant]
        else:
            variants = valid_variants

        run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "__hello"
        run_dir = Path.cwd() / "results" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        with open(run_dir / "tasks.json", "w") as f:
            json.dump({"skill": SMOKE_SKILL_NAME, "tasks": tasks}, f, indent=2)

        console.print(f"[bold]cultivar hello[/bold]  runner={runner}  variant={','.join(variants)}  remote={remote}")
        console.print(f"  results: {run_dir}")

        if remote:
            REMOTE_RUNNERS = {"claude", "gemini", "copilot"}
            if runner not in REMOTE_RUNNERS:
                console.print(f"[yellow]'{runner}' has no remote support; running locally.[/yellow]")
                run_local(tasks, runner_cls, variants, str(skill_dir), max_turns, 1, run_dir, timeout=600)
            else:
                run_remote(
                    tasks, runner, variants, str(skill_dir), max_turns, 1, run_dir, parallel=len(variants), timeout=600
                )
        else:
            run_local(tasks, runner_cls, variants, str(skill_dir), max_turns, 1, run_dir, timeout=600)

        if not grade:
            console.print("\n[green]Smoke run complete.[/green] (skipped grading)")
            console.print(f"Inspect: cultivar report {run_dir}")
            return

        console.print("\nGrading...")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "evals.cli",
                "grade",
                str(run_dir),
                "--skill",
                SMOKE_SKILL_NAME,
                "--skills-dir",
                str(skill_dir.parent),
                "--report",
            ],
        )
        if proc.returncode != 0:
            console.print("[red]Grader exited non-zero.[/red]")
            raise typer.Exit(proc.returncode)

        grades_path = run_dir / "grades.json"
        if not grades_path.exists():
            console.print("[red]Grader did not produce grades.json.[/red]")
            raise typer.Exit(1)

        grades = json.loads(grades_path.read_text())
        all_pass = bool(grades) and all(g.get("pass") for g in grades)
        if all_pass:
            mode = "remote (Modal sandbox)" if remote else "local"
            console.print(
                f"\n[bold green]✓ all good[/bold green]  {runner} · {mode} · {len(grades)}/{len(grades)} graded PASS"
            )
            console.print(
                "\n[bold]Next:[/bold] scaffold a skill of your own —\n"
                "  [bold]cultivar init <your-skill-name>[/bold]\n"
                "Then edit [dim]tasks/<your-skill-name>.yaml[/dim] and run "
                "[bold]cultivar run -s <your-skill-name> -r claude --grade[/bold]."
            )
            return
        failed = [g for g in grades if not g.get("pass")]
        console.print(f"\n[bold red]hello: FAIL[/bold red]  ({len(failed)}/{len(grades)} graded)")
        for g in failed:
            console.print(
                f"  [red]FAIL[/red] {g.get('task_id')} / {g.get('runner')}/{g.get('variant')} — "
                f"{g.get('reasoning', '')[:200]}"
            )
        raise typer.Exit(1)

    finally:
        shutil.rmtree(smoke_root, ignore_errors=True)


if __name__ == "__main__":
    app()
