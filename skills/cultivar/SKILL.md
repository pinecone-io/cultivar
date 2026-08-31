---
name: cultivar
description: Drive the cultivar CLI to test whether an agent skill improves behavior — scaffold tasks, run with/without the skill across Claude/Copilot/Gemini (locally or on Modal), grade against a rubric, and read the results.
---

# cultivar

cultivar is a CLI that measures whether an agent **skill** actually improves an agent's
behavior. For each task it runs the agent **with the skill** and **without** it (and
optionally **with the source docs**), then an LLM grader scores each run against a
natural-language rubric. Use this skill when the user wants to create, run, or interpret
cultivar evals.

## The loop

1. `cultivar init <skill>` — scaffold `tasks/<skill>.yaml` + a `SKILL.md` stub.
2. Edit the task file (intent + PASS/FAIL criteria) and the skill.
3. `cultivar run -s <skill> -r <runner> --grade` — run all variants and grade.
4. `cultivar report` / `cultivar show latest -t <task>` — read the outcome.
5. Iterate on the skill; re-run; compare.

Always confirm the install first with `cultivar hello` (or `cultivar hello --no-grade`
when no `ANTHROPIC_API_KEY` is available) — it runs a packaged smoke task end-to-end.

## Commands

- `cultivar init <skill> [--skills-dir DIR]` — scaffold task YAML + SKILL.md stub.
- `cultivar run -s <skill> -r <claude|copilot|gemini>` — run. Key flags:
  - `-t <task>` one task · `-v <with-skill|without-skill|with-docs>` one variant
  - `--remote` run in isolated Modal sandboxes · `-n N` repeat · `-p N` parallelism
  - `--grade` grade after running · `--title NAME` label the run · `--dry-run` print the
    prompt + command without calling anything · `--timeout S` per-call budget (default 90)
- `cultivar grade <run|latest> -s <skill> [--report]` — (re)grade an existing run.
  `--model` picks the grading model (any current Claude model works, including the "-5"
  generation) · `--max-tokens` raises the per-reply budget if evidence/reasoning truncate.
- `cultivar report [run]` — summary table across runners/variants.
- `cultivar show <run|latest> -t <task> [--grader|--conversation-only|--workdir]` — inspect one run.

`--dry-run` is the safe way to preview exactly what will be sent before spending tokens.

## Variants (the controls)

- **with-skill** — skill loaded; prompt prefixed `Use the /<skill>`.
- **without-skill** — no skill; identical otherwise. The baseline.
- **with-docs** — no skill, but the task's `ground_truth.context_refs` files are prepended.
  Only runs for tasks that declare `context_refs`.

Read two deltas: with-skill vs without-skill ("does the skill do anything?") and
with-skill vs with-docs ("is the distilled skill better than dumping the raw docs?").

## Tasks

`tasks/<skill>.yaml` holds one or more tasks. Each task:

```yaml
tasks:
  - id: a-short-id
    intent: "what you'd ask the agent to do"
    category: cli            # or: code-gen
    # setup / teardown / verify: optional shell hooks
    # env: ["SOME_KEY"]      # required env vars, checked upfront
    ground_truth:
      criteria: |
        PASS requires <2-3 concrete, checkable things>.
        FAIL if <a common failure mode>.
      # context_refs: [docs/ref.md]   # activates the with-docs variant
```

Guidance:
- For **code-gen** tasks, the intent must say "write a file … in the current directory."
  Anything the agent writes to its cwd is captured and shown to the grader. A code-gen
  task that produces no file auto-fails.
- Write criteria as crisp PASS conditions + at least one concrete FAIL mode — vague
  criteria produce vague grades.

## Where skills live

cultivar tests exactly **one** skill per run (the `-s` one). It resolves the skills root
as: `--skills-dir` flag → `CULTIVAR_SKILLS_DIR` env → `./.claude/skills`. Keep
skills-under-test outside `.claude/` (e.g. `./skills`, via `CULTIVAR_SKILLS_DIR=skills`)
if you don't want your interactive coding agent to auto-load them.

## Local vs remote

- **Local** (default) — uses the runner CLI installed on your machine + its auth.
- **`--remote`** — each (task, variant, repeat) runs in its own Modal sandbox: clean
  isolation, parallelism, reproducibility. Requires a Modal account (`modal token new`)
  and a secret holding the agent's `ANTHROPIC_API_KEY` (default secret name
  `eval-sandbox-secrets`; override with `CULTIVAR_MODAL_SECRET`). Prefer `--remote` for
  rigorous comparisons. The grader always runs locally and needs `ANTHROPIC_API_KEY`.

## Reading results

`results/<timestamp>[__title]/` holds per-run `.json` (stats), `.md` (readable trace),
`.jsonl` (raw events), and `.workdir/` (files the agent wrote). `grades.json` holds the
verdicts. Use `cultivar report` for the table and `cultivar show … --grader` for the
grader's reasoning + suggestions on a failure.

## Gotchas

- Grading needs `ANTHROPIC_API_KEY` (loaded from a `.env` in the cwd). `hello --no-grade`
  and `run --dry-run` need no key.
- A grader call that fails is recorded as a FAIL for that conversation and the run
  continues. Auth and permission errors abort the whole grading run immediately.
- `tasks/`, `examples/`, and `results/` are cwd-relative and user-owned.
- One run is a sample. Use `-n 3` (or more) for anything you'll act on.
