"""Print a detailed eval report from grades.json."""

import json

import typer

from evals.framework.reporting import console, print_report, resolve_results_dir

app = typer.Typer(pretty_exceptions_enable=False)


@app.command()
def main(
    results_dir: str = typer.Argument("latest", help="Results dir to print. Use 'latest' for the most recent."),
):
    """Print a report from a graded results dir (no grading, no API calls).

    Reads <results_dir>/grades.json (written by `skill-eval grade`) and renders
    a per-task / per-runner / per-variant panel plus a summary table. FAIL
    panels include grader-supplied remediation suggestions (cause → fix) when
    present. For a deeper view of one run, use `skill-eval show`.

    Examples:
      skill-eval report                                        # latest run
      skill-eval report results/2026-04-22T11-31-47__baseline  # specific run
    """
    results_path = resolve_results_dir(results_dir)
    grades_file = results_path / "grades.json"
    if not grades_file.exists():
        console.print(f"[red]No grades.json in {results_path}. Run grader first.[/red]")
        raise typer.Exit(1)

    with open(grades_file) as f:
        grades = json.load(f)

    console.print(f"\n[bold]EVAL REPORT:[/bold] {results_path.name}\n")

    notes = None
    notes_file = results_path / "notes.md"
    if notes_file.exists():
        notes = notes_file.read_text()

    print_report(grades, notes)


if __name__ == "__main__":
    app()
