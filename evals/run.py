"""Run eval tasks against runners and save conversations."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import typer
import yaml

from evals.framework.reporting import resolve_results_base, resolve_skills_base
from evals.runners.claude import ClaudeRunner
from evals.runners.copilot import CopilotRunner
from evals.runners.gemini import GeminiRunner

TASKS_DIR = Path.cwd() / "tasks"
RESULTS_DIR = resolve_results_base()

RUNNER_CLASSES = {
    "claude": ClaudeRunner,
    "copilot": CopilotRunner,
    "gemini": GeminiRunner,
}

app = typer.Typer()


def load_tasks(skill: str, task_id: str | None = None, category: str | None = None) -> list[dict]:
    path = TASKS_DIR / f"{skill}.yaml"
    if not path.exists():
        available = [f.stem for f in TASKS_DIR.glob("*.yaml")]
        typer.echo(f"No tasks file found: {path}")
        if available:
            typer.echo(f"Available: {', '.join(available)}")
        raise typer.Exit(1)

    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        typer.echo(f"Error: {path} is not a valid YAML dict")
        raise typer.Exit(1)

    tasks = data.get("tasks", [])
    for t in tasks:
        if "id" not in t or "intent" not in t:
            typer.echo(f"Error: task missing required 'id' or 'intent' field: {t}")
            raise typer.Exit(1)

    if task_id:
        tasks = [t for t in tasks if t["id"] == task_id]
    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    return tasks


def save_result(runner_dir: Path, base: str, result: dict):
    """Save a runner result to disk in all formats."""
    with open(runner_dir / f"{base}.json", "w") as f:
        json.dump(result, f, indent=2)

    if result.get("conversation_md"):
        with open(runner_dir / f"{base}.md", "w") as f:
            f.write(result["conversation_md"])

    if result.get("raw_events"):
        with open(runner_dir / f"{base}.jsonl", "w") as f:
            f.write("\n".join(result["raw_events"]) + "\n")

    if result.get("stderr"):
        with open(runner_dir / f"{base}.stderr.log", "w") as f:
            f.write(result["stderr"] + "\n")


def variants_for_task(task: dict, requested: list[str]) -> list[str]:
    """Filter requested variants to those applicable for this task.

    `with-docs` is only applicable when the task declares non-empty
    `ground_truth.context_refs` — otherwise there's nothing to feed the agent
    and we'd just be re-running the without-skill baseline.
    """
    has_refs = bool(task.get("ground_truth", {}).get("context_refs"))
    return [v for v in requested if v != "with-docs" or has_refs]


def docs_context_for_task(task: dict) -> str:
    """Resolve the with-docs prompt prefix for a task, or empty string."""
    refs = task.get("ground_truth", {}).get("context_refs") or []
    if not refs:
        return ""
    # Local import: keeps this module importable without the anthropic SDK
    # for tests that only exercise non-grader paths.
    from evals.framework.grader import load_runner_refs

    return load_runner_refs(refs)


def validate_env_vars(tasks: list[dict]):
    """Check all required env vars upfront before running anything."""
    missing = {}
    for t in tasks:
        for env_var in t.get("env", []):
            if not os.environ.get(env_var):
                missing.setdefault(env_var, []).append(t["id"])
    if missing:
        typer.echo("Error: missing required environment variables:")
        for var, task_ids in sorted(missing.items()):
            typer.echo(f"  {var} (needed by: {', '.join(task_ids)})")
        raise typer.Exit(1)


def run_local(tasks, runner_cls, variants, skill_dir, max_turns, repeat, run_dir, timeout=60):
    """Run evals locally, sequentially."""
    r = runner_cls(skill_dir=skill_dir)
    # Variants are filtered per-task (with-docs only applies when the task has
    # context_refs), so the total reflects the actual work, not the matrix size.
    total = sum(len(variants_for_task(t, variants)) for t in tasks) * repeat
    done = 0

    for t in tasks:
        docs_context = docs_context_for_task(t)
        for v in variants_for_task(t, variants):
            runner_dir = run_dir / r.name
            runner_dir.mkdir(parents=True, exist_ok=True)

            for i in range(repeat):
                suffix = f"__{i + 1}" if repeat > 1 else ""
                base = f"{t['id']}__{v}{suffix}"

                if t.get("setup"):
                    setup_result = subprocess.run(t["setup"], shell=True, capture_output=True, text=True)
                    with open(runner_dir / f"{base}.setup.log", "w") as f:
                        f.write(
                            f"$ {t['setup']}\nexit={setup_result.returncode}\nstdout={setup_result.stdout}\nstderr={setup_result.stderr}\n"
                        )
                    if setup_result.returncode != 0:
                        done += 1
                        typer.echo(
                            f"[{done}/{total}] {t['id']} / {r.name}/{v} ... setup failed (exit {setup_result.returncode})"
                        )
                        result = {
                            "error": f"setup_failed (exit {setup_result.returncode})",
                            "stderr": setup_result.stderr.strip(),
                            "conversation_md": f"# Error\n\nSetup command failed (exit {setup_result.returncode})\n```\n$ {t['setup']}\n{setup_result.stderr[:1000]}\n```\n",
                        }
                        save_result(runner_dir, base, result)
                        continue

                done += 1
                run_label = f" (run {i + 1}/{repeat})" if repeat > 1 else ""
                label = f"[{done}/{total}] {t['id']} / {r.name}/{v}{run_label}"
                typer.echo(f"{label} ...", nl=False)

                with tempfile.TemporaryDirectory(prefix=f"eval-{t['id']}-") as tmpdir:
                    # Mount the skill into the agent's cwd for with-skill so
                    # Claude Code / Copilot can discover it via .claude/skills/
                    # walk-up. Without this the prompt's "Use the /<skill>"
                    # reference dangles and the agent improvises, often badly.
                    # Mirror of modal_runner's image.add_local_dir(...) line.
                    if v == "with-skill" and skill_dir:
                        local_skill = Path(skill_dir)
                        if local_skill.is_dir():
                            mount = Path(tmpdir) / ".claude" / "skills" / local_skill.name
                            shutil.copytree(local_skill, mount)

                    result = r.run(
                        t["intent"],
                        v,
                        max_turns=max_turns,
                        cwd=tmpdir,
                        docs_context=docs_context if v == "with-docs" else "",
                        timeout=timeout,
                        extra_tools=t.get("extra_tools") or None,
                    )

                    if t.get("verify"):
                        verify_result = subprocess.run(t["verify"], shell=True, capture_output=True, text=True)
                        result["verify_output"] = verify_result.stdout.strip()
                        result["verify_exit_code"] = verify_result.returncode
                        if verify_result.stderr.strip():
                            result["verify_stderr"] = verify_result.stderr.strip()
                        with open(runner_dir / f"{base}.verify.log", "w") as f:
                            f.write(
                                f"$ {t['verify']}\nexit={verify_result.returncode}\nstdout={verify_result.stdout}\nstderr={verify_result.stderr}\n"
                            )

                    save_result(runner_dir, base, result)

                    # Capture anything the agent wrote before the tempdir cleans up.
                    # Exclude .claude — we mounted the skill there ourselves; it's
                    # not agent output. Without this exclusion the workdir-empty
                    # autofail wouldn't fire and the grader would receive the skill's
                    # own SKILL.md as if the agent had produced it.
                    tmp_path = Path(tmpdir)
                    if any(tmp_path.iterdir()):
                        shutil.copytree(
                            tmp_path,
                            runner_dir / f"{base}.workdir",
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".venv", "node_modules", ".git", ".claude"),
                        )

                status = "error" if "error" in result else "ok"
                typer.echo(f" {status}")

                if t.get("teardown"):
                    td_result = subprocess.run(t["teardown"], shell=True, capture_output=True, text=True)
                    with open(runner_dir / f"{base}.teardown.log", "w") as f:
                        f.write(
                            f"$ {t['teardown']}\nexit={td_result.returncode}\nstdout={td_result.stdout}\nstderr={td_result.stderr}\n"
                        )


def run_remote(tasks, runner_name, variants, skill_dir, max_turns, repeat, run_dir, parallel, timeout=60):
    """Run evals in Modal sandboxes, one sandbox per (task, variant, repeat)."""
    import time

    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    from evals.remote.modal_runner import run_one_remote

    console = Console()

    # Build work items — one per sandbox
    work_items = []
    for t in tasks:
        docs_context = docs_context_for_task(t)
        for v in variants_for_task(t, variants):
            for i in range(repeat):
                suffix = f"__{i + 1}" if repeat > 1 else ""
                base = f"{t['id']}__{v}{suffix}"
                work_items.append(
                    {
                        "runner": runner_name,
                        "intent": t["intent"],
                        "variant": v,
                        "max_turns": max_turns,
                        "skill_dir": skill_dir if v == "with-skill" else "",
                        "docs_context": docs_context if v == "with-docs" else "",
                        "setup": t.get("setup", ""),
                        "teardown": t.get("teardown", ""),
                        "verify": t.get("verify", ""),
                        "extra_tools": t.get("extra_tools") or [],
                        "base": base,
                        "task_id": t["id"],
                        "run_num": i + 1,
                    }
                )

    runner_dir = run_dir / runner_name
    runner_dir.mkdir(parents=True, exist_ok=True)

    # Track state for each work item
    states = {}  # key -> "pending" | "running" | "ok" | "error" | "exception: ..."
    timings = {}  # key -> (start_time, end_time|None)
    for item in work_items:
        states[item["base"]] = "pending"

    def render_dashboard(elapsed: float) -> Table:
        table = Table(
            title=f"[bold]Remote Eval[/bold]  [dim]{runner_name}[/dim]  [dim]{elapsed:.0f}s elapsed[/dim]",
            show_lines=True,
            padding=(0, 1),
        )
        table.add_column("Task", style="bold", min_width=16)
        table.add_column("Variant", min_width=14)
        if repeat > 1:
            table.add_column("#", justify="center", width=4)
        table.add_column("Status", justify="center", min_width=12)
        table.add_column("Time", justify="right", min_width=8)

        for item in work_items:
            key = item["base"]
            state = states[key]

            if state == "pending":
                status = Text(" waiting ", style="dim")
            elif state == "running":
                dots = "." * (int(elapsed * 2) % 4)
                status = Text(f" running{dots: <4}", style="bold yellow")
            elif state == "ok":
                status = Text(" done ", style="bold green")
            elif state.startswith("error"):
                status = Text(" error ", style="bold red")
            else:
                status = Text(" error ", style="bold red")

            # Timing
            if key in timings:
                start, end = timings[key]
                dur = (end or time.time()) - start
                time_str = f"{dur:.1f}s"
            else:
                time_str = ""

            row = [
                item["task_id"],
                item["variant"],
            ]
            if repeat > 1:
                row.append(str(item["run_num"]))
            row.extend([status, time_str])
            table.add_row(*row)

        # Summary footer
        done = sum(1 for s in states.values() if s not in ("pending", "running"))
        passed = sum(1 for s in states.values() if s == "ok")
        failed = done - passed
        running = sum(1 for s in states.values() if s == "running")
        total = len(work_items)

        footer = Text()
        footer.append(f"\n {done}/{total} done", style="bold")
        if running:
            footer.append(f"  {running} running", style="yellow")
        if passed:
            footer.append(f"  {passed} passed", style="green")
        if failed:
            footer.append(f"  {failed} failed", style="red")
        table.caption = footer

        return table

    def run_item(item):
        key = item["base"]
        states[key] = "running"
        timings[key] = (time.time(), None)
        try:
            result = run_one_remote(
                runner=item["runner"],
                intent=item["intent"],
                variant=item["variant"],
                max_turns=item["max_turns"],
                skill_dir=item["skill_dir"],
                setup=item["setup"],
                teardown=item["teardown"],
                verify=item["verify"],
                workdir_out=runner_dir / f"{item['base']}.workdir",
                docs_context=item.get("docs_context", ""),
                timeout=timeout,
                extra_tools=item.get("extra_tools") or [],
            )
        except Exception as e:
            result = {
                "error": f"exception: {type(e).__name__}: {e}",
                "conversation_md": f"# Error\n\nSandbox raised exception: {e}\n",
            }
        timings[key] = (timings[key][0], time.time())
        save_result(runner_dir, item["base"], result)
        states[key] = "error" if "error" in result else "ok"
        return item, result

    start = time.time()

    with Live(render_dashboard(0), console=console, refresh_per_second=4) as live:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(run_item, item): item for item in work_items}

            while futures:
                # Update display while waiting
                live.update(render_dashboard(time.time() - start))

                done_futures = []
                for f in futures:
                    if f.done():
                        done_futures.append(f)

                for f in done_futures:
                    item = futures.pop(f)
                    try:
                        f.result()
                    except Exception as e:
                        states[item["base"]] = f"exception: {e}"

                if not done_futures and futures:
                    time.sleep(0.25)

        live.update(render_dashboard(time.time() - start))

    # Print final summary
    elapsed = time.time() - start
    passed = sum(1 for s in states.values() if s == "ok")
    total = len(work_items)
    console.print(f"\n[bold]{passed}/{total} passed[/bold] in {elapsed:.1f}s")


@app.command()
def main(
    skill: str = typer.Option(..., "--skill", "-s", help="Skill to test. Loads ./tasks/<skill>.yaml."),
    task: str | None = typer.Option(None, "--task", "-t", help="Filter to a single task by id."),
    category: str | None = typer.Option(None, "--category", "-c", help="Filter to tasks with this category."),
    runner: str = typer.Option(
        "claude", "--runner", "-r", help="Agent CLI to run against: claude, copilot, or gemini."
    ),
    variant: str | None = typer.Option(
        None,
        "--variant",
        "-v",
        help="Limit to one variant: with-skill, without-skill, or with-docs. Default: every variant the task supports (with-docs requires task.ground_truth.context_refs).",
    ),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per run."),
    timeout: int = typer.Option(
        90,
        "--timeout",
        help="Wall-clock budget (seconds) for each agent CLI invocation. Remote sandboxes get this plus a 60s buffer for setup/verify/teardown.",
    ),
    repeat: int = typer.Option(1, "--repeat", "-n", help="Run each (task, variant) N times for reliability stats."),
    skills_dir: str = typer.Option(
        "", "--skills-dir", help="Skills root dir. Overrides CULTIVAR_SKILLS_DIR env; default ./.claude/skills."
    ),
    title: str = typer.Option("", "--title", help="Name this run; appears in the results dir name."),
    notes: str = typer.Option("", "--notes", help="Free-form notes saved to <run>/notes.md and shown in the report."),
    remote: bool = typer.Option(
        False,
        "--remote",
        help="Run each (task, variant, repeat) in its own Modal sandbox. Recommended for parallelism + isolation.",
    ),
    parallel: int = typer.Option(
        5, "--parallel", "-p", help="Max concurrent sandboxes when --remote. Ignored locally."
    ),
    grade: bool = typer.Option(
        False, "--grade", help="After runs finish, invoke the grader and print a report. Requires ANTHROPIC_API_KEY."
    ),
    fail_under: float | None = typer.Option(
        None,
        "--fail-under",
        help=(
            "With --grade, exit 1 if the with-skill pass rate is below this percentage (0-100). "
            "Omit to always exit 0. Intended for CI gates."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the agent prompt, CLI command, and grader criteria — but don't execute. No API calls.",
    ),
):
    """Run eval tasks against an agent CLI runner (locally, or in Modal sandboxes with --remote).

    Loads tasks from ./tasks/<skill>.yaml and runs each (task × variant × repeat)
    against the chosen runner. Locally by default; --remote runs each combination
    in its own Modal sandbox. Results go to ./results/<timestamp>[__title]/.

    Examples:
      cultivar run -s my-skill -t my-task --dry-run            # preview prompt + command + criteria
      cultivar run -s my-skill -r claude --remote --grade      # run + grade + report in one shot
      cultivar run -s my-skill -r claude --remote -n 3 -p 5    # 3 repeats, 5 sandboxes in parallel
      cultivar run -s my-skill -r claude --remote --title v2   # name a run for later comparison
      cultivar run -s my-skill -r claude --timeout 180         # raise per-call budget to 180s (default 90)

    See docs/task-yaml.md for the task schema and docs/sandbox.md for --remote setup.
    """
    if repeat < 1:
        typer.echo("Error: --repeat must be >= 1")
        raise typer.Exit(1)

    tasks = load_tasks(skill, task, category)
    if not tasks:
        typer.echo("No tasks matched filters.")
        raise typer.Exit(1)

    runner_cls = RUNNER_CLASSES.get(runner)
    if not runner_cls:
        typer.echo(f"Unknown runner: {runner}. Available: {list(RUNNER_CLASSES.keys())}")
        raise typer.Exit(1)

    # Resolve skill directory for with-skill variant
    base_dir = resolve_skills_base(skills_dir)
    skill_dir = base_dir / skill
    if not (skill_dir / "SKILL.md").exists():
        typer.echo(f"Error: Skill '{skill}' not found at {skill_dir}")
        typer.echo(f"  Expected: {skill_dir / 'SKILL.md'}")
        typer.echo(f"  Place your skill in: {base_dir / '<skill-name>' / 'SKILL.md'}")
        raise typer.Exit(1)
    skill_dir_str = str(skill_dir)

    r = runner_cls(skill_dir=skill_dir_str)
    valid_variants = r.variants()
    if variant:
        if variant not in valid_variants:
            typer.echo(f"Error: Unknown variant '{variant}'. Available: {valid_variants}")
            raise typer.Exit(1)
        variants = [variant]
    else:
        variants = valid_variants

    # --dry-run: show what would be sent without running
    if dry_run:
        r = runner_cls(skill_dir=skill_dir_str)
        total_runs = 0
        for t in tasks:
            docs_context = docs_context_for_task(t)
            for v in variants_for_task(t, variants):
                total_runs += 1
                ctx = docs_context if v == "with-docs" else ""
                cmd, prompt = r.build_command(
                    t["intent"], v, max_turns, docs_context=ctx, extra_tools=t.get("extra_tools") or None
                )
                typer.echo(f"\n{'━' * 70}")
                typer.echo(f"Task:    {t['id']}")
                typer.echo(f"Variant: {v}")
                typer.echo(f"Runner:  {runner}")
                typer.echo("\n── Agent Prompt ──")
                typer.echo(prompt)
                typer.echo("\n── CLI Command ──")
                typer.echo(" ".join(cmd))
                gt = t.get("ground_truth", {})
                if gt:
                    typer.echo("\n── Grader Criteria ──")
                    typer.echo(gt.get("criteria", "(none)").strip())
                    if gt.get("commands"):
                        typer.echo(f"Expected commands: {', '.join(gt['commands'])}")
                    if gt.get("flexible"):
                        typer.echo(f"Flexibility: {'; '.join(gt['flexible'])}")
                    if gt.get("outcome"):
                        typer.echo(f"Expected outcome: {gt['outcome']}")
        typer.echo(f"\n{'━' * 70}")
        typer.echo(
            f"{len(tasks)} task(s), {total_runs} run(s) total (with-docs skipped for tasks with no context_refs)"
        )
        raise typer.Exit(0)

    # Validate env vars upfront before creating any directories or running anything
    validate_env_vars(tasks)

    if grade and not os.environ.get("ANTHROPIC_API_KEY"):
        typer.echo(
            "Error: --grade requires ANTHROPIC_API_KEY.\n"
            "Add ANTHROPIC_API_KEY=sk-ant-... to a .env in this directory (auto-loaded),\n"
            "or `export ANTHROPIC_API_KEY=...` in your shell."
        )
        raise typer.Exit(1)

    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if title:
        run_id += f"__{title.replace(' ', '-')}"
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    with open(run_dir / "tasks.json", "w") as f:
        json.dump({"skill": skill, "tasks": tasks}, f, indent=2)

    if notes:
        with open(run_dir / "notes.md", "w") as f:
            f.write(notes + "\n")

    REMOTE_RUNNERS = {"claude", "gemini", "copilot"}
    if remote:
        if runner not in REMOTE_RUNNERS:
            typer.echo(f"Note: '{runner}' doesn't support remote mode yet. Running locally.")
            run_local(tasks, runner_cls, variants, skill_dir_str, max_turns, repeat, run_dir, timeout)
        else:
            run_remote(tasks, runner, variants, skill_dir_str, max_turns, repeat, run_dir, parallel, timeout)
    else:
        run_local(tasks, runner_cls, variants, skill_dir_str, max_turns, repeat, run_dir, timeout)

    # Post-run summary
    n_convos = len(tasks) * len(variants) * repeat
    typer.echo(f"\nResults saved to: {run_dir}")
    typer.echo(f"  {len(tasks)} task(s) x {len(variants)} variant(s) x {repeat} repeat(s) = {n_convos} conversation(s)")

    if grade:
        typer.echo("\nGrading...")
        cmd = [sys.executable, "-m", "evals.cli", "grade", str(run_dir), "--skill", skill, "--report"]
        if fail_under is not None:
            cmd += ["--fail-under", str(fail_under)]
        result = subprocess.run(cmd)
        # Propagate the child's status. Previously discarded, which meant a failing
        # gate — or a crashed grader — still exited 0 and a CI job went green.
        if result.returncode != 0:
            raise typer.Exit(result.returncode)
    else:
        typer.echo("\nTo grade and view report:")
        typer.echo(f"  cultivar grade {run_dir} --report")


if __name__ == "__main__":
    app()
