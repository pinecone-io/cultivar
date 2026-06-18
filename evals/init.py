"""`cultivar init <skill>` — scaffold a tasks/<skill>.yaml and optional SKILL.md."""

from pathlib import Path

import typer

from evals.framework.reporting import console, resolve_skills_base

app = typer.Typer(pretty_exceptions_enable=False)

TASK_TEMPLATE = """\
# Tasks for the "{skill}" skill. One YAML per skill.
# Full schema + every field: docs/task-yaml.md (https://github.com/pinecone-io/cultivar/blob/main/docs/task-yaml.md)

tasks:
  # Example 1 — CLI-style task: agent runs a shell command, grader checks the conversation.
  - id: example-cli-task
    intent: "do something with the {skill} tool"
    # setup: "optional shell command run before the agent"
    # teardown: "optional shell command run after (cleanup)"
    # verify: "optional shell command run after; output is passed to the grader"
    # env: ["SOME_REQUIRED_ENV_VAR"]
    ground_truth:
      criteria: |
        Describe what correct looks like in plain English.
        PASS requires:
        - <specific thing 1>
        - <specific thing 2>
        FAIL if:
        - <common failure mode>
      commands: ["some-cli-command"]      # optional
    category: cli

  # Example 2 — code-gen task: agent writes files, grader reads them.
  # Anything the agent writes to cwd is captured to <base>.workdir/ and shown
  # to the grader under a "## Generated Code Files" section.
  - id: example-code-gen-task
    intent: "Write a file <filename> that <does something>"
    ground_truth:
      criteria: |
        PASS requires a file named <filename> that <does the expected thing>.
        Acceptable variations:
        - <e.g. single vs double quotes>
        - <e.g. case-insensitive matching>
        FAIL if:
        - No file was written
        - File has the wrong name
        - File doesn't do the expected thing
    category: code-gen

  # Example 3 — task with context_refs: pin one or more docs the grader treats
  # as authoritative AND activate the `with-docs` runner variant. This gives
  # you a three-way comparison: with-skill / without-skill / with-docs, so
  # you can answer "is my distilled skill better than just dumping the docs
  # in the agent's prompt?" Only tasks that declare `context_refs` get the
  # with-docs variant; without it, runs are just the standard two variants.
  #
  # Paths are cwd-relative or absolute. Combined cap: 100 KB. URLs not yet
  # supported — `curl https://... > docs/refs/source.md` first.
  - id: example-task-with-context-refs
    intent: "do the thing per your team's best-practices doc"
    ground_truth:
      criteria: |
        PASS requires the agent to follow the patterns spelled out in
        docs/best-practices.md — specifically <name 2-3 concrete things>.
        FAIL if the agent ignores those patterns.
      context_refs:
        - docs/best-practices.md
        # - docs/another-reference.md   # add more refs as needed
    category: cli
"""

SKILL_TEMPLATE = """\
---
name: {skill}
description: One-line description of what this skill teaches the agent.
---

# {skill}

Describe the skill's guidance here. The agent sees this content when invoked
via `/{skill}` in the with-skill variant.
"""


@app.command()
def main(
    skill: str = typer.Argument(..., help="Skill name (used as the YAML filename and skill directory)."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing tasks/<skill>.yaml or SKILL.md if present."
    ),
    skill_md: bool = typer.Option(
        True,
        "--skill-md/--no-skill-md",
        help="Also create <skills-dir>/<skill>/SKILL.md as a placeholder. Default: yes.",
    ),
    skills_dir: str = typer.Option(
        "", "--skills-dir", help="Skills root dir. Overrides CULTIVAR_SKILLS_DIR env; default ./.claude/skills."
    ),
):
    """Scaffold tasks/<skill>.yaml (+ placeholder <skills-dir>/<skill>/SKILL.md) for a new skill.

    Writes ./tasks/<skill>.yaml with three template tasks (CLI-style, code-gen,
    and one with context_refs to activate the with-docs runner variant), and a
    SKILL.md stub under the resolved skills dir. The skills dir is ./.claude/skills
    by default; override with --skills-dir or the CULTIVAR_SKILLS_DIR env var (e.g.
    keep skills under ./skills so your interactive agent doesn't auto-load them).

    Examples:
      cultivar init my-skill                  # scaffold both files
      cultivar init my-skill --skills-dir skills  # scaffold SKILL.md under ./skills/
      cultivar init my-skill --no-skill-md    # only the task YAML; reuse an existing skill
      cultivar init my-skill --force          # overwrite an existing tasks file

    See docs/task-yaml.md for the full task schema and docs/concepts.md for the
    with-skill / without-skill / with-docs variant breakdown.
    """
    cwd = Path.cwd()
    tasks_dir = cwd / "tasks"
    task_file = tasks_dir / f"{skill}.yaml"
    skill_md_path = resolve_skills_base(skills_dir) / skill / "SKILL.md"

    def _disp(p: Path) -> str:
        try:
            return str(p.relative_to(cwd))
        except ValueError:
            return str(p)

    created = []
    skipped = []

    # tasks/<skill>.yaml
    if task_file.exists() and not force:
        skipped.append(f"{_disp(task_file)} (already exists; use --force to overwrite)")
    else:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file.write_text(TASK_TEMPLATE.format(skill=skill))
        created.append(_disp(task_file))

    # <skills-dir>/<skill>/SKILL.md — only if --skill-md and not already there
    if skill_md:
        if skill_md_path.exists() and not force:
            skipped.append(f"{_disp(skill_md_path)} (already exists)")
        else:
            skill_md_path.parent.mkdir(parents=True, exist_ok=True)
            skill_md_path.write_text(SKILL_TEMPLATE.format(skill=skill))
            created.append(_disp(skill_md_path))

    for path in created:
        console.print(f"[green]created[/green]  {path}")
    for note in skipped:
        console.print(f"[yellow]skipped[/yellow]  {note}")

    if created:
        sd = f" --skills-dir {skills_dir}" if skills_dir else ""
        console.print(
            f"\nNext: edit the tasks file and run [bold]cultivar run --skill {skill} --runner claude{sd}[/bold]"
        )


if __name__ == "__main__":
    app()
