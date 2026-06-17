# Gemini runner

Wraps Google's [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`) as a runner.

## Install

```bash
npm install -g @google/gemini-cli
gemini auth   # OAuth, or set GEMINI_API_KEY
```

In Modal sandboxes the CLI is preinstalled in the image; auth comes from `GEMINI_API_KEY` in `eval-sandbox-secrets` (or whatever the sandbox image sets up).

## How it's invoked

```bash
gemini --approval-mode=yolo --output-format stream-json -p "<intent>"
```

No `--bare` or `--max-turns` flags exist on Gemini. The orchestrator's `--timeout` flag (default 90s) is the per-call wall-clock budget.

## Variants

| | with-skill | without-skill | with-docs |
|---|---|---|---|
| Prompt | `Use the /<skill> skill. <intent>` | `<intent>` | `<context_refs>\n---\n<intent>` |
| Skill linking | `gemini skills link <path>` (with `Y\n` piped to bypass interactive confirm) | none | none |
| Working dir | runner's `cwd` (or framework tempdir) | runner's `cwd` (or its own empty tempdir) | runner's `cwd` (or its own empty tempdir) |

If the orchestrator passes a `cwd` (the per-task tempdir), every variant uses it. If not, the bare variants make their own empty tempdir so workspace `GEMINI.md` and skills don't leak in. User-level skills under `~/.gemini/skills/` are still present, but without a `/<skill-name>` prompt the agent doesn't invoke them, which is an acceptable baseline.

## Quirks

- `gemini skills link` is interactive — we pipe `"Y\n"` and a 30s timeout. Failures are swallowed (skill may already be linked from a previous run).
- No turn limit flag, so `--max-turns` translates to a subprocess timeout, not a hard cap on agent turns.
- Event schema differs from Claude: looks for `type=message`, `tool_use`, `tool_result`, `result`, plus stats under `result.stats` with field names like `tool_calls`, `input_tokens`, `output_tokens`, `cached`.

## Upstream docs

- [Gemini CLI repo](https://github.com/google-gemini/gemini-cli)
- [Headless usage](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/index.md)

## Sources

- [evals/runners/gemini.py](../../evals/runners/gemini.py) — runner implementation, `gemini skills link` handling, event parsing
