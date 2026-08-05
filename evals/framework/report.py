"""Print a detailed eval report from grades.json."""

import json

import typer

from evals.framework.reporting import console, gate_pass_rate, print_report, resolve_results_dir

app = typer.Typer(pretty_exceptions_enable=False)

VARIANT_ORDER = ["with-skill", "without-skill", "with-docs"]


def _summarize(grades: list[dict]) -> dict:
    """Machine-readable summary of a graded run.

    Shape is deliberately flat and stable: CI writes this to a PR comment or keeps it
    as a trend record, so fields should only ever be added, never renamed.
    """
    variants = {}
    seen = [v for v in VARIANT_ORDER if any(g.get("variant") == v for g in grades)]
    seen += sorted({g.get("variant", "") for g in grades} - set(VARIANT_ORDER) - {""})
    for v in seen:
        rate, passed, total = gate_pass_rate(grades, v)
        variants[v] = {"pass_rate": round(rate, 1), "passed": passed, "total": total}

    return {
        "total_conversations": len(grades),
        "variants": variants,
        "cost_usd": round(sum(g.get("cost_usd") or 0 for g in grades), 4),
        "duration_s": round(sum(g.get("duration_s") or 0 for g in grades), 1),
        "failures": [
            {
                "task_id": g.get("task_id", ""),
                "runner": g.get("runner", ""),
                "variant": g.get("variant", ""),
                "run_num": g.get("run_num", 1),
                "evidence": g.get("evidence", ""),
            }
            for g in grades
            if not g.get("pass")
        ],
    }


def _render_md(grades: list[dict], run_name: str) -> str:
    """GitHub-flavoured markdown, sized for a PR comment."""
    s = _summarize(grades)
    lines = [f"### Eval report — `{run_name}`", ""]
    lines += ["| Variant | Pass rate | Passed | Total |", "|---|---:|---:|---:|"]
    for v, st in s["variants"].items():
        lines.append(f"| `{v}` | {st['pass_rate']:.1f}% | {st['passed']} | {st['total']} |")
    lines += [
        "",
        f"{s['total_conversations']} conversation(s) · "
        f"${s['cost_usd']:.4f} · {s['duration_s']:.1f}s",
    ]

    if s["failures"]:
        lines += ["", f"<details><summary>{len(s['failures'])} failure(s)</summary>", ""]
        for f in s["failures"]:
            loc = f"`{f['task_id']}` — {f['runner']}/{f['variant']}"
            if f["run_num"] != 1:
                loc += f" (run {f['run_num']})"
            evidence = " ".join((f["evidence"] or "").split())
            if len(evidence) > 300:
                evidence = evidence[:297] + "..."
            lines.append(f"- {loc}{': ' + evidence if evidence else ''}")
        lines += ["", "</details>"]
    else:
        lines += ["", "No failures."]

    return "\n".join(lines) + "\n"


@app.command()
def main(
    results_dir: str = typer.Argument("latest", help="Results dir to print. Use 'latest' for the most recent."),
    format: str = typer.Option(
        "rich",
        "--format",
        help="Output format: rich (default, terminal panels), md (PR comment), or json (machine-readable).",
    ),
):
    """Print a report from a graded results dir (no grading, no API calls).

    Reads <results_dir>/grades.json (written by `cultivar grade`) and renders
    a per-task / per-runner / per-variant panel plus a summary table. FAIL
    panels include grader-supplied remediation suggestions (cause → fix) when
    present. For a deeper view of one run, use `cultivar show`.

    `--format md` and `--format json` write plain output to stdout with no Rich
    markup, so they can be redirected into a PR comment or a trend record.

    Examples:
      cultivar report                                        # latest run
      cultivar report results/2026-04-22T11-31-47__baseline  # specific run
      cultivar report --format md >> "$GITHUB_STEP_SUMMARY"  # CI job summary
      cultivar report --format json | jq .variants           # machine-readable
    """
    fmt = format.lower()
    if fmt not in ("rich", "md", "json"):
        console.print(f"[red]Unknown --format '{format}'. Use rich, md, or json.[/red]")
        raise typer.Exit(2)

    results_path = resolve_results_dir(results_dir)
    grades_file = results_path / "grades.json"
    if not grades_file.exists():
        console.print(f"[red]No grades.json in {results_path}. Run grader first.[/red]")
        raise typer.Exit(1)

    with open(grades_file) as f:
        grades = json.load(f)

    # Machine-readable formats bypass Rich entirely — console applies markup and
    # wraps to terminal width, both of which corrupt piped output.
    if fmt == "json":
        payload = {"run": results_path.name, **_summarize(grades)}
        print(json.dumps(payload, indent=2))
        return
    if fmt == "md":
        print(_render_md(grades, results_path.name), end="")
        return

    console.print(f"\n[bold]EVAL REPORT:[/bold] {results_path.name}\n")

    notes = None
    notes_file = results_path / "notes.md"
    if notes_file.exists():
        notes = notes_file.read_text()

    print_report(grades, notes)


if __name__ == "__main__":
    app()
