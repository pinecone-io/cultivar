# Gemini runner

Wraps Google's [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`) as a runner.

> **Note:** this runner is slated for deprecation. See the runner table in the [README](../../README.md#supported-agents).

## Install

```bash
npm install -g @google/gemini-cli
gemini auth   # OAuth, or set GEMINI_API_KEY
```

In Modal sandboxes the CLI is preinstalled in the image; auth comes from `GEMINI_API_KEY` in `eval-sandbox-secrets` (the default name, configurable via `CULTIVAR_MODAL_SECRET`).

## How it's invoked

```bash
gemini --approval-mode=yolo --output-format stream-json -p "<intent>"
```

No `--bare` or `--max-turns` flags exist on Gemini. The orchestrator's `--timeout` flag (default 90s) is the per-call wall-clock budget. Remote sandboxes get that budget plus a 60s buffer (`SANDBOX_BUFFER_S`) for everything outside the agent run: cold start, setup, verify, teardown, workdir extraction.

## Variants

| | with-skill | without-skill | with-docs |
|---|---|---|---|
| Prompt | `Use the /<skill> skill. <intent>` | `<intent>` | `<docs_context><intent>` |
| Skill linking | `gemini skills link <path>` (with `Y\n` piped to bypass interactive confirm) | none | none |
| Working dir | runner's `cwd` (or framework tempdir) | runner's `cwd` (or its own empty tempdir) | runner's `cwd` (or its own empty tempdir) |

For flat with-docs, the runner prepends `docs_context` to the intent. `load_runner_refs` in `evals/framework/grader.py` builds that prefix, including the framing preamble ("Reference these documents…") and a `\n---\n\n` divider.

If the orchestrator passes a `cwd` (the per-task tempdir), every variant uses it. If not, the bare variants make their own empty tempdir so workspace `GEMINI.md` and skills don't leak in. User-level skills under `~/.gemini/skills/` are still present, but without a `/<skill-name>` prompt the agent doesn't invoke them, which is an acceptable baseline.

## Quirks

- `gemini skills link` is interactive. We pipe `"Y\n"` with a 30s timeout. A timeout or non-zero exit is swallowed (skill may already be linked from a previous run). If the `gemini` CLI itself is missing, the run aborts immediately with a `gemini_cli_not_found` error.
- No turn-limit flag exists on the CLI, so cultivar accepts but ignores `--max-turns` for Gemini; it bounds the subprocess with `--timeout`.
- Event schema differs from Claude: looks for `type=message`, `tool_use`, `tool_result`, `result`, plus stats under `result.stats` with field names like `tool_calls`, `input_tokens`, `output_tokens`, `cached`.

## Upstream docs

- [Gemini CLI repo](https://github.com/google-gemini/gemini-cli)
- [Headless usage](https://geminicli.com/docs/cli/headless/)

## Sources

- [evals/runners/gemini.py](../../evals/runners/gemini.py) — runner implementation, `gemini skills link` handling, event parsing
