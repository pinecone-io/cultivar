# Sandbox (Modal) — DIY guide

`skill-eval run --remote` executes each (task, variant, repeat) in its own [Modal](https://modal.com) sandbox: isolated filesystem, no auth-state collisions across parallel runs, billed per-second of execution.

## Image

Built once per Modal workspace, cached after the first `--remote` invocation (subsequent cold starts ~5–10 s; first build ~3–5 min):

- `debian_slim` + Python 3.11 + Node 22
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
| **workdir capture** | `find /workspace/app -type f` + per-file `sb.open(path, "rb").read()` → local `write_bytes` | `*.workdir/` |
| **teardown** | `task.teardown` shell command, if defined | `*.teardown.log` |
| **terminate** | `sb.terminate()` (in `finally`) | always runs |

Each phase is timed; per-phase splits live in `result["sandbox_timing"]` and show in `skill-eval report`.

## Hard timeout

Set with `--timeout <seconds>` (default `90`). That value is the **agent CLI budget** — the wall-clock cap on the subprocess inside the sandbox. The sandbox itself gets `timeout + 60s` so image cold-start, setup, verify, teardown, and workdir extraction don't eat into the agent's window. The 60s buffer is `SANDBOX_BUFFER_S` in [`evals/remote/modal_runner.py`](../evals/remote/modal_runner.py); per-task overrides are not yet supported.

## What's controllable today

| Knob | How |
|---|---|
| Skill mounted | `--skill <name>`, `--skills-dir <path>` |
| Setup / verify / teardown | per-task in YAML (run inside the sandbox) |
| Required env vars (preflight, locally) | per-task `env: [...]` |
| Sandbox env / secrets | a Modal secret named `eval-sandbox-secrets` by default; override with `SKILL_EVAL_MODAL_SECRET` env var |
| Modal app name | `skill-evals` by default; override with `SKILL_EVAL_MODAL_APP` env var |
| Parallelism | `--parallel N` (default 5) |
| Repeats | `--repeat N` |
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
skill-eval run --skill workdir-smoke --runner claude --remote
```

The secret is **named** `eval-sandbox-secrets` by default. To use a different name (e.g. when sharing the tool across orgs), set `SKILL_EVAL_MODAL_SECRET=my-secret-name` in your environment — `modal_runner.py` reads it on import.

If you belong to multiple Modal workspaces, switch with `modal profile activate <name>` or set `MODAL_PROFILE` for a single command.

## Adding a CLI tool for a specific skill

The base image includes the three agent CLIs and nothing else. If your tasks need an
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
