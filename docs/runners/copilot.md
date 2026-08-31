# Copilot runner

Wraps [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli) (`copilot`) as a runner.

## Install

```bash
# See: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli
```

## Auth

Copilot CLI needs **either** `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`. For headless use, that token must be a **fine-grained PAT with the "Copilot Requests" permission**. Classic PATs and `gh auth login` sessions don't work for `-p` mode.

In Modal sandboxes the CLI is preinstalled in the image; the token is injected via `eval-sandbox-secrets`.

## How it's invoked

```bash
copilot --autopilot --yolo \
  --max-autopilot-continues <N> \
  --output-format json \
  --no-ask-user \
  [--no-custom-instructions --excluded-tools skill] \
  -p "<intent>"
```

`-p` must come last to avoid flag-parsing issues.

## Variants

| | with-skill | without-skill | with-docs |
|---|---|---|---|
| Prompt | `Use the /<skill> skill. <intent>` | `<intent>` | `<docs_context><intent>` |
| `--no-custom-instructions` | no | yes | yes |
| `--excluded-tools skill` | no | yes | yes |

**Fairness caveat.** `--no-custom-instructions` disables `AGENTS.md` loading on the bare variants; the with-skill variant doesn't pass it. That means cost/token deltas across variants reflect more than just the skill. We accept this asymmetry on Copilot because there's no equivalent of Claude's `Write`-stripping bug — Copilot's "bare" flags are tool/instruction-level and don't break code-gen. Revisit if you need a strict like-for-like cost comparison.

## Skill discovery

Copilot CLI discovers skills from `.claude/skills/` (same path as Claude Code), so the `/<skill>` invocation pattern works identically without any link step.

## Quirks

- Fine-grained PAT scope is non-obvious — classic PATs silently fail with auth errors.
- Event schema is nested under `data` with types like `assistant.message`, `assistant.message_delta`, `assistant.turn_end`, `tool.execution_start`, `tool.execution_complete`. Tool call IDs are matched between `execution_start` and `execution_complete` to attribute output.
- Internal events `report_intent` / `task_complete` are skipped from the conversation trace.

## Upstream docs

- [Install Copilot CLI](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli)
- [Headless usage](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli)

## Sources

- [evals/runners/copilot.py](../../evals/runners/copilot.py) — runner implementation, `build_command`, nested event parser
