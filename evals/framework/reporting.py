"""Shared reporting utilities for the eval framework."""

from collections import defaultdict
from pathlib import Path
from typing import TypedDict

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


class _VariantStats(TypedDict):
    passed: int
    total: int
    durations: list[float]
    costs: list[float]
    input_tokens: list[int]
    output_tokens: list[int]


def _new_variant_stats() -> _VariantStats:
    return {
        "passed": 0,
        "total": 0,
        "durations": [],
        "costs": [],
        "input_tokens": [],
        "output_tokens": [],
    }


def resolve_results_dir(results_dir: str | None) -> Path:
    results_root = Path.cwd() / "results"
    if results_dir and results_dir != "latest":
        return Path(results_dir)

    dirs = sorted(
        [d for d in results_root.iterdir() if d.is_dir() and d.name[0:4].isdigit()],
        key=lambda d: d.name,
    )
    if not dirs:
        console.print("[red]No results found.[/red]")
        raise typer.Exit(1)
    return dirs[-1]


def print_report(grades: list[dict], notes: str | None = None):
    if notes:
        console.print(Panel(notes.strip(), title="Notes", border_style="dim"))
        console.print()

    by_category = defaultdict(lambda: defaultdict(list))
    for g in grades:
        by_category[g["category"]][g["task_id"]].append(g)

    for _category, tasks in sorted(by_category.items()):
        for task_id, task_grades in sorted(tasks.items()):
            for g in sorted(task_grades, key=lambda x: (x.get("variant", ""), x.get("run_num", 1))):
                runner = g.get("runner", "?")
                variant = g.get("variant", "?")
                run_num = g.get("run_num", 1)
                passed = g.get("pass", False)
                cmd = g.get("proposed_command", "")
                evidence = g.get("evidence", "")
                reasoning = g.get("reasoning", "")

                if passed:
                    verdict = Text(" PASS ", style="bold white on green")
                else:
                    verdict = Text(" FAIL ", style="bold white on red")

                header = Text()
                header.append(f"{task_id} ", style="bold")
                header.append(f"{runner}/{variant}", style="dim")
                if run_num > 1 or any(x.get("run_num", 1) > 1 for x in task_grades):
                    header.append(f" #{run_num}", style="dim")

                # Stats line
                stats = []
                if g.get("duration_s"):
                    stats.append(f"{g['duration_s']}s")
                if g.get("num_turns"):
                    stats.append(f"{g['num_turns']} turns")
                if g.get("input_tokens") or g.get("output_tokens"):
                    stats.append(f"{g.get('input_tokens', 0):,}in/{g.get('output_tokens', 0):,}out tokens")
                if g.get("cost_usd"):
                    stats.append(f"${g['cost_usd']:.4f}")

                body_parts = []
                if stats:
                    body_parts.append(f"[dim]{' · '.join(stats)}[/dim]")
                st = g.get("sandbox_timing")
                if st:
                    body_parts.append(
                        f"[dim]Sandbox: {st['create_s']}s create · {st['setup_s']}s setup · {st['eval_s']}s eval · {st['teardown_s']}s teardown[/dim]"
                    )
                if cmd:
                    body_parts.append(f"[bold]Command:[/bold]   {cmd}")
                if evidence:
                    ev = evidence[:300] + "..." if len(evidence) > 300 else evidence
                    body_parts.append(f"[bold]Evidence:[/bold]  {ev}")
                if reasoning:
                    body_parts.append(f"[bold]Reasoning:[/bold] {reasoning}")
                suggestions = g.get("suggestions") or []
                if suggestions:
                    lines = ["[bold]Suggestions:[/bold]"]
                    for s in suggestions:
                        cause = (s.get("cause") or "").strip() if isinstance(s, dict) else ""
                        fix = (s.get("fix") or "").strip() if isinstance(s, dict) else str(s).strip()
                        if cause and fix:
                            lines.append(f"  • [yellow]{cause}[/yellow] → {fix}")
                        elif fix:
                            lines.append(f"  • {fix}")
                        elif cause:
                            lines.append(f"  • [yellow]{cause}[/yellow]")
                    body_parts.append("\n".join(lines))
                session_id = g.get("session_id", "")
                if session_id:
                    body_parts.append(f"[dim]Resume:[/dim]     [dim]claude --resume {session_id}[/dim]")

                body = "\n".join(body_parts) if body_parts else "(no details)"

                color = "green" if passed else "red"
                console.print(
                    Panel(
                        body,
                        title=header,
                        subtitle=verdict,
                        border_style=color,
                        padding=(0, 1),
                    )
                )
                console.print()

    # Summary table
    variant_stats: defaultdict[str, _VariantStats] = defaultdict(_new_variant_stats)

    for g in grades:
        variant = g.get("variant", "?")
        s = variant_stats[variant]
        s["total"] += 1
        if g.get("pass", False):
            s["passed"] += 1
        if g.get("duration_s"):
            s["durations"].append(g["duration_s"])
        if g.get("cost_usd"):
            s["costs"].append(g["cost_usd"])
        if g.get("input_tokens"):
            s["input_tokens"].append(g["input_tokens"])
        if g.get("output_tokens"):
            s["output_tokens"].append(g["output_tokens"])

    all_variants = sorted(variant_stats.keys())
    has_repeats = any(s["total"] > 1 for s in variant_stats.values())

    def avg_fmt(values, fmt_fn):
        if not values:
            return "[dim]—[/dim]"
        avg = sum(values) / len(values)
        if has_repeats and len(values) > 1:
            return f"{fmt_fn(avg)} [dim](n={len(values)})[/dim]"
        return fmt_fn(avg)

    table = Table(title="Summary", show_lines=True)
    table.add_column("", style="bold")
    for v in all_variants:
        table.add_column(v, justify="center")

    # Pass rate row
    row = ["Pass Rate"]
    for v in all_variants:
        s = variant_stats[v]
        pct = (s["passed"] / s["total"] * 100) if s["total"] else 0
        style = "green" if s["passed"] == s["total"] else "red"
        if has_repeats:
            row.append(f"[{style}][bold]{s['passed']}/{s['total']} ({pct:.0f}%)[/bold][/{style}]")
        else:
            row.append(f"[{style}][bold]{s['passed']}/{s['total']}[/bold][/{style}]")
    table.add_row(*row)

    # Duration row
    row = ["Avg Duration"]
    for v in all_variants:
        s = variant_stats[v]
        row.append(avg_fmt(s["durations"], lambda x: f"{x:.1f}s"))
    table.add_row(*row)

    # Tokens row
    row = ["Avg Tokens"]
    for v in all_variants:
        s = variant_stats[v]
        if s["input_tokens"] or s["output_tokens"]:
            avg_in = sum(s["input_tokens"]) / len(s["input_tokens"]) if s["input_tokens"] else 0
            avg_out = sum(s["output_tokens"]) / len(s["output_tokens"]) if s["output_tokens"] else 0
            cell = f"{avg_in:,.0f}in / {avg_out:,.0f}out"
            if has_repeats and len(s["input_tokens"]) > 1:
                cell += f" [dim](n={len(s['input_tokens'])})[/dim]"
            row.append(cell)
        else:
            row.append("[dim]—[/dim]")
    table.add_row(*row)

    # Cost row
    row = ["Avg Cost"]
    for v in all_variants:
        s = variant_stats[v]
        row.append(avg_fmt(s["costs"], lambda x: f"${x:.4f}"))
    table.add_row(*row)

    console.print(table)
    console.print()
