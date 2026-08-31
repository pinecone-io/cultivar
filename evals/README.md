# `evals/` — package internals

This is the Python package that backs the `cultivar` CLI. End-user docs live in [README.md](../README.md) (top-level) and the [docs/](../docs) tree. This file is a code-oriented orientation for anyone reading or editing the framework itself.

If you just want to *use* cultivar, you're in the wrong place. Start with [the top-level README](../README.md) and [docs/concepts.md](../docs/concepts.md).

## What's in here

```
evals/
  cli.py                 # Typer entry point; mounts subcommands. Loads .env from cwd.
  run.py                 # Orchestrator: run_local, run_remote, the --dry-run path
  hello.py               # `cultivar hello` — packaged smoke + preflight checks
  init.py                # `cultivar init <skill>` — scaffolds tasks/ + SKILL.md
  show.py                # `cultivar show <run>` — render a conversation trace
  runners/
    base.py              # Runner ABC + run_cli() subprocess helper
    claude.py            # Claude Code CLI wrapper
    copilot.py           # GitHub Copilot CLI wrapper
    gemini.py            # Google Gemini CLI wrapper
  framework/
    reporting.py         # Shared rich rendering, resolve_results_dir, console
    grader.py            # LLM grader: prompt assembly, autofail logic, calibration
    report.py            # `cultivar report` — reads grades.json, prints panels + summary
  remote/
    modal_runner.py      # Modal sandbox lifecycle (image, create, exec, terminate)
    entry.py             # Sandbox-side: imports the real runner, prints JSON
  _resources/smoke/      # Packaged hello-world task + skill, shipped in the wheel
```

`cli.py::app` is the Typer root. Subcommands are mounted via `app.command("init")(init_main)` etc. Each subcommand's `main` lives in its own module.

## The Runner contract

Every CLI wrapper subclasses `Runner` (in `runners/base.py`):

```python
class Runner(ABC):
    name: str

    @abstractmethod
    def variants(self) -> list[str]: ...  # ["with-skill", "without-skill", "with-docs"]

    @abstractmethod
    def build_command(
        self, intent: str, variant: str, max_turns: int = 10, docs_context: str = "",
        extra_tools: list[str] | None = None, model: str | None = None,
    ) -> tuple[list[str], str]: ...   # (argv, full_prompt) — used by --dry-run

    @abstractmethod
    def run(
        self, intent: str, variant: str, max_turns: int = 10,
        cwd: str | None = None, docs_context: str = "", timeout: int = 90,
        extra_tools: list[str] | None = None, model: str | None = None,
    ) -> dict: ...
```

`extra_tools` is the task YAML's `extra_tools:` opt-in, unioned into the variant's tool allow-list. `model` is `cultivar run --model <id>`, an orchestration-level override of the agent CLI's model. Runners with no matching mechanism (Copilot, Gemini) accept and ignore both.

`run()` returns a dict whose only required key is `conversation_md` (the readable trace). Optional but recommended: `raw_events`, `session_id`, `duration_ms`, `total_cost_usd`, `num_turns`, `usage`, `stderr`, `error`.

The orchestrator passes `cwd` so any files the agent writes can be captured to `results/<run>/<runner>/<base>.workdir/`. The runner is responsible for forwarding `cwd` to its subprocess.

To add a new runner, subclass `Runner`, register in `run.py::RUNNER_CLASSES`, and (for remote support) add to `REMOTE_RUNNERS` and ensure the CLI is installed in `remote/modal_runner.py::eval_image`. Per-runner specifics (flags, auth, quirks) live in [docs/runners/](../docs/runners/).

## Local vs remote: two orchestrators, one contract

Both `run_local` and `run_remote` (in `run.py`) iterate `(task × variant × repeat)` and call the same runner classes. The difference is where the subprocess lives.

**Local (`run_local`):**
- Each iteration runs in a fresh `tempfile.TemporaryDirectory()` as the agent's cwd.
- For `with-skill`, the skill is copied into `<tmpdir>/.claude/skills/<name>/` before the runner is called, so Claude Code / Copilot can discover it via walk-up.
- After the runner returns, non-noise contents of the tmpdir get copied to `<runner>/<base>.workdir/`. `.claude` is excluded: with-skill mounts the skill there and it isn't agent output. Without the exclusion the code-gen empty-workdir autofail (see below) wouldn't fire.

**Remote (`run_remote`):**
- Each iteration submits a `run_one_remote()` call to a `ThreadPoolExecutor` (max workers = `--parallel`, default 5).
- `modal_runner.py` creates a fresh `modal.Sandbox` per iteration with the image, the secret named by `CULTIVAR_MODAL_SECRET` (default: `eval-sandbox-secrets`), and a `+60s` buffer on top of the agent's `--timeout`.
- For `with-skill`, the skill is mounted into the image at `/workspace/.claude/skills/<name>/`; the agent's cwd is `/workspace/app/`.
- `entry.py` runs inside the sandbox, imports the same `Runner` class, calls `.run()`, prints JSON to stdout. The orchestrator parses that and pulls workdir files out via per-file `sb.filesystem.read_bytes(path)`.

`entry.py` keeps runner output identical between local and remote runs, without a second implementation of the runner.

See [docs/sandbox.md](../docs/sandbox.md) for the full lifecycle, image contents, and DIY workspace setup.

## The grader

`framework/grader.py` runs **locally only**. It never runs inside the sandbox, so the Anthropic key stays on the user's machine. For each conversation in a run dir, it:

1. Builds a prompt from: skill SKILL.md + task criteria + `context_refs` reference material + calibration examples + the conversation + verify output + any workdir files.
2. Sends to Claude Haiku (or `--model`).
3. Parses the JSON response into a grade `{pass, evidence, reasoning, suggestions, ...}`.

Two pre-API short-circuits avoid hallucinated grades:
- Empty/no-signal conversation → autofail before the call.
- `category: code-gen` with an empty workdir → autofail before the call.

Thinking-by-default models:
- Claude's "-5" generation (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-5`, including pinned `-YYYYMMDD` snapshots) thinks by default. The grader sends `thinking: {"type": "disabled"}` to turn it off.
- Fable 5 / Mythos 5 (also including pinned `-YYYYMMDD` snapshots) can't disable thinking at all. The grader omits the `thinking` param, sets a low effort, and doubles `--max-tokens` since thinking and the reply share the same budget.
- Older models (haiku-4-5, sonnet-4-6, the 4.x Opus/Sonnet line) already default to no thinking and need no special handling.

If the model truncates its JSON mid-evidence, `_salvage_truncated_grade()` regex-extracts the verdict so a real PASS doesn't become a fake FAIL. Default reply budget is `--max-tokens 4096`; raise it if truncation recurs.

If a single grading call raises, `_grade_conversation_safely()` records a FAIL grade for that conversation instead of aborting the rest of the run. Auth/permission errors are the exception: those crash the run immediately, since a bad or revoked key fails the same way on every remaining conversation.

Full prompt anatomy + calibration mechanics: [docs/grader.md](../docs/grader.md).

## Variants

Three of them: `with-skill`, `without-skill`, `with-docs`. The third auto-activates when a task declares `ground_truth.context_refs: [...]`; otherwise it's skipped. The same `context_refs` files are used in two places: the grader prompt (as authoritative reference) and the with-docs runner prompt (prepended to the intent). See [docs/concepts.md#the-controls-with-skill-without-skill-with-docs](../docs/concepts.md#the-controls-with-skill-without-skill-with-docs) and [docs/task-yaml.md#variants](../docs/task-yaml.md#variants).

## Path resolution

User-data paths (`tasks/`, `examples/`, `results/`, `.claude/skills/`) resolve relative to the current working directory, regardless of where the package is installed. `tasks/` and `results/` are module-level constants in `run.py`, `examples/` in `framework/grader.py`. The skills root comes from `resolve_skills_base()` in `framework/reporting.py`, which honors `--skills-dir` and `CULTIVAR_SKILLS_DIR`. Don't reintroduce `Path(__file__).parent`-based defaults for user data. This design lets a globally installed `cultivar` run from a skills repo without ever cloning this one.

## Test layout

`tests/test_core.py` covers loaders, env validation, save_result, grader prompt construction + autofail short-circuits, calibration filtering, workdir filtering, packaged smoke resources, and orchestrator call-surface guards. The last is an AST-based check that `hello.py`'s calls to `run_local`/`run_remote` still match their signatures, catching the bug class that hit us once.

```bash
uv run pytest -q       # ~0.6s
uv run ruff check .    # lint
uv run ty check        # types
```

## Source-of-truth pointers

| Topic | File |
|---|---|
| How to write a task YAML | [docs/task-yaml.md](../docs/task-yaml.md) |
| What the framework measures + why | [docs/concepts.md](../docs/concepts.md) |
| Grader prompt anatomy + calibration | [docs/grader.md](../docs/grader.md) |
| Modal sandbox setup + lifecycle | [docs/sandbox.md](../docs/sandbox.md) |
| Per-runner flag/auth/quirk details | [docs/runners/](../docs/runners/) |
| End-user CLI usage | [README.md](../README.md), `cultivar <cmd> --help` |
| Contributing | [CONTRIBUTING.md](../CONTRIBUTING.md) |
