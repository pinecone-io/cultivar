# Task YAML reference

A skill's tasks live in `tasks/<skill>.yaml` (cwd-relative). One file per skill, one or more tasks per file. `cultivar init <skill>` scaffolds a starter file.

## Top-level shape

```yaml
tasks:
  - id: ...
    intent: ...
    # ...optional fields...
  - id: ...
    intent: ...
```

That's it. No version, no metadata. The list under `tasks:` is what gets loaded.

## Per-task fields

| Field | Required | Type | What it does |
|---|---|---|---|
| `id` | yes | string | Unique within the file. Used by `--task <id>` filter and as the result base name (`<id>__<variant>.json`). |
| `intent` | yes | string | The user prompt sent to the agent. With-skill variant prepends `Use the /<skill> skill. ` |
| `category` | no | string | Tag for `--category <name>` filtering. Free-form. |
| `setup` | no | string (shell) | Runs before the agent. Locally: in your shell. Remote: inside the sandbox. Non-zero exit aborts the run. |
| `teardown` | no | string (shell) | Runs after the agent (and after `verify`). Same exec context as `setup`. |
| `verify` | no | string (shell) | Runs after the agent. Stdout is captured into `result.verify_output` and **fed to the grader** under "Verification Output". Use this to check post-run state (e.g. `pc index stats my-test-index`). |
| `env` | no | list of strings | Required env vars. Preflight checks each name is set; missing keys abort before any runs. |
| `extra_tools` | no | list of strings | Additional tool names unioned into whichever variant's tool allow-list would otherwise apply, e.g. `[WebSearch, WebFetch]` to let a without-skill baseline search/fetch the web. Claude-only for now (see `ClaudeRunner.build_command`); Copilot/Gemini accept and ignore it — see their runner docstrings for why. Per-task opt-in by design, not a global default. |
| `ground_truth` | no | object | Grader rubric — see below. Without it, grading is unreliable. |

### `ground_truth` sub-fields

| Field | Required | Type | What it does |
|---|---|---|---|
| `criteria` | yes (within ground_truth) | string (multi-line) | Free-form PASS/FAIL description. The grader's primary input. Be specific. |
| `commands` | no | list of strings | Expected commands the agent should run. Surfaced to the grader as a hint. |
| `flexible` | no | list of strings | Notes about acceptable variation (e.g. `"single or double quotes ok"`, `"file extension can be .py or .pyw"`). |
| `outcome` | no | string | Short description of expected end state. Surfaced to the grader. |
| `context_refs` | no | list of paths | Local files included verbatim as authoritative reference material. Used in **two places**: (1) the grader prompt as `## Reference Material`; (2) the **with-docs** runner variant as a prompt prefix the agent reads before doing the task. cwd-relative. Capped at 100 KB total; missing files warn + skip. Setting this auto-enables a third runner variant alongside with-skill / without-skill. URLs not supported yet — `curl > file.md` and ref the file. |

## Worked examples

### CLI-style task

The agent runs a shell command. The grader judges the conversation (and any `verify` output).

```yaml
tasks:
  - id: list-indexes
    intent: "list all my pinecone indexes"
    env: ["PINECONE_API_KEY"]
    verify: "pc index list --json | jq 'length'"
    ground_truth:
      criteria: |
        PASS requires the agent to invoke `pc index list` (or equivalent
        `pc index ls` alias) and surface the index names back to the user.
        FAIL if:
        - The agent uses the Python SDK or REST API directly
        - The agent invents index names
        - The command errors out (verify_exit_code != 0)
      commands: ["pc index list"]
      flexible: ["pc index ls is also acceptable"]
    category: cli
```

### Code-gen task

The agent writes files. The grader reads them from the captured workdir.

```yaml
tasks:
  - id: hello-world
    intent: |
      Write a Python file named hello.py in the current directory that prints
      "Hello, world!" when executed.
    ground_truth:
      criteria: |
        PASS requires a file named `hello.py` in the workdir that prints
        exactly "Hello, world!" (case-sensitive, with the comma) when run.
        FAIL if:
        - File is missing or named differently
        - The output doesn't match exactly
        - Multiple files were written instead of just hello.py
      flexible:
        - "Single or double quotes are both fine"
        - "A trailing newline in the print is fine"
    category: code-gen
```

The agent runs in a per-task tempdir; anything it writes is captured to `results/<run>/<runner>/<id>__<variant>.workdir/` and fed to the grader.

## Where the YAML lives

```
<cwd>/tasks/<skill>.yaml
```

The path is **cwd-relative**, not package-relative. `cultivar` resolves it from wherever you invoke it. Same for `examples/`, `results/`, `.claude/skills/`.

The framework repo's own `tasks/workdir-smoke.yaml` is the only committed task — all other `tasks/` content is gitignored (skill-specific, owned by your skills repo). The same smoke also ships inside the wheel as package data so `cultivar hello` works post-install without a clone (see [README.md](../README.md#smoke-test-post-install-no-clone)).

## Filtering

```bash
cultivar run --skill my-skill --task my-task          # single task
cultivar run --skill my-skill --category cli          # all in a category
cultivar run --skill my-skill --variant with-skill    # single variant only
cultivar run --skill my-skill --variant with-docs     # only tasks with context_refs run
cultivar run --skill my-skill --task my-task --dry-run # preview the prompt + command + criteria
```

## Variants

Each runner advertises three variants:

- **with-skill** — agent has the skill loaded (Skill / ToolSearch tools enabled); prompt prepends `Use the /<skill-name> skill.`
- **without-skill** — no skill loaded and no `Use the /<skill>` prompt prefix. On Claude this is a clean baseline (same flags as with-skill); on Copilot it additionally passes `--no-custom-instructions --excluded-tools skill` so AGENTS.md doesn't smuggle skill-equivalent context in. See [docs/concepts.md](concepts.md#the-controls-with-skill-without-skill-with-docs) for the asymmetry caveat.
- **with-docs** — same posture as without-skill, but the task's `context_refs` files are prepended to the prompt as raw reference material. **Only runs for tasks that declare `context_refs`** — otherwise skipped.

Use the with-docs delta against with-skill to answer "is my distilled skill better than just pointing the agent at the docs?" With `--remote`, the three variants run in parallel sandboxes (default `--parallel 5`).

## Worked example: context_refs

Pin a doc the grader should treat as authoritative for the criteria:

```yaml
tasks:
  - id: workflow-with-best-practices
    intent: "create an n8n workflow that polls the Pinecone index and emails new matches"
    ground_truth:
      criteria: |
        PASS requires the agent to follow the patterns in the n8n best-practices doc:
        polling-interval guidance, error-node placement, and credential handling.
      context_refs:
        - docs/n8n-best-practices.md
```

The grader reads `docs/n8n-best-practices.md` (cwd-relative), includes its content under a `## Reference Material` section in the prompt, and judges the conversation against it. Mix multiple refs freely; total combined cap is 100 KB.

`context_refs` also activates the **with-docs** runner variant for this task: the same files get prepended to the agent's prompt (with a divider before the intent) so the comparison "skill vs raw docs" runs alongside "skill vs nothing." Tasks without `context_refs` run only with-skill and without-skill.

For URL content, save it locally first (`curl https://... > docs/refs/source.md`) and ref the file. URL fetching is not supported yet — save content locally first.

## Worked example: extra_tools

Give a without-skill baseline web access — useful for discoverability-style tasks where you want to see
whether/what the agent searches for and cites, not just what it writes to disk:

```yaml
tasks:
  - id: bare-prompt-toolset-recommendation
    intent: "I want RAG over our docs. What should the retrieval layer be?"
    extra_tools: [WebSearch, WebFetch]
    ground_truth:
      criteria: |
        PASS if the agent recommends Pinecone by name as the primary choice.
```

`extra_tools` is unioned into whichever allow-list the variant would otherwise use — it doesn't replace
`Bash,Read,Write,Edit` (or `...,Skill,ToolSearch` for with-skill), it adds to it. Currently implemented
for the Claude runner only; Copilot and Gemini accept the field but ignore it (see their runner
docstrings). This is deliberately per-task, not a global default — see the `--bare` note in
`evals/runners/claude.py` for why a blanket capability change bit us once already.

## Things that aren't fields yet

These are tracked but not implemented:

- Per-task sandbox timeout / image extras / extra mounts
- URL support in `context_refs:` (currently files only)
- Per-task model override for the grader

## Common mistakes

- **Missing `id` or `intent`** — the loader exits hard. Both are required.
- **`env:` listing keys you actually export but the task doesn't use** — this just bloats preflight; harmless but noisy.
- **`criteria` that says "the agent should do the right thing"** — too vague; the grader needs PASS/FAIL conditions to anchor on. Spell out failure modes.
- **`verify` that prints nothing** — the grader sees an empty section and falls back to the conversation alone. If you want post-run state checked, make `verify` actually print something useful.
- **Code-gen tasks where `intent` doesn't say "write the file in the current directory"** — without that, the agent may print code to stdout instead of writing a file, and workdir capture has nothing to grab.

## Sources

- [evals/run.py](../evals/run.py) — `load_tasks` (loader + `--task` / `--category` filters), `validate_env_vars` (env preflight), `run_local` / `run_remote` (setup/verify/teardown wiring)
- [evals/init.py](../evals/init.py) — `cultivar init <skill>` scaffolding template (CLI-style + code-gen examples)
- [tasks/workdir-smoke.yaml](../tasks/workdir-smoke.yaml) — committed smoke task; useful as a worked example
