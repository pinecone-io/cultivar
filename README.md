# cultivar

A CLI tool to help you write tests for skills, test them across agents, and iterate until they work, from the Pinecone DevRel team.

Test how well skills work against tasks, across agents, locally and remotely. Customize sandboxes for how agents should start, and graders for how agents should work. 

Use traces to iteratively refine skills and optimize them against tasks.

Benchmark against skills, docs, and baselines. And, even run in parallel simulatenously for faster execution.

Want to use cultivar with an agent? Point it at this repo, or ask it to call --help to learn the tool!


## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- An Anthropic API key (the grader runs locally; agents in the sandbox use Modal-injected keys)
- A [Modal](https://modal.com) account if you want `--remote` runs (recommended for parallelism + isolation).**This is the recommended experience!**

## Install

```bash
uv tool install cultivar

# Or install from source:
uv tool install --from "git+https://github.com/pinecone-io/cultivar" cultivar
```

## Modal setup (one-time)

`--remote` runs each eval in an isolated [Modal](https://modal.com) sandbox — recommended for parallelism and clean auth state. Skip this section if you only need local runs.

```bash
# 1. Install Modal and authenticate
pip install modal
modal token new

# 2. Create the secret the sandbox reads at runtime
modal secret create eval-sandbox-secrets \
  ANTHROPIC_API_KEY=sk-ant-...
  # Add any keys your tasks need: GEMINI_API_KEY, COPILOT_GITHUB_TOKEN, etc.

# 3. Verify
modal secret list   # eval-sandbox-secrets should appear
```

The first `--remote` run builds the sandbox image (~3–5 min). Subsequent runs use the cached image (~5–10 s cold start).

**Defaults you can override via env var**:

| Env var | Default | What it controls |
|---|---|---|
| `CULTIVAR_MODAL_SECRET` | `eval-sandbox-secrets` | Name of the Modal secret mounted into each sandbox |
| `CULTIVAR_MODAL_APP` | `cultivar` | Modal app name (useful for isolating runs across teams or projects) |

For workspace sharing, custom images, and debugging sandbox failures, see [docs/sandbox.md](docs/sandbox.md).

## Quickstart: testing your own skill

**1. Set up your working directory**

```bash
mkdir ~/my-evals && cd ~/my-evals
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

**2. Scaffold a task file**

```bash
cultivar init my-skill
```

This writes `./tasks/my-skill.yaml` and `./.claude/skills/my-skill/SKILL.md`.

**3. Edit the skill** (`.claude/skills/my-skill/SKILL.md`)

The skill file is what the agent sees when you invoke `/my-skill`. Write it like a concise brief: what the skill does, when to use it, and the key commands or patterns it should follow. Keep it tight — a few focused sections outperform a wall of text. If you're not sure where to start, drop your existing docs or a rough draft into Claude and ask it to write a SKILL.md for you.

**4. Edit the tasks** (`tasks/my-skill.yaml`)

Each task has an `intent` (what you'd say to the agent) and a `criteria` block (what PASS looks like, in plain English). A good criteria block names 2–3 concrete things that must be true and at least one common failure mode. Agents are good at this too: share a few examples of passing and failing behavior and ask Claude to draft the criteria.

For the full YAML schema and field reference, see [docs/task-yaml.md](docs/task-yaml.md).

**5. Run + grade**

```bash
cultivar run --skill my-skill --runner claude --remote --grade
```

## Smoke test (post-install, no clone)

After `uv tool install`, verify the install works end-to-end with the packaged smoke:

```bash
cultivar hello                      # local: agent + grader (needs ANTHROPIC_API_KEY)
cultivar hello --remote             # also exercises Modal + eval-sandbox-secrets
cultivar hello --no-grade           # just exercise the runner (no API key needed)
```

`hello` runs a tiny "write hello.py" task that ships inside the wheel — no repo clone, no `tasks/` setup. It exits 0 on PASS and prints diagnostics on FAIL. Use this to learn how to use cultivar.


## Running remotely + inspecting results

```bash
# Single task, single variant
cultivar run --skill my-skill --runner claude --task my-task -v with-skill --remote

# All tasks + every applicable variant (with-skill, without-skill, and with-docs
# for tasks that declare context_refs)
cultivar run --skill my-skill --runner claude --remote

# 3 runs per (task, variant) for reliability, 5 sandboxes at once
cultivar run --skill my-skill --runner claude --remote --repeat 3 --parallel 5

# Raise the per-call wall-clock budget (default 90s; sandbox gets +60s buffer)
cultivar run --skill my-skill --runner claude --remote --timeout 180

# All three runners in parallel
cultivar run --skill my-skill --runner claude --remote &
cultivar run --skill my-skill --runner copilot --remote &
cultivar run --skill my-skill --runner gemini --remote &

# Run + grade in one shot
cultivar run --skill my-skill --runner claude --remote --grade

# Name a run so you can tell it apart later
cultivar run --skill my-skill --runner claude --remote --title baseline
cultivar run --skill my-skill --runner claude --remote --title after-tweak
```

**What you get per run** (`results/<timestamp>[__title]/`):

```
results/2026-04-22T11-31-47__baseline/
├── tasks.json                                 # task definitions used (for reproducibility)
├── notes.md                                   # --notes text, if any
├── grades.json                                # written by grader after `cultivar grade`
└── claude/                                    # one subdir per runner
    ├── my-task__with-skill.json               # structured result + stats (tokens, cost, timing, session_id)
    ├── my-task__with-skill.md                 # readable conversation trace
    ├── my-task__with-skill.jsonl              # raw event stream from the agent CLI
    ├── my-task__with-skill.stderr.log         # captured stderr (if any)
    ├── my-task__with-skill.setup.log          # setup/verify/teardown outputs (if those hooks ran)
    ├── my-task__with-skill.verify.log
    ├── my-task__with-skill.teardown.log
    └── my-task__with-skill.workdir/           # any files the agent wrote (code-gen tasks)
        └── hello.py
```

With `--repeat N`, files get a `__1` / `__2` / `__N` suffix. Without `--title`, the dir is just `<timestamp>/`.

**Inspecting what actually happened:**

| What | Where to look |
|---|---|
| One run, all sections (conversation, stats, workdir, grader) | `cultivar show latest -r claude -t <task>` |
| Just the conversation transcript for one run | `cultivar show latest -t <task> --conversation-only` |
| Just the grader verdict + reasoning + suggestions | `cultivar show latest -t <task> --grader` |
| Just the workdir file listing | `cultivar show latest -t <task> --workdir` |
| Summary table across all runners + variants | `cultivar report` |
| Human-readable conversation file | `*.md` |
| Raw stream-json events (Claude) / JSON lines (Copilot, Gemini) | `*.jsonl` |
| Stats (duration, tokens, cost, session id, sandbox timing) | `*.json` under `usage` / `total_cost_usd` / `sandbox_timing` |
| Grader verdict + evidence + reasoning + suggestions | `grades.json` or `cultivar report latest` |
| Why setup/verify/teardown failed | `*.setup.log` / `*.verify.log` / `*.teardown.log` |
| What the agent actually wrote to disk | `*.workdir/` |
| Resume a Claude session interactively to poke at it | `claude --resume <session_id>` (in the panel footer of `report`, or via `show … --grader`) |
| Live sandbox state / per-sandbox logs (remote only) | [Modal dashboard](https://modal.com/) → Sandboxes — each has stdout/stderr + resource graphs |
| Phase-by-phase sandbox timing (create / setup / eval / teardown) | `sandbox_timing` field in `*.json`, also printed in `cultivar report` |

**Quick debugging recipes:**

```bash
# Read one run end-to-end (replaces jq/less incantations)
cultivar show latest -r claude -t my-task

# Just the grader's verdict + remediation suggestions on a failure
cultivar show latest -t my-task --grader

# Pipe-friendly conversation transcript (ASCII fallback when not a TTY)
cultivar show latest -t my-task --conversation-only > convo.txt

# Full summary table for the latest run (no regrading)
cultivar report

# Regrade after editing criteria or adding calibration examples
cultivar grade --report

# Drop down to raw artifacts when needed
jq . results/<run>/claude/my-task__with-skill.jsonl | less
ls results/<run>/claude/my-task__with-skill.workdir/

# Resume a Claude session interactively
claude --resume $(jq -r .session_id results/<run>/claude/my-task__with-skill.json)
```

## Handing off to a coworker

Want to use cultivar with a team, but don't want to make everyone have different Modal workspaces?

Easiest path (assumes you have a Modal workspace set up):

1. **Invite them to the Modal workspace** (Modal dashboard → Settings → Members). They inherit the `eval-sandbox-secrets` secret group, so they don't need to set up their own Anthropic/Pinecone/etc. keys for remote runs.
2. **On their machine:**
   ```bash
   uv tool install cultivar
   modal token new                     # personal Modal token
   modal profile activate <workspace>  # if they belong to multiple workspaces
   echo "ANTHROPIC_API_KEY=sk-ant-..." > .env   # auto-loaded from cwd
   cultivar init my-skill            # scaffolds tasks/my-skill.yaml
   cultivar run --skill my-skill --runner claude --remote --grade
   ```
3. Billing accrues to your Modal account regardless of who runs what — set an expected budget if needed.

The only key your coworkers personally need is `ANTHROPIC_API_KEY` (the grader runs locally). For local (non-remote) agent runs they also need whatever the relevant agent CLI requires (Claude OAuths via `claude` on first run; Copilot needs `COPILOT_GITHUB_TOKEN` with the "Copilot Requests" fine-grained PAT scope; Gemini needs `GEMINI_API_KEY`).



## Supported Agents

We're always interested in adding more agents. If you have one that's not here, please let us know by opening an Issue!

| Runner | CLI | Headless flag | How without-skill is isolated | Per-runner doc |
|---|---|---|---|---|
| Claude | `claude` | `-p` | `--allowedTools` trimmed; no `Use the /<skill>` prefix in the prompt | [docs/runners/claude.md](docs/runners/claude.md) |
| Copilot | `copilot` | `-p --autopilot --yolo` | `--no-custom-instructions --excluded-tools skill` | [docs/runners/copilot.md](docs/runners/copilot.md) |
| Gemini (soon to be deprecated) | `gemini` | `-p --approval-mode=yolo` | temp-dir isolation (no flag) | [docs/runners/gemini.md](docs/runners/gemini.md) |

Each runner advertises three variants:

- `with-skill` — skill loaded, agent invoked via `/<skill-name>`
- `without-skill` — same agent, no skill loaded and no `Use the /<skill>` prefix in the prompt
- `with-docs` — same as without-skill, but the task's `context_refs` files are prepended to the prompt as raw reference material. Only runs for tasks that declare `context_refs`.

Two deltas to read:

| Comparison | Question it answers |
|---|---|
| with-skill vs without-skill | Is the skill doing anything at all? |
| with-skill vs with-docs | Is my distilled skill better than just dumping the docs into the prompt? |

With `--remote`, each `(task, variant, repeat)` runs in its own Modal sandbox in parallel — three variants on one task means three sandboxes, run concurrently up to `--parallel N` (default 5). Apples-to-apples baseline; same image, only the prompt + skill mounting differ. See [docs/concepts.md](docs/concepts.md#the-controls-with-skill-without-skill-with-docs) for the full discussion and [docs/task-yaml.md](docs/task-yaml.md#variants) for how to add `context_refs` to a task.

## Docs

- [docs/concepts.md](docs/concepts.md) — **start here** if you're new: what cultivar measures, why, and how to read the results
- [docs/task-yaml.md](docs/task-yaml.md) — task YAML schema, every field, worked examples
- [docs/grader.md](docs/grader.md) — how grading works, calibration examples, the prompt anatomy
- [docs/sandbox.md](docs/sandbox.md) — Modal sandbox setup (DIY), lifecycle, what's controllable
- [docs/runners/claude.md](docs/runners/claude.md), [gemini.md](docs/runners/gemini.md), [copilot.md](docs/runners/copilot.md) — per-runner specifics

For any subcommand: `cultivar <cmd> --help`.
