# Concepts

A 5-minute read for someone new to cultivar. Read this before the per-area docs.

## The question this framework answers

> *"Does adding this skill to an agent's context actually make it behave better?"*

You write a skill (a markdown file with patterns, commands, examples). You hand it to an agent. The agent does *something* — and you want to know whether that something is better than what the agent would have done without the skill.

cultivar automates that comparison.

## The unit of measurement: a task

A **task** is a small, self-contained jobs the agent is asked to do, plus a description of what success looks like:

```yaml
- id: list-indexes
  intent: "list all my pinecone indexes"
  ground_truth:
    criteria: |
      PASS if the agent runs `pc index list` (or alias) and surfaces names.
      FAIL if it uses the SDK directly or invents indexes.
```

The agent runs the task. cultivar saves the conversation. An LLM grader reads the conversation against the criteria and returns `{pass, evidence, reasoning}`.

That's the atom. Everything else is composition: many tasks per skill, many runs per task, many runners.

## The controls: with-skill, without-skill, with-docs

For each task, the agent runs **up to three times**:

- **with-skill** — the skill is loaded into the agent's context (via `/<skill-name>` invocation, or auto-discovery from `.claude/skills/`)
- **without-skill** — the skill is absent; everything else is the same agent, same model, same task
- **with-docs** *(optional)* — no skill loaded, but the task's `context_refs` files are prepended to the prompt as raw reference material. Only runs for tasks that declare `context_refs` in `ground_truth`; otherwise skipped.

Two deltas to read:

| Comparison | Question it answers |
|---|---|
| with-skill vs without-skill | Is the skill doing anything at all? |
| with-skill vs with-docs | Is my distilled skill better than just dumping the docs into the prompt? |

If `with-skill` beats `with-docs` on pass rate or cost, your distillation is real. If they're a wash, the skill is repackaging effort the agent could do itself given the same source material — worth a hard look. See [docs/task-yaml.md](task-yaml.md#variants) for how to add `context_refs` to a task and [docs/grader.md](grader.md) for how the grader uses the same files.

> **Remote runs.** When you pass `--remote`, each `(task, variant, repeat)` runs in its own Modal sandbox — three variants = three sandboxes per task, run in parallel up to `--parallel N` (default 5). Genuinely apples-to-apples: same image, same task, only the prompt + skill mounting differ.

> **Caveat** The without-skill variant isn't a strictly identical baseline across all runners. On **Claude**, without-skill is a clean baseline — same flags, just no skill mounted and no `Use the /<skill>` in the prompt; we used to pass `--bare` but removed it after it silently filtered `Write` from the tool set and broke code-gen. On **Copilot**, without-skill still passes `--no-custom-instructions` and `--excluded-tools skill`, so the delta there reflects skill + AGENTS.md loading. Pass-rate comparisons are honest everywhere; token/cost comparisons are directionally useful but not strictly like-for-like across runners.

## What "better" means

Three signals, all in `grades.json` and the report:

| Signal | What it tells you |
|---|---|
| **Pass rate** | Did the agent do the right thing more often with the skill? |
| **Cost / tokens** | Is the skill paying for itself? A skill that doubles cost for marginal gains is suspect. |
| **Duration / turns** | Is the skill helping the agent get there faster, or making it overthink? |

Pass rate is primary. The others are guardrails.

## Why an LLM grader, not exact matching

Skill criteria are usually qualitative: *"PASS if the agent uses the right CLI command and explains the result."* Hard to express as regex. An LLM grader reads the natural-language criteria, the conversation, and any post-run state — and judges. We use Claude Haiku by default (cheap, fast, plenty smart for grader work).

The risk is grader drift. Two safeguards:

- **Criteria specificity.** Vague criteria → vague grades. Spell out PASS conditions and concrete failure modes. See [docs/grader.md](grader.md).
- **Calibration examples.** Anchor the grader with labeled past runs (`examples/<skill>/{pass,fail}/<name>.yaml`). Filtered per-task, included in every grader prompt.

## Three runners

The same task + same criteria runs against three agent CLIs:

- **Claude** ([Anthropic](https://docs.claude.com/en/docs/claude-code))
- **Copilot** ([GitHub](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli))
- **Gemini** ([Google](https://github.com/google-gemini/gemini-cli))

This lets you see whether your skill helps *all* agents or just one. A skill that only helps Claude probably leans on Claude-specific tool patterns; one that helps all three is more portable.

## Local vs remote (Modal sandboxes)

- **Local:** `cultivar run --skill X --runner claude` — uses your local CLI install, your auth, your filesystem. Fast for one-offs.
- **Remote:** `... --remote` — each (task, variant, repeat) runs in its own [Modal](https://modal.com) sandbox. Isolated filesystem, no auth-state collisions, parallel by default. Recommended for anything beyond a quick check.

See [docs/sandbox.md](sandbox.md).

## Repeats and reliability

LLMs are noisy. One run isn't a signal — it's a sample. Use `--repeat N` to run each (task, variant) N times; pass rate becomes a fraction (`3/3 passed`, `2/3 passed`) and you can tell signal from variance.

## What this framework is *not*

- **Not a benchmark of agent quality.** It's about *skill effectiveness*. A skill that improves Claude on a task says nothing about whether Claude is "better than" Gemini overall.
- **Not a unit-test suite for the skill itself.** The skill's correctness lives in `SKILL.md` and is judged by how it shapes agent behavior, not by direct assertion.
- **Not a regression catcher for the skill author's repo.** It tests the skill against agent behavior, not the surrounding code.

## Typical workflow

1. **Verify the install** (`cultivar hello` → runs a packaged smoke task; confirms runner CLI auth, workdir capture, and grader work end-to-end).
2. **Write the skill** (`SKILL.md` under `.claude/skills/<skill>/`).
3. **Scaffold tasks** (`cultivar init <skill>` → edit `tasks/<skill>.yaml`).
4. **Dry-run** (`cultivar run -s <skill> -t <task> --dry-run`) to see exactly what the agent will be asked.
5. **Run + grade** (`cultivar run -s <skill> -r claude --remote --grade`).
6. **Inspect** (`cultivar report`, `cultivar show <run>`).
7. **Calibrate** when the grader misjudges — add labeled examples to `examples/<skill>/{pass,fail}/`.
8. **Iterate on the skill**, rerun, compare.

## Where to go next

- **Authoring tasks:** [docs/task-yaml.md](task-yaml.md)
- **How grading works:** [docs/grader.md](grader.md)
- **Modal sandbox setup + lifecycle:** [docs/sandbox.md](sandbox.md)
- **Per-runner specifics + quirks:** [docs/runners/claude.md](runners/claude.md), [gemini.md](runners/gemini.md), [copilot.md](runners/copilot.md)
- **Contributing:** [CONTRIBUTING.md](../CONTRIBUTING.md)
