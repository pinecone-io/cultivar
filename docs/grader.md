# Grader

LLM-based grader that scores runner conversations against natural-language criteria. Runs locally (never in the sandbox) so the Anthropic key stays on your machine.

## When it runs

- `cultivar run … --grade` — runs grader after the runs finish
- `cultivar grade <results-dir> --report` — re-grades an existing run (e.g. after editing criteria or adding examples)
- `cultivar grade latest` — most-recent run

## Required env

`ANTHROPIC_API_KEY`. Drop it in `.env` (cwd) — auto-loaded.

## Model

Default: `claude-haiku-4-5-20251001`. Override with `--model claude-…` — any current Claude model works, including the "-5" generation (`claude-opus-5`, `claude-sonnet-5`) and Fable/Mythos 5, which think by default. The grader detects the model family and adjusts the request so the reply is still plain JSON text:

- Older models (`claude-haiku-4-5`, `claude-sonnet-4-6`, the 4.x Opus/Sonnet line) already default to no thinking — nothing changes for them.
- Bare "-5" models (`claude-opus-5`, `claude-sonnet-5`) think by default, so the grader explicitly sends `thinking: {"type": "disabled"}`.
- `claude-fable-5` / `claude-mythos-5` can't disable thinking at all — the grader omits the `thinking` param, runs at `effort: low` to keep thinking shallow, and gives the response more `max_tokens` headroom since thinking and the JSON reply share the same budget. Response parsing scans for the first text block instead of assuming it's `content[0]`.

## Prompt anatomy

The grader prompt is assembled in this order ([`build_grader_prompt`](../evals/framework/grader.py)):

1. **Skill reference** — full `SKILL.md` of `--skill` (auto-detected from `tasks.json` if not passed)
2. **Criteria** — `task.ground_truth.criteria` verbatim
3. **Expected** — `commands`, `flexible`, `outcome` (each line if present)
4. **Reference material** — files listed in `task.ground_truth.context_refs` (cwd-relative paths), included verbatim. Capped at 100 KB combined; missing files warn + skip. Treated as ground truth for "what correct behavior looks like" — the grader can use it to judge specifics but isn't allowed to quote from it as evidence (evidence must come from the actual run). See [docs/task-yaml.md#worked-example-context_refs](task-yaml.md#worked-example-context_refs).
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
- **Cap:** 40 KB total — files truncated or omitted past the cap, with a note appended

Empty or missing workdirs contribute nothing — the section is omitted from the prompt entirely.

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

**Filtering.** Examples are filtered by `task_id` — only examples matching the active task are included. This keeps prompts small and on-topic.

**Authoring tips:**
- Pin examples to *real* runs, not hypotheticals. The closer to actual runner output, the better the calibration.
- Write `reasoning` from the grader's perspective: "passes because…" / "fails because…", citing the criteria.
- Add a fail example *before* tightening criteria — it teaches the grader the failure mode without you having to over-specify.

**Promoting a run to an example.** Copy the relevant conversation or workdir content into an example YAML by hand for now.

## Output

`grades.json` in the run dir. Each entry:

```json
{
  "task_id": "...", "runner": "...", "variant": "...", "run_num": 1,
  "pass": true, "proposed_command": "...", "evidence": "...", "reasoning": "...",
  "suggestions": [{"cause": "...", "fix": "..."}],
  "duration_s": 12.3, "cost_usd": 0.0123,
  "num_turns": 4, "input_tokens": 1234, "output_tokens": 567,
  "session_id": "...", "category": "..."
}
```

`suggestions` is empty `[]` on clean passes. On failures it carries 1–3 `{cause, fix}` entries the grader thinks are probable root causes + concrete next steps; `cultivar report` and `cultivar show … --grader` render them as a yellow `cause → fix` bullet list. Plus `sandbox_timing` (create/setup/eval/teardown phase splits) when run remotely.

## Re-grading

`cultivar grade latest --report` re-grades without re-running the agents. Use this loop when iterating on `criteria` or adding calibration examples — it's cheap (one Haiku call per task) and fast.

## Sources

- [evals/framework/grader.py](../evals/framework/grader.py) — prompt assembly, workdir loader, calibration loader, grading loop
- [evals/framework/reporting.py](../evals/framework/reporting.py) — report rendering and `resolve_results_dir`
