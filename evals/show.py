"""`skill-eval show` — readable, pipe-friendly inspector for one run.

Walks a results dir, filters by runner/task/variant, and prints conversation +
metadata + grader output for a single run. Used both by humans (instead of jq +
less incantations) and by coding agents inspecting their own behavior.
"""

import json
import re
from pathlib import Path

import typer
from rich.syntax import Syntax
from rich.text import Text

from evals.framework.reporting import console, resolve_results_dir

app = typer.Typer(pretty_exceptions_enable=False)

_MAJOR_RE = re.compile(r"^\*\*(User|Assistant|Final result):\*\*\s?(.*)$")
_MINOR_RE = re.compile(r"^\*\*([A-Za-z_][A-Za-z0-9_ ]*):\*\*\s?(.*)$")
_MAJOR_COLOR = {"User": "cyan", "Assistant": "green", "Final result": "yellow"}

# Minor (tool-call) events the user wants to spot at a glance — same arrow,
# but bright color and bold instead of dim.
_HIGHLIGHTED_MINOR = {
    "Skill invoked": "magenta",
    "Skill activated": "magenta",
    "Skill": "magenta",
    "write_file": "blue",
    "Write": "blue",
}

_INLINE_MD_RE = re.compile(r"\*\*([^*]+)\*\*|`([^`]+)`")


def _styled(content: str, indent: str = "  ") -> Text:
    """Build a rich.Text from a content line, honoring inline **bold** and `code`.

    Anything outside those markers is preserved verbatim — runner transcripts
    contain stray characters (brackets, JSON) that would otherwise fight rich's
    markup parser. Returning a Text object keeps everything literal.
    """
    t = Text(indent)
    pos = 0
    for m in _INLINE_MD_RE.finditer(content):
        if m.start() > pos:
            t.append(content[pos : m.start()])
        if m.group(1) is not None:
            t.append(m.group(1), style="bold")
        else:
            t.append(m.group(2), style="cyan")
        pos = m.end()
    if pos < len(content):
        t.append(content[pos:])
    return t


def _arrow() -> str:
    """Unicode arrow when stdout is a TTY, ASCII fallback otherwise."""
    return "→" if console.is_terminal else "->"


def _rule_chars() -> str:
    return "─" if console.is_terminal else "-"


def render_conversation(md: str) -> None:
    """Pretty-print a runner's conversation_md transcript.

    Runners produce a flat markdown stream: ``**Role:** content`` per turn,
    occasional fenced code blocks for tool output. We layer on rules per major
    role (User / Assistant / Final result), dim arrows for tool-call lines, and
    syntax-highlighted code blocks. Stays pipe-friendly — rich strips styling
    automatically when stdout isn't a TTY.
    """
    lines = md.splitlines()
    code_lang: str | None = None
    code_buf: list[str] = []

    def flush_code() -> None:
        nonlocal code_lang, code_buf
        if code_buf:
            console.print(
                Syntax(
                    "\n".join(code_buf),
                    code_lang or "text",
                    background_color="default",
                    word_wrap=True,
                    padding=(0, 2),
                )
            )
        code_lang = None
        code_buf = []

    for line in lines:
        if line.startswith("```"):
            if code_lang is None:
                code_lang = line[3:].strip() or "text"
                code_buf = []
            else:
                flush_code()
            continue
        if code_lang is not None:
            code_buf.append(line)
            continue

        if line.startswith("# "):
            continue  # variant title — already in our header

        m = _MAJOR_RE.match(line)
        if m:
            role, content = m.group(1), m.group(2)
            color = _MAJOR_COLOR[role]
            console.print()
            console.rule(
                f"[bold {color}]{role}[/bold {color}]",
                align="left",
                style=color,
                characters=_rule_chars(),
            )
            if content.strip():
                console.print(_styled(content))
            continue

        m2 = _MINOR_RE.match(line)
        if m2:
            role, content = m2.group(1), m2.group(2)
            highlight = _HIGHLIGHTED_MINOR.get(role)
            arrow_style = f"bold {highlight}" if highlight else "bold dim"
            sep_style = highlight or "dim"
            t = Text("  ")
            t.append(f"{_arrow()} {role}", style=arrow_style)
            if content:
                t.append(": ", style=sep_style)
                # Inline markup on tool-call args too — cmds + paths read better.
                t.append_text(_styled(content, indent=""))
            console.print(t)
            continue

        if line.strip():
            console.print(_styled(line))
        else:
            console.print()

    flush_code()


def _parse_base(stem: str) -> tuple[str, str, int]:
    """Split a result base filename into (task_id, variant, run_num).

    Layout: ``{task}__{variant}`` or ``{task}__{variant}__{N}`` when repeat>1.
    Handles task ids that themselves contain ``__`` (rare but legal).
    """
    parts = stem.split("__")
    if len(parts) >= 3 and parts[-1].isdigit():
        return "__".join(parts[:-2]), parts[-2], int(parts[-1])
    if len(parts) >= 2:
        return "__".join(parts[:-1]), parts[-1], 1
    return stem, "", 1


def discover_runs(results_path: Path) -> list[dict]:
    """List every per-run artifact in a results dir.

    Skips ``tasks.json`` / ``grades.json`` (run-level files, not per-runner).
    Each entry carries the paths needed to render the run; missing optional
    files (md, jsonl, workdir) are still listed but won't be shown if absent.
    """
    runs: list[dict] = []
    for runner_dir in sorted(p for p in results_path.iterdir() if p.is_dir()):
        for json_path in sorted(runner_dir.glob("*.json")):
            if json_path.name in ("tasks.json", "grades.json"):
                continue
            task_id, variant, run_num = _parse_base(json_path.stem)
            runs.append(
                {
                    "runner": runner_dir.name,
                    "task_id": task_id,
                    "variant": variant,
                    "run_num": run_num,
                    "json_path": json_path,
                    "md_path": json_path.with_suffix(".md"),
                    "jsonl_path": json_path.with_suffix(".jsonl"),
                    "workdir_path": json_path.parent / f"{json_path.stem}.workdir",
                }
            )
    return runs


def filter_runs(
    runs: list[dict],
    runner: str | None = None,
    task: str | None = None,
    variant: str | None = None,
    run_num: int | None = None,
) -> list[dict]:
    """Apply selector filters to a discovered-runs list."""
    out = runs
    if runner:
        out = [r for r in out if r["runner"] == runner]
    if task:
        out = [r for r in out if r["task_id"] == task]
    if variant:
        out = [r for r in out if r["variant"] == variant]
    if run_num is not None:
        out = [r for r in out if r["run_num"] == run_num]
    return out


def _find_grade(grades: list[dict], run: dict) -> dict | None:
    for g in grades:
        if (
            g.get("runner") == run["runner"]
            and g.get("task_id") == run["task_id"]
            and g.get("variant") == run["variant"]
            and g.get("run_num", 1) == run["run_num"]
        ):
            return g
    return None


def _print_listing(runs: list[dict]) -> None:
    """Brief listing shown when the selector matches multiple runs."""
    console.print(f"[bold]{len(runs)} matching runs:[/bold]")
    for r in runs:
        suffix = f"  #{r['run_num']}" if r["run_num"] > 1 else ""
        console.print(f"  [bold]{r['task_id']}[/bold]  {r['runner']}/{r['variant']}{suffix}")
    console.print(
        "\n[dim]Narrow with [/dim][bold]-r <runner>[/bold] · "
        "[bold]-t <task>[/bold] · [bold]-v <variant>[/bold]"
        "[dim] (or pass --all to render every match)[/dim]"
    )


def _stats_line(result: dict) -> str:
    bits = []
    if result.get("duration_ms"):
        bits.append(f"{result['duration_ms'] / 1000:.1f}s")
    if result.get("num_turns"):
        bits.append(f"{result['num_turns']} turns")
    usage = result.get("usage") or {}
    if usage:
        in_tok = (
            (usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
        )
        out_tok = usage.get("output_tokens") or 0
        if in_tok or out_tok:
            bits.append(f"{in_tok:,}in / {out_tok:,}out tokens")
    if result.get("total_cost_usd"):
        bits.append(f"${result['total_cost_usd']:.4f}")
    return " · ".join(bits)


def _print_grader(grade: dict) -> None:
    verdict = "[bold green]PASS[/bold green]" if grade.get("pass") else "[bold red]FAIL[/bold red]"
    console.print(f"## Grader  ·  {verdict}")
    if grade.get("proposed_command"):
        console.print(f"[bold]Command:[/bold]   {grade['proposed_command']}")
    if grade.get("evidence"):
        ev = grade["evidence"]
        if len(ev) > 600:
            ev = ev[:600] + "…"
        console.print(f"[bold]Evidence:[/bold]  {ev}")
    if grade.get("reasoning"):
        console.print(f"[bold]Reasoning:[/bold] {grade['reasoning']}")
    suggestions = grade.get("suggestions") or []
    if suggestions:
        console.print("[bold]Suggestions:[/bold]")
        for s in suggestions:
            cause = (s.get("cause") or "").strip() if isinstance(s, dict) else ""
            fix = (s.get("fix") or "").strip() if isinstance(s, dict) else str(s).strip()
            if cause and fix:
                console.print(f"  • [yellow]{cause}[/yellow] → {fix}")
            elif fix:
                console.print(f"  • {fix}")
            elif cause:
                console.print(f"  • [yellow]{cause}[/yellow]")
    console.print()


def _print_workdir(workdir: Path) -> None:
    if not workdir.is_dir():
        return
    files = []
    for p in sorted(workdir.rglob("*")):
        if p.is_file():
            files.append(p)
    if not files:
        return
    console.print(f"## Workdir  ·  [dim]{workdir}[/dim]")
    for p in files:
        rel = p.relative_to(workdir)
        size = p.stat().st_size
        console.print(f"  {rel}  [dim]({size:,} bytes)[/dim]")
    console.print()


def _render(run: dict, grade: dict | None, mode: str) -> None:
    """Render one run in the requested mode."""
    suffix = f"  #{run['run_num']}" if run["run_num"] > 1 else ""
    console.rule(f"[bold]{run['task_id']}[/bold]  ·  {run['runner']}/{run['variant']}{suffix}")

    result = {}
    if run["json_path"].exists():
        try:
            result = json.loads(run["json_path"].read_text())
        except json.JSONDecodeError:
            result = {}

    if mode == "events":
        if not run["jsonl_path"].exists():
            console.print("[yellow]No raw events recorded for this run.[/yellow]")
            return
        console.print(run["jsonl_path"].read_text(), markup=False, highlight=False)
        return

    if mode == "grader":
        if grade is None:
            console.print("[yellow]No grader entry found. Run `skill-eval grade <run>` first.[/yellow]")
            return
        _print_grader(grade)
        return

    if mode == "workdir":
        _print_workdir(run["workdir_path"])
        if not run["workdir_path"].is_dir():
            console.print("[yellow]No captured workdir for this run.[/yellow]")
        return

    if mode == "conversation":
        if run["md_path"].exists():
            render_conversation(run["md_path"].read_text())
        else:
            console.print("[yellow]No conversation transcript saved.[/yellow]")
        return

    # Default: full render — stats, conversation, workdir, grader, hint.
    stats = _stats_line(result)
    if stats:
        console.print(f"[dim]{stats}[/dim]")
    if result.get("error"):
        console.print(f"[red]Runner error:[/red] {result['error']}")
        if result.get("stderr"):
            console.print(f"[dim]{result['stderr'][:500]}[/dim]")
    if result.get("session_id"):
        console.print(f"[dim]Resume: claude --resume {result['session_id']}[/dim]")
    console.print()

    console.print("[bold]## Conversation[/bold]")
    if run["md_path"].exists():
        render_conversation(run["md_path"].read_text())
    else:
        console.print("[yellow](no transcript)[/yellow]")
    console.print()

    _print_workdir(run["workdir_path"])

    if grade is not None:
        _print_grader(grade)
    else:
        console.print("[dim]## Grader[/dim]\n[dim](not graded yet — run `skill-eval grade <run>`)[/dim]\n")

    label = "pass" if grade and grade.get("pass") else "fail"
    selector = f"-r {run['runner']} -t {run['task_id']} -v {run['variant']}"
    console.print(
        f"[dim]Promote this run to a calibration example:[/dim]\n"
        f'  skill-eval examples add {selector} --label {label} --reason "..."'
    )


@app.command()
def main(
    run: str = typer.Argument("latest", help="Results dir. Use 'latest' for the most recent."),
    runner: str = typer.Option("", "--runner", "-r", help="Filter to one runner: claude, gemini, copilot."),
    task: str = typer.Option("", "--task", "-t", help="Filter to one task id."),
    variant: str = typer.Option(
        "", "--variant", "-v", help="Filter to one variant: with-skill, without-skill, or with-docs."
    ),
    run_num: int = typer.Option(0, "--num", "-n", help="When repeat>1, pick a specific run number."),
    show_all: bool = typer.Option(False, "--all", help="Render every matching run instead of just listing them."),
    conversation_only: bool = typer.Option(False, "--conversation-only", help="Print only the .md transcript."),
    events: bool = typer.Option(False, "--events", help="Print only the raw .jsonl event stream."),
    grader: bool = typer.Option(False, "--grader", help="Print only the grader verdict + reasoning + suggestions."),
    workdir: bool = typer.Option(False, "--workdir", help="Print only the captured workdir file listing."),
):
    """Print a readable view of a run: conversation, metadata, grader output.

    Selectors compose: pass any of -r/-t/-v to narrow. With no filters and a
    multi-task run, prints a brief listing instead of a wall of conversations.

    Examples:
      skill-eval show latest -r claude -t hello-py     # one run, full view
      skill-eval show latest -t hello-py --grader      # just the grader notes
      skill-eval show latest -r claude --events        # raw .jsonl stream
      skill-eval show latest -t hello-py --workdir     # workdir file listing
    """
    modes = [conversation_only, events, grader, workdir]
    if sum(bool(m) for m in modes) > 1:
        console.print("[red]Pick at most one of --conversation-only / --events / --grader / --workdir.[/red]")
        raise typer.Exit(2)
    mode = (
        "conversation"
        if conversation_only
        else "events"
        if events
        else "grader"
        if grader
        else "workdir"
        if workdir
        else "default"
    )

    results_path = resolve_results_dir(run)
    runs = discover_runs(results_path)
    if not runs:
        console.print(f"[red]No runs found in {results_path}.[/red]")
        raise typer.Exit(1)

    matches = filter_runs(
        runs,
        runner=runner or None,
        task=task or None,
        variant=variant or None,
        run_num=run_num if run_num else None,
    )
    if not matches:
        console.print("[red]No runs match the selector.[/red]")
        _print_listing(runs)
        raise typer.Exit(1)

    grades_path = results_path / "grades.json"
    grades = json.loads(grades_path.read_text()) if grades_path.exists() else []

    if len(matches) > 1 and not show_all:
        _print_listing(matches)
        return

    for r in matches:
        _render(r, _find_grade(grades, r), mode)


if __name__ == "__main__":
    app()
