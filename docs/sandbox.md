# Sandbox (Modal) — DIY guide

`cultivar run --remote` executes each (task, variant, repeat) in its own [Modal](https://modal.com) sandbox: isolated filesystem, no auth-state collisions across parallel runs, billed per-second of execution.

## Image

Built once per Modal workspace, cached after the first `--remote` invocation (subsequent cold starts ~5–10 s; first build ~3–5 min):

- `debian_slim` + Python 3.11 + Node 22
- `curl`, `git`, `jq`, `procps` (apt) + `pyyaml` (pip)
- `npm install -g @anthropic-ai/claude-code @google/gemini-cli @github/copilot`
- Runner code mounted from `evals/runners/` into `/workspace/evals/runners/`
- `evals/remote/entry.py` mounted into the sandbox to invoke the runner class

The skill being tested is mounted per-run at `/workspace/.claude/skills/<skill>/` (the with-skill variant only).

## Lifecycle

Per (task, variant, repeat), one sandbox:

| Phase | What runs | Where output goes |
|---|---|---|
| **create** | `modal.Sandbox.create(...)` with the image, secrets, `workdir=/workspace`, timeout | `sandbox_timing.create_s` |
| **setup** | `task.setup` shell command, if defined | `*.setup.log` (failure aborts the run) |
| **eval** | `entry.py` → real runner class → JSON result on stdout | `sandbox_timing.eval_s` |
| **verify** | `task.verify` shell command, if defined; stdout passed to grader | `*.verify.log`; stdout in `result.verify_output` |
| **workdir capture** | `find /workspace/app -type f` + per-file `sb.filesystem.read_bytes(path)` → local `write_bytes` (skips `__pycache__`, `.venv`, `node_modules`, `.git`) | `*.workdir/` |
| **teardown** | `task.teardown` shell command, if defined | `*.teardown.log` |
| **terminate** | `sb.terminate()` (in `finally`) | always runs |

The full set in `result["sandbox_timing"]` is `create_s`, `setup_s`, `eval_s`, `teardown_s`, `total_s`; `cultivar report` shows the first four. Note `teardown_s` is measured from the end of the agent run, so it also absorbs **verify** and **workdir capture** in addition to the teardown command.

## Hard timeout

Set with `--timeout <seconds>` (default `90`). That value is the **agent CLI budget** — the wall-clock cap on the subprocess inside the sandbox. The sandbox itself gets `timeout + 60s` so image cold-start, setup, verify, teardown, and workdir extraction don't eat into the agent's window. The 60s buffer is `SANDBOX_BUFFER_S` in [`evals/remote/modal_runner.py`](../evals/remote/modal_runner.py); per-task overrides are not yet supported.

## What's controllable today

| Knob | How |
|---|---|
| Skill mounted | `--skill <name>`, `--skills-dir <path>` |
| Setup / verify / teardown | per-task in YAML (run inside the sandbox) |
| Required env vars (preflight, locally) | per-task `env: [...]` |
| Sandbox env / secrets | a Modal secret named `eval-sandbox-secrets` by default; override with `CULTIVAR_MODAL_SECRET` env var |
| Modal app name | `cultivar` by default; override with `CULTIVAR_MODAL_APP` env var |
| Parallelism | `--parallel N` (default 5) |
| Repeats | `--repeat N` |
| Model | `--model <name>` (Claude runner only; Copilot/Gemini accept and ignore it) |
| Per-call timeout | `--timeout <seconds>` (default 90; sandbox gets +60s buffer) |



## DIY workspace setup

Use this if you don't have access to a shared workspace.

```bash
# 1. Account + token
# Sign up at https://modal.com, then:
pip install modal
modal token new

# 2. Create the secret the sandbox expects
modal secret create eval-sandbox-secrets \
  ANTHROPIC_API_KEY=sk-ant-... \
  # plus any other keys your tasks need (PINECONE_API_KEY, GEMINI_API_KEY,
  # COPILOT_GITHUB_TOKEN, etc.)

# 3. Verify
modal secret list   # eval-sandbox-secrets should appear
modal token current # should show your username + workspace

# 4. First remote run (image builds; takes ~3–5 min)
cultivar run --skill workdir-smoke --runner claude --remote
```

The secret is **named** `eval-sandbox-secrets` by default. To use a different name (e.g. when sharing the tool across orgs), set `CULTIVAR_MODAL_SECRET=my-secret-name` in your environment — `modal_runner.py` reads it on import.

If you belong to multiple Modal workspaces, switch with `modal profile activate <name>` or set `MODAL_PROFILE` for a single command.

## Passing per-run env into sandboxes

The named secret is shared by everyone in the workspace and is the same for every run. Credentials that only exist on the machine starting the run, such as a short-lived CI token, can't go there. List them in `CULTIVAR_SANDBOX_ENV_PASSTHROUGH` instead (comma-separated variable names). For each sandbox, cultivar copies the named variables that are set in its own environment into a Modal secret built just for that sandbox, mounted next to the named one. Names that are unset or empty are skipped. Values are never logged. Unset, nothing extra is mounted.

`ANTHROPIC_IDENTITY_TOKEN_FILE` is the one special case: its path exists only on your machine, so cultivar reads the file and passes the contents as `ANTHROPIC_IDENTITY_TOKEN`. The file is read again for every sandbox, so a token refreshed between runs reaches the next sandbox. If you pass both variables, the literal `ANTHROPIC_IDENTITY_TOKEN` wins.

### Claude with workload identity federation from GitHub Actions

Workload identity federation lets a GitHub Actions job authenticate to Anthropic with the short-lived OIDC token GitHub issues to that job, instead of a stored API key. The Claude CLI in the sandbox does the token exchange itself, so it needs the GitHub token and the federation IDs:

```yaml
permissions:
  id-token: write
  contents: read

steps:
  - name: Fetch GitHub OIDC token for Anthropic
    run: |
      curl -sSf -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
        "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://api.anthropic.com" \
        | jq -r .value > "$RUNNER_TEMP/anthropic-identity-token"
      echo "ANTHROPIC_IDENTITY_TOKEN_FILE=$RUNNER_TEMP/anthropic-identity-token" >> "$GITHUB_ENV"

  - name: Run evals
    env:
      ANTHROPIC_FEDERATION_RULE_ID: ${{ vars.ANTHROPIC_FEDERATION_RULE_ID }}
      ANTHROPIC_ORGANIZATION_ID: ${{ vars.ANTHROPIC_ORGANIZATION_ID }}
      ANTHROPIC_SERVICE_ACCOUNT_ID: ${{ vars.ANTHROPIC_SERVICE_ACCOUNT_ID }}  # if your rule needs it
      CULTIVAR_SANDBOX_ENV_PASSTHROUGH: >-
        ANTHROPIC_FEDERATION_RULE_ID,ANTHROPIC_ORGANIZATION_ID,
        ANTHROPIC_SERVICE_ACCOUNT_ID,ANTHROPIC_WORKSPACE_ID,ANTHROPIC_IDENTITY_TOKEN_FILE
    run: cultivar run --skill my-skill --runner claude --remote --grade
```

The same variables authenticate the grader, which runs on the runner and reads the token file directly.

GitHub OIDC tokens expire within minutes. Re-fetch the file before each long step, since every sandbox and every grader token exchange reads it fresh. Remove `ANTHROPIC_API_KEY` from the named Modal secret: a static key outranks federation, so a revoked key left there still gets used.

## Adding a CLI tool for a specific skill

The base image includes the three agent CLIs plus a few basics (`curl`, `git`, `jq`, `procps`). If your tasks need an
additional CLI (e.g. `pc`, `gh`, `aws`), install it in the task `setup` field — it runs
inside the sandbox before the agent starts:

```yaml
tasks:
  - id: my-task
    setup: |
      curl -fsSL -o /tmp/pc.tar.gz \
        https://github.com/pinecone-io/cli/releases/latest/download/pc_Linux_x86_64.tar.gz
      tar -xzf /tmp/pc.tar.gz -C /usr/local/bin && rm /tmp/pc.tar.gz
      chmod +x /usr/local/bin/pc
    intent: "list all my Pinecone indexes"
```

Setup runs once per sandbox (per task × variant × repeat). For CLIs used across many
tasks in a skill, put the install in every task's `setup` — or consider building a
custom image if the install time is significant.

## Joining a shared workspace

If a teammate has already set up an `eval-sandbox-secrets` for shared use, ask them to invite you (Modal dashboard → Settings → Members). Once accepted, run `modal profile activate <their-workspace>` and skip step 2 above.

## Debugging sandbox failures

| Symptom | Where to look |
|---|---|
| Image build fails | `modal app list` and dashboard logs for the build |
| Sandbox exits non-zero | `*.json` → `error` and `stderr` (capped at 2000 chars); also Modal dashboard → Sandboxes |
| Setup failed | `*.setup.log` |
| Agent silently produced no output | `*.stderr.log` (auth errors usually surface here) |
| Verify ran but grader got the wrong context | `*.verify.log` and `result.verify_output` |
| Workdir files missing | check the agent actually wrote into `cwd` (sandbox's `cwd` is `/workspace/app`); `find` step happens before teardown |
| Anthropic / provider auth errors inside sandbox | `eval-sandbox-secrets` is missing the key — `modal secret list` and edit |

For richer per-sandbox stdout/stderr + resource graphs, [Modal dashboard → Sandboxes](https://modal.com).

## Sources

- [evals/remote/modal_runner.py](../evals/remote/modal_runner.py) — image definition, `run_one_remote`, lifecycle phases, workdir capture
- [evals/remote/entry.py](../evals/remote/entry.py) — sandbox-side entry point that invokes the runner class
