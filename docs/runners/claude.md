# Claude runner

Wraps [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) as a runner. This is cultivar's default and most exercised runner.

## Install

```bash
npm install -g @anthropic-ai/claude-code
```

In Modal sandboxes the CLI is preinstalled in the image; auth comes from `ANTHROPIC_API_KEY` in `eval-sandbox-secrets`.

## Auth

Locally, `claude` uses whatever auth you've already set up — it OAuths on first run (or reads `ANTHROPIC_API_KEY`). No extra setup beyond a working `claude` CLI.

In Modal sandboxes the key is injected via `eval-sandbox-secrets` (the secret name defaults to that, configurable via `CULTIVAR_MODAL_SECRET`). Note the grader always runs locally and needs its own `ANTHROPIC_API_KEY` regardless of where the agent ran.

## How it's invoked

```bash
claude -p "<prompt>" \
  --output-format stream-json \
  --verbose \
  --max-turns <N> \
  --allowedTools <comma-joined tools> \
  [--model <model>]
```

`--verbose` is required to get the stream-json event stream in `-p` (headless) mode. `--max-turns` defaults to 10; the per-call wall-clock budget is the orchestrator's `--timeout` (default 90s). Remote sandboxes get `--timeout` plus a 60s buffer for everything outside the agent run (cold-start, setup, verify, teardown, workdir extraction).

`--model` is appended only when `cultivar run --model` is set; without it the CLI uses whatever the account's headless default is. Pin it for any comparison run — an unpinned default that shifts between batches reads as a behavioural finding when it's really just a different model.

Two unrelated flags share the name: `cultivar run --model` picks the **agent** model (this one, Claude-only — the other runners accept and ignore it), while `cultivar grade --model` picks the **grader** model (see [grader.md](../grader.md)).

`--allowedTools` takes a **single comma-joined value** (e.g. `Bash,Read,Write,Edit`). Passing tools as separate argv tokens silently drops everything after the first.

## Variants

| | with-skill | without-skill | with-docs |
|---|---|---|---|
| Prompt | `Use the /<skill> skill. <intent>` | `<intent>` | `<docs_context><intent>` |
| `--allowedTools` | `Bash,Read,Write,Edit,Skill,ToolSearch` | `Bash,Read,Write,Edit` | `Bash,Read,Write,Edit` |

The baseline variants drop `Skill` and `ToolSearch` from `--allowedTools` — there's no mounted skill to invoke, so the tools have nothing to act on. This is the one flag that differs between with-skill and the baselines; everything else (model, turn cap, timeout, output format) is identical.

A task's [`extra_tools`](../task-yaml.md) list is unioned into whichever variant's allow-list applies, de-duplicated. That's how a `without-skill` baseline gets `WebSearch`/`WebFetch` — the table's values are the floor, not a fixed set. It's a per-task opt-in rather than a global default, since most tasks don't want the extra surface.

The with-docs prompt is just `f"{docs_context}{intent}"` — the runner concatenates, nothing more. The `docs_context` prefix is built by `load_runner_refs` (`evals/framework/grader.py`), which wraps the `context_refs` in a framing preamble ("Reference these documents…") and appends a `\n---\n\n` divider before the intent. So the divider comes from the grader's ref-loader, not the runner.

## The `--bare` caveat

No variant passes Claude's `--bare` flag. It strips `Write` regardless of `--allowedTools`, silently breaking code-gen tasks (empty workdir → auto-fail). The baselines don't need it anyway: without a `Use the /<skill>` prompt, the `Skill` tool stays unused even when allowed.

To add a true bare baseline later, make it a per-task opt-in — a `bare-baseline` variant or a `use_bare: true` flag, not a global default. See [claude.py](../../evals/runners/claude.py) for the rationale.

## Skill discovery

Claude Code discovers skills from `.claude/skills/`, so the `/<skill>` invocation works without any link step. The framework mounts the skill-under-test at `.claude/skills/<skill>/` under the run's working directory (`/workspace/...` in a Modal sandbox), for the with-skill variant only.

## Quirks

- Output is stream-json (JSONL). The runner parses the `type=result` event for stats and the `type=system` / `subtype=init` event for `session_id`. On timeout the `result` event never arrives, so `session_id` falls back to the value captured from the `init` event.
- The captured `session_id` lets you resume a run interactively: `claude --resume <session_id>`.
- `--allowedTools` as separate argv tokens is a silent footgun — see "How it's invoked" above.

## Upstream docs

- [Claude Code docs](https://docs.claude.com/en/docs/claude-code)
- [Headless / `-p` usage](https://docs.claude.com/en/docs/claude-code/sdk)

## Sources

- [evals/runners/claude.py](../../evals/runners/claude.py) — runner implementation, `build_command` (`--allowedTools` / `--bare` rationale), stream-json parser
