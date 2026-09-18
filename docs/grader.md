# Grader

Grades runner conversations against natural-language criteria. Two backends: **Claude** (the default) and **TypeSafe System One**. Both run locally, never in the sandbox, so API keys stay on your machine.

## When it runs

- `cultivar run … --grade` — runs grader after the runs finish
- `cultivar run … --grade --grade-backend typesafe` — same, graded by System One
- `cultivar grade <results-dir> --report` — re-grades an existing run (e.g. after editing criteria or adding examples)
- `cultivar grade latest` — most-recent run

## Backends

Two graders. **Claude is the default and nothing changes unless you opt in.**

| | `claude` (default) | `typesafe` |
|---|---|---|
| What it is | Anthropic Messages API, returns a JSON object | TypeSafe System One (Jev), returns typed judgments |
| Verdict from | The model writing `"pass": true` | `P(completed) >= 0.5` (a calibrated probability) |
| `evidence` | A quote from the run | **None** — says so in plain text |
| `reasoning` | 1–2 sentences the model wrote | Mechanical: the probability and threshold |
| `suggestions` | Model-written `cause`/`fix` | Mapped in code from a `failure_kind` label |
| Env | `ANTHROPIC_API_KEY` | `TYPESAFE_API_KEY` |

```bash
cultivar grade latest                      # claude (default)
cultivar grade latest --backend typesafe   # System One
```

The TypeSafe SDK is an optional extra, not part of the base install — the default backend never needs it:

```bash
uv add 'cultivar[typesafe]'      # or: pip install 'cultivar[typesafe]'
```

Without it, `--backend typesafe` exits 1 with an install hint instead of a traceback; everything else is unaffected.

### When to use which

**Use `claude` (the default) when:**
- You're iterating on a skill and need to know *why* a run failed. This is the common case.
- Your `criteria` were tuned against Claude's reading of them.
- You rely on calibration examples (see below — `typesafe` ignores them).
- A task has large `context_refs`; Claude's context window fits what Jev's state budget won't.

**Use `typesafe` when:**
- It's a CI gate that only reads pass/fail. You never look at `evidence` there.
- You're re-grading a large back-catalogue and cost or wall-clock matters.
- You want a calibrated probability rather than a verdict — `typesafe_p_completed` tells you *how* marginal a run was, which a binary pass/fail hides.

Measured on one task (`hybrid-trap`, 6 conversations, both backends given identical evidence):

| | claude (`haiku-4-5`) | typesafe (`jev-latest`) |
|---|---|---|
| Per grade | 6.87s | 0.40s |
| Per 1000 grades | $18.66 | $0.63 |
| Agreement | — | 6/6 with claude |

Passes came back at 0.90–0.94 and failures at 0.12–0.20, so the verdicts weren't threshold-sensitive. **That's one task** — validate on your own before trusting it as a gate.

### Per-task pinning

A task can pin its backend in the task YAML:

```yaml
tasks:
  - id: debug-heavy-task
    grader_backend: claude     # claude | typesafe; omit to use the run default
```

**A pin beats `--backend`**, because it records a property of the task (this one's criteria are only useful with quoted evidence) rather than a preference for one run. `--backend` sets the default for tasks that don't pin, so a mixed run is fine and the header prints the breakdown:

```
Backend: typesafe (default); per-task: hybrid-trap=claude
```

An unknown value warns and falls back to the default rather than aborting a long run over one typo.

### Both backends must see the same evidence

Every section of the Claude prompt maps to a TypeSafe state field, tracked in `SECTION_PARITY` in [typesafe_grader.py](../evals/framework/typesafe_grader.py) and asserted by tests. If you add a section to one backend, add it to the other — otherwise the two graders judge from different inputs and any comparison between them is meaningless. This is not hypothetical: `SKILL.md` was briefly sent to Claude and not to Jev, which silently invalidated a benchmark.

**One intentional exception: calibration examples are not sent to `typesafe`.** They exist to anchor a text model's pass/fail threshold; System One is calibrated already and is steered by `PASS_THRESHOLD` instead. The grader warns when examples exist and you've selected `typesafe`, because those verdicts are *not* calibrated by them the way Claude's are.

### State budget

Jev budgets **32,000 tokens** for state plus the longest question, where Claude has a far larger window. The grader's own caps (50k chars of conversation + 40k workdir + 100k `context_refs` + `SKILL.md`) can exceed that, so the TypeSafe path trims — shedding `skill_reference`, then `reference_material`, then `generated_code_files`, then `agent_conversation` last. `task_criteria` is never trimmed; losing it changes the question being asked.

Check before you spend anything:

```bash
cultivar grade latest --backend typesafe --dry-run
```

```
hybrid-trap / claude/with-skill (run 1)
  skill_reference            41,164 chars  ~11,761 tok   72.6%
  agent_conversation          7,022 chars  ~ 2,006 tok   12.4%
  generated_code_files        6,968 chars  ~ 1,990 tok   12.3%
  task_criteria               1,575 chars  ~   450 tok    2.8%
  TOTAL                      56,729 chars  ~16,208 tok  (cap ~26,142 tok) fits
```

No API calls, no `grades.json`. Token counts are estimates (3.5 chars/token, deliberately conservative); a state flagged as over budget gets truncated at grade time and is judged on less evidence than Claude would see.

**Exit code 1 if anything is over budget**, 0 otherwise, so this works as a CI preflight before a `--backend typesafe` gate.

## Required env

`ANTHROPIC_API_KEY` for the default backend. `TYPESAFE_API_KEY` only if you use `--backend typesafe`. Drop either in `.env` in your cwd; they're auto-loaded. A run only requires the keys its tasks actually need, and a missing key fails immediately with a clear message rather than falling back to the other backend — a Claude verdict written into a run you asked TypeSafe to grade would make `grades.json` lie.

## Model

Applies to the `claude` backend. For `typesafe`, use `--typesafe-model` (default `jev-latest`).

Default: `claude-haiku-4-5-20251001`. Override with `--model claude-…`. Any current Claude model works, including the "-5" generation (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-5`, optionally pinned to a dated snapshot like `claude-opus-5-20260315`) and Fable/Mythos 5, which always think and can't turn it off. The grader classifies the model from its id alone and adjusts the request so the reply is still plain JSON text:

- Older models (`claude-haiku-4-5`, `claude-sonnet-4-6`, the 4.x Opus/Sonnet line) already default to no thinking. Nothing changes for them.
- Bare or dated "-5" models (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-5`, `claude-opus-5-20260315`, …) think by default, so the grader explicitly sends `thinking: {"type": "disabled"}`.
- `claude-fable-5` / `claude-mythos-5` (and their dated snapshots) can't disable thinking at all. The grader omits the `thinking` param, runs at `effort: low` to keep thinking shallow, and doubles `--max-tokens` since thinking and the JSON reply share the same budget.

Response parsing scans past leading `thinking`/`redacted_thinking` blocks for the first text block instead of assuming it's `content[0]`. It still raises loudly on any other unexpected block type, e.g. `tool_use`, since this call never passes tools and seeing one means something is misconfigured.

## Max tokens

`--max-tokens` (default `4096`) caps each grader reply.

Raise it if `evidence`/`reasoning` are getting truncated. Check `grades.json` for a `reasoning` mentioning `_salvage_truncated_grade`'s header-only recovery. Models that can't disable thinking (`claude-fable-5`, `claude-mythos-5`) already get double whatever value you pass here.

## Grader call failures

If a single conversation's grading call raises (e.g. a thinking-only model exhausts `--max-tokens` before producing any text), it no longer aborts the whole run. `_grade_conversation_safely` converts the exception into a normal FAIL grade with a `cause`/`fix` suggestion naming the error, prints a warning, and the batch continues. Only that one entry needs a re-grade.

Exception: `AuthenticationError` and `PermissionDeniedError` (a bad or revoked API key) still propagate and crash the run immediately. That failure repeats identically on every remaining conversation, so surfacing it once beats grinding through the batch producing a wall of duplicate FAIL grades.

## Prompt anatomy

The grader prompt is assembled in this order ([`build_grader_prompt`](../evals/framework/grader.py)):

1. **Skill reference** — full `SKILL.md` of `--skill` (auto-detected from `tasks.json` if not passed)
2. **Criteria** — `task.ground_truth.criteria` verbatim
3. **Expected** — `commands`, `flexible`, `outcome` (each line if present)
4. **Reference material** — files listed in `task.ground_truth.context_refs` (cwd-relative paths), included verbatim. Capped at 100 KB combined; missing files warn + skip. Treated as ground truth for "what correct behavior looks like": the grader can use it to judge specifics but can't quote from it as evidence (evidence must come from the actual run). See [docs/task-yaml.md#worked-example-context_refs](task-yaml.md#worked-example-context_refs).
5. **Calibration examples** — pass/fail YAMLs filtered by `task_id`
6. **Agent conversation** — `conversation_md` truncated at 50 KB
7. **Verification output** — stdout of `task.verify` if defined
8. **Workdir files** — files the agent wrote to its tempdir, allowlisted by extension, capped at 40 KB total
9. **Instructions** — return JSON only: `{pass, proposed_command, evidence, reasoning, suggestions}`. On FAIL or partial pass, the grader populates `suggestions: [{cause, fix}, ...]` with 1–3 actionable items (probable root cause + concrete next step). Clean PASS returns `suggestions: []`.

## Workdir filtering

- **Included extensions:** `.py .ts .tsx .js .jsx .json .yaml .yml .toml .md .txt .sh`
- **Included names:** `Dockerfile Makefile requirements.txt pyproject.toml package.json .env.example`
- **Skipped dirs:** `__pycache__ node_modules .venv .git dist build`
- **Skipped files:** `*.lock`
- **Cap:** 40 KB total. Files truncated or omitted past the cap, with a note appended.

Empty or missing workdirs contribute nothing. The section is omitted from the prompt entirely.

## `context_refs` is dual-use

The same files you list under `ground_truth.context_refs` for the grader **also** activate a third runner variant (`with-docs`) that prepends those files to the agent's own prompt. Lets you compare a distilled skill against just dumping the docs into context. See [docs/concepts.md](concepts.md#the-controls-with-skill-without-skill-with-docs) for when to use it. If you only want the grader use and not the runner use, omit the variant via `--variant with-skill` (or `without-skill`); the with-docs variant is only ever auto-included alongside the others.

## Calibration examples

Optional but powerful. Anchor the grader on real pass/fail cases for a specific task.

**Layout:** `examples/<skill>/{pass,fail}/<name>.yaml`

```yaml
task_id: my-task-id
label: pass     # or fail
agent_proposed: |
  what the agent did or proposed
reasoning: |
  why this passes (or fails) the criteria
```

**Filtering.** Examples are filtered by `task_id`. Only examples matching the active task are included, keeping prompts small and on-topic.

**Authoring tips:**
- Pin examples to *real* runs. Skip hypotheticals. The closer to actual runner output, the better the calibration.
- Write `reasoning` from the grader's perspective: "passes because…" / "fails because…", citing the criteria.
- Add a fail example *before* tightening criteria. It teaches the grader the failure mode without over-specifying.

**Promoting a run to an example.** Copy the relevant conversation or workdir content into an example YAML by hand for now.

## Output

`grades.json` in the run dir. Each entry:

```json
{
  "task_id": "...", "runner": "...", "variant": "...", "run_num": 1,
  "pass": true, "proposed_command": "...", "evidence": "...", "reasoning": "...",
  "suggestions": [{"cause": "...", "fix": "..."}],
  "grader_backend": "claude", "grader_model": "claude-haiku-4-5-20251001",
  "duration_s": 12.3, "cost_usd": 0.0123,
  "num_turns": 4, "input_tokens": 1234, "output_tokens": 567,
  "session_id": "...", "category": "..."
}
```

`grader_backend` and `grader_model` record who produced the verdict — always check them before comparing two runs. `cultivar report` and `cultivar show … --grader` print the backend, and warn on a `typesafe` grade that there's no quoted evidence or model-written reasoning, so an empty `evidence` field doesn't read as a bug.

TypeSafe grades carry three extra keys:

```json
{
  "typesafe_p_completed": 0.94,
  "typesafe_failure_kind": "wrong_approach",
  "typesafe_failure_confidence": 0.62
}
```

`typesafe_p_completed` is the calibrated probability behind the verdict — worth reading directly, since a 0.49 FAIL and a 0.02 FAIL are very different runs. A low `typesafe_failure_confidence` on a confident pass/fail means the *category* was ambiguous, not the verdict; treat it as a hint that the failure taxonomy needs work, not that the grade is wrong.

`duration_s`, `cost_usd`, `num_turns` and the token counts describe the **agent run**, not the grading call, and are identical under both backends.

`suggestions` is empty `[]` on clean passes. On failures it carries 1–3 `{cause, fix}` entries the grader thinks are probable root causes + concrete next steps; `cultivar report` and `cultivar show … --grader` render them as a yellow `cause → fix` bullet list. Plus `sandbox_timing` (create/setup/eval/teardown phase splits) when run remotely.

## Re-grading

`cultivar grade latest --report` re-grades without re-running the agents. Use this loop when iterating on `criteria` or adding calibration examples. It's cheap (one Haiku call per task on the default backend) and fast — and ~30x cheaper again with `--backend typesafe` when you only need the verdict.

## Sources

- [evals/framework/grader.py](../evals/framework/grader.py) — prompt assembly, workdir loader, calibration loader, grading loop
- [evals/framework/typesafe_grader.py](../evals/framework/typesafe_grader.py) — System One backend, state assembly, `SECTION_PARITY`, budget trimming
- [evals/framework/reporting.py](../evals/framework/reporting.py) — report rendering, backend banner, and `resolve_results_dir`
