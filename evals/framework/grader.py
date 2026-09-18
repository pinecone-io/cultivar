"""Grade eval results using Claude API with optional calibration examples."""

import json
import os
import re
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import typer
import yaml
from anthropic import Anthropic, AuthenticationError, PermissionDeniedError
from anthropic.types import RedactedThinkingBlock, TextBlock, ThinkingBlock

from evals.framework.reporting import (
    console,
    gate_verdict,
    print_report,
    resolve_results_dir,
    resolve_skills_base,
)


def _require_anthropic_key() -> None:
    """Fail fast with a clear message if ANTHROPIC_API_KEY isn't set.

    The claude backend calls the Anthropic API directly; without the key it throws
    a raw SDK traceback, which is confusing for new users. Called from any entry
    point that will reach the Anthropic client.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red]\n"
            "The grader needs this to call the Anthropic API.\n\n"
            "Set it one of these ways:\n"
            "  - Add [bold]ANTHROPIC_API_KEY=sk-ant-...[/bold] to a .env file "
            "in your current directory (it's auto-loaded)\n"
            "  - [bold]export ANTHROPIC_API_KEY=sk-ant-...[/bold] in your shell"
        )
        raise typer.Exit(1)


EXAMPLES_DIR = Path.cwd() / "examples"

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

BACKEND_CLAUDE = "claude"
BACKEND_TYPESAFE = "typesafe"
BACKENDS = (BACKEND_CLAUDE, BACKEND_TYPESAFE)


def resolve_task_backend(task: dict, default: str) -> str:
    """The backend for one task: its own `grader_backend` pin, else the run default.

    A pin wins over the CLI flag because it records a property of the task --
    e.g. a debugging-oriented task whose criteria are only useful with quoted
    evidence -- whereas `--backend` is a run-wide default. An unknown value
    falls back to the default rather than aborting a long run over a typo in
    one task.
    """
    pinned = task.get("grader_backend")
    if not pinned:
        return default
    if pinned not in BACKENDS:
        console.print(
            f"[yellow]Task {task.get('id', '?')!r} pins unknown grader_backend {pinned!r}; "
            f"using {default!r}. Valid values: {', '.join(BACKENDS)}.[/yellow]"
        )
        return default
    return pinned


def _auth_error_types(backend: str) -> tuple[type[BaseException], ...]:
    """Exceptions that should abort the whole grading batch for `backend`.

    A bad key fails identically on every remaining conversation, so these are
    re-raised rather than turned into a wall of identical FAIL grades. The set
    is per-backend because the two SDKs raise unrelated classes.
    """
    if backend == BACKEND_TYPESAFE:
        from evals.framework.typesafe_grader import auth_error_types

        return auth_error_types()
    return (AuthenticationError, PermissionDeniedError)


GRADER_MAX_TOKENS = 4096

# Conversation traces are truncated to this many characters before grading.
# Shared with the TypeSafe backend so both judge the same slice of a long run.
GRADER_CONV_CAP = 50000

# Claude Fable 5 / Mythos 5 think on every request and reject an explicit
# `thinking: {"type": "disabled"}` at any effort level -- the grader must not
# send a `thinking` param to these at all. The optional `-YYYYMMDD` group
# matches a pinned dated snapshot (e.g. "claude-fable-5-20260315") the same
# way as the bare alias.
_ALWAYS_THINKS = re.compile(r"^claude-(fable|mythos)-\d+(-\d{8})?$")

# The "-5" model generation (Opus 5, Sonnet 5, Haiku 5, ...) thinks by default
# but can be told not to. The family name is an explicit allowlist rather
# than a loose `[a-z]+`, which would also match model ids from unrelated
# families with the same shallow `family-digit` shape (e.g. "claude-instant-1")
# that never had a thinking concept at all. Matches a bare generation number
# or one pinned to a `-YYYYMMDD` dated snapshot, and not the dotted/multi-part
# ids (`claude-haiku-4-5`, `claude-opus-4-6`, ...), which already default to
# no thinking and need no extra kwargs here.
_THINKS_BY_DEFAULT = re.compile(r"^claude-(opus|sonnet|haiku)-\d+(-\d{8})?$")


def _grader_request_kwargs(model: str, base_max_tokens: int = GRADER_MAX_TOKENS) -> dict:
    """All extra `messages.create` kwargs for a grader call, keyed by model alone.

    This is the single place that decides how a thinking-by-default Claude
    model is handled -- `max_tokens` and the `thinking`/`output_config` kwargs
    used to be decided in two separate places (this classification, and a
    second one at the call site), which could silently desync if either was
    edited without updating the other. The grader's job is a plain pass/fail
    classification -- it never needs extended thinking, and `grade_one`
    assumes the reply is a JSON text block. Older models (haiku-4-5,
    sonnet-4-6, the 4.x Opus/Sonnet line) already run with thinking off by
    default, so they get `base_max_tokens` unchanged. The "-5" generation
    thinks by default, so explicitly turn it back off. Fable 5 / Mythos 5
    can't turn thinking off at all -- omit `thinking` for those, keep it
    shallow with a low effort, and double `base_max_tokens` since thinking
    and the reply share it (see `grade_one`'s response parsing, which scans
    past leading thinking blocks instead of assuming the reply is
    `content[0]`).
    """
    if _ALWAYS_THINKS.match(model):
        return {"max_tokens": base_max_tokens * 2, "output_config": {"effort": "low"}}
    if _THINKS_BY_DEFAULT.match(model):
        return {"max_tokens": base_max_tokens, "thinking": {"type": "disabled"}}
    return {"max_tokens": base_max_tokens}


_AGENT_SIGNALS = (
    "**Assistant:**",
    "**Final result:**",
    "**Bash:**",
    "**Write:**",
    "**Edit:**",
    "**write_file:**",
    "**Read:**",
    "**Skill invoked:**",
    "**Skill activated:**",
    "**ToolSearch:**",
    "**Output:**",
)


def _has_agent_signal(md: str) -> bool:
    """True if the trace contains any agent turn or tool call we'd grade."""
    return any(sig in md for sig in _AGENT_SIGNALS)


WORKDIR_INCLUDE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh"}
WORKDIR_INCLUDE_NAMES = {"Dockerfile", "Makefile", "requirements.txt", "pyproject.toml", "package.json", ".env.example"}
WORKDIR_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", ".git", "dist", "build"}
WORKDIR_CAP_BYTES = 40_000
CONTEXT_REFS_CAP_BYTES = 100_000

_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "bash",
}


def load_workdir_files(workdir: Path) -> str:
    """Render filtered workdir contents as a grader-prompt section.

    Walks the directory recursively, applies an extension allowlist, skips
    common noise directories and lock files, and concatenates contents into a
    single markdown block capped at WORKDIR_CAP_BYTES. Returns empty string if
    the workdir is missing or contributes no eligible files.
    """
    if not workdir.is_dir():
        return ""

    eligible = []
    for p in sorted(workdir.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(workdir).parts
        if any(part in WORKDIR_SKIP_DIRS for part in rel_parts):
            continue
        if p.name.endswith(".lock"):
            continue
        if p.suffix in WORKDIR_INCLUDE_EXTS or p.name in WORKDIR_INCLUDE_NAMES:
            eligible.append(p)

    parts = []
    remaining = WORKDIR_CAP_BYTES
    skipped = 0
    for p in eligible:
        try:
            content = p.read_text(errors="replace")
        except Exception:
            continue
        rel = p.relative_to(workdir)
        lang = _LANG_BY_EXT.get(p.suffix, "")
        header = f"\n### {rel}\n```{lang}\n"
        footer = "\n```\n"
        budget = remaining - len(header) - len(footer)
        if budget <= 0:
            skipped += 1
            continue
        if len(content) > budget:
            content = content[:budget] + "\n... (file truncated)"
            skipped += 1
        parts.append(header + content + footer)
        remaining -= len(header) + len(content) + len(footer)

    if not parts:
        return ""

    out = (
        "## Generated Code Files\n"
        "The agent wrote these files to its working directory. "
        "Evaluate them against the criteria above.\n" + "".join(parts)
    )
    if skipped:
        out += f"\n... ({skipped} file(s) truncated or omitted to stay under {WORKDIR_CAP_BYTES} byte cap)\n"
    return out


def _resolve_refs_body(refs: list[str], cap: int = CONTEXT_REFS_CAP_BYTES) -> tuple[str, int]:
    """Read each ref, render as fenced markdown sections, capped at `cap` bytes total.

    Returns (body, skipped_capped). `body` is the concatenated content with no
    top-level section header — callers add their own framing. Missing files are
    skipped with a warning so a typo doesn't abort the caller. Empty string for
    no eligible content.
    """
    if not refs:
        return "", 0

    parts = []
    remaining = cap
    skipped_capped = 0

    for ref in refs:
        p = Path(ref)
        if not p.is_absolute():
            p = Path.cwd() / ref
        if not p.is_file():
            console.print(f"[yellow]Warning: context_ref not found, skipping: {ref}[/yellow]")
            continue
        try:
            content = p.read_text(errors="replace")
        except Exception as e:
            console.print(f"[yellow]Warning: could not read context_ref {ref}: {e}[/yellow]")
            continue
        lang = _LANG_BY_EXT.get(p.suffix, "")
        header = f"\n### {ref}\n```{lang}\n"
        footer = "\n```\n"
        budget = remaining - len(header) - len(footer)
        if budget <= 0:
            skipped_capped += 1
            continue
        if len(content) > budget:
            content = content[:budget] + "\n... (file truncated)"
            skipped_capped += 1
        parts.append(header + content + footer)
        remaining -= len(header) + len(content) + len(footer)

    return "".join(parts), skipped_capped


def load_context_refs(refs: list[str]) -> str:
    """Render task-declared reference files as a grader-prompt section.

    Each entry in `refs` is a cwd-relative (or absolute) path. Content is
    concatenated under '## Reference Material' and capped at
    CONTEXT_REFS_CAP_BYTES total.
    """
    body, skipped = _resolve_refs_body(refs)
    if not body:
        return ""
    out = (
        "## Reference Material\n"
        "This is reference content the agent had access to — NOT the agent's output. "
        "Treat it as ground truth for what correct behavior looks like, but NEVER "
        "quote from this section as evidence. Evidence must come from sections that "
        "contain the actual run.\n" + body
    )
    if skipped:
        out += f"\n... ({skipped} file(s) truncated or omitted to stay under {CONTEXT_REFS_CAP_BYTES} byte cap)\n"
    return out


def load_runner_refs(refs: list[str]) -> str:
    """Render the same refs as a runner prompt prefix (agent-friendly framing).

    Used by the with-docs variant: gives the agent the same files the grader
    treats as authoritative, so we can compare a distilled skill against just
    pointing the agent at the docs. Empty string when refs is empty — caller
    should skip the with-docs variant in that case.
    """
    body, skipped = _resolve_refs_body(refs)
    if not body:
        return ""
    out = (
        "Reference these documents as you complete the task below. "
        "Treat them as the authoritative source for correct behavior; "
        "the task itself follows after the divider.\n" + body
    )
    if skipped:
        out += f"\n... ({skipped} file(s) truncated or omitted to stay under {CONTEXT_REFS_CAP_BYTES} byte cap)\n"
    out += "\n---\n\n"
    return out


app = typer.Typer(pretty_exceptions_enable=False)


def load_examples(skill: str, task_id: str | None = None) -> str:
    """Load calibration examples, optionally filtered to a specific task."""
    examples_path = EXAMPLES_DIR / skill
    if not examples_path.exists():
        return ""

    lines = []
    for label_dir in ["pass", "fail"]:
        d = examples_path / label_dir
        if not d.exists():
            continue
        for f in sorted(d.glob("*.yaml")):
            with open(f) as fh:
                ex = yaml.safe_load(fh)
            if not isinstance(ex, dict) or not all(k in ex for k in ("task_id", "agent_proposed", "reasoning")):
                console.print(f"[yellow]Warning: skipping malformed example {f.name}[/yellow]")
                continue
            if task_id and ex["task_id"] != task_id:
                continue
            tag = "PASS" if label_dir == "pass" else "FAIL"
            lines.append(f"### {tag} ({ex['task_id']}):")
            lines.append(f"Agent proposed: {ex['agent_proposed'].strip()}")
            reason_label = "Why this passes" if tag == "PASS" else "Why this fails"
            lines.append(f"{reason_label}: {ex['reasoning'].strip()}")
            lines.append("")

    if not lines:
        return ""

    return (
        "## Calibration Examples\n\n"
        "These are HISTORICAL examples from PRIOR evaluations of this skill, "
        "shown only to anchor your judgment of pass/fail thresholds. They are "
        "NOT the current agent's output. Never quote from this section as "
        "evidence — evidence must come only from '## Agent Conversation', "
        "'## Verification Output', or '## Generated Code Files'.\n\n" + "\n".join(lines)
    )


def build_grader_prompt(
    task: dict,
    conversation_json: str,
    examples_block: str,
    skill_content: str = "",
    verify_output: str = "",
    workdir_content: str = "",
    refs_content: str = "",
) -> str:
    gt = task.get("ground_truth", {})
    criteria = gt.get("criteria", "")
    commands = gt.get("commands", [])
    flexible = gt.get("flexible", [])
    outcome = gt.get("outcome", "")

    parts = [
        "You are evaluating whether an AI assistant correctly completed a task.",
        "",
    ]

    if skill_content:
        parts.extend(
            [
                "## Skill Reference",
                "This is reference content the agent had access to — NOT the agent's output. "
                "Use it to understand what correct behavior looks like, but NEVER quote from "
                "this section as evidence. Evidence must come from sections that contain the "
                "actual run.",
                "",
                skill_content,
                "",
            ]
        )

    parts.extend(
        [
            "## Criteria",
            criteria if criteria else "(no specific criteria provided)",
            "",
        ]
    )

    if commands:
        parts.append(f"Expected commands: {', '.join(commands)}")
    if flexible:
        parts.append(f"Flexibility notes: {'; '.join(flexible)}")
    if outcome:
        parts.append(f"Expected outcome: {outcome}")

    parts.append("")

    if refs_content:
        parts.extend([refs_content, ""])

    if examples_block:
        parts.append(examples_block)

    parts.extend(
        [
            "## Agent Conversation",
            conversation_json,
            "",
        ]
    )

    if verify_output:
        parts.extend(
            [
                "## Verification Output",
                "This command was run after the agent finished to check the actual state:",
                f"```\n{verify_output}\n```",
                "",
            ]
        )

    if workdir_content:
        parts.extend([workdir_content, ""])

    parts.extend(
        [
            "## Instructions",
            "Evaluate whether the agent successfully completed the task.",
            "If the task failed or only partially succeeded, populate `suggestions` with"
            " 1-3 actionable items — each a probable root cause paired with a concrete fix"
            " the user could apply (e.g. add a missing flag to the skill, tighten the task"
            " criteria, supply a setup command). Keep cause and fix to one short sentence each."
            " On a clean pass, return `suggestions: []`.",
            "Return ONLY valid JSON (no markdown fencing):",
            json.dumps(
                {
                    "pass": "true or false",
                    "proposed_command": "the command(s) the agent ran or proposed",
                    "evidence": "quote at most ~500 characters / 5-10 lines, ONLY from the Agent Conversation, Verification Output, or Generated Code Files sections — never from Skill Reference, Reference Material, or Calibration Examples (those are reference / historical content, NOT this run's output). Prefer citing line ranges or function names over reproducing whole blocks; long quotes truncate the JSON and lose the verdict.",
                    "reasoning": "1-2 sentences explaining why this passes or fails",
                    "suggestions": [
                        {"cause": "probable reason this failed", "fix": "concrete next step"},
                    ],
                },
                indent=2,
            ),
        ]
    )

    return "\n".join(parts)


# Markers that indicate the agent RUN itself failed (auth/API/quota/etc.) rather
# than a clean run that simply wrote nothing — used to give an accurate cause when
# a code-gen workdir is empty (see _pregrade_autofail).
_RUN_ERROR_MARKERS = (
    "invalid api key",
    "fix external api key",
    "authentication_error",
    "permission denied",
    "rate limit",
    "overloaded_error",
    "insufficient_quota",
    "credit balance is too low",
    "could not resolve",
)


def _detect_run_error(conversation: dict, conv_str: str) -> str:
    """Short description of an agent-run error if the trace shows one, else ""."""
    if conversation.get("is_error") or conversation.get("api_error_status"):
        result = (conversation.get("result") or "").strip()
        if result:
            return result[:200]
    low = conv_str.lower()
    for marker in _RUN_ERROR_MARKERS:
        if marker in low:
            for line in conv_str.splitlines():
                if marker in line.lower():
                    return line.strip().lstrip("*# ").strip()[:200]
            return marker
    return ""


def _pregrade_autofail(
    task: dict,
    conversation: dict,
    conv_str: str,
    workdir_content: str,
) -> dict[str, Any] | None:
    """The grade for a trace not worth sending to any model, or None to proceed.

    These checks are properties of the trace, not of the grading model, so
    every backend shares them — a code-gen run with an empty workdir would
    otherwise reach the model with criteria and reference material but no
    files, which is exactly how fabricated evidence gets reported as a pass.
    """
    # Autofail when there's no agent signal in the trace — calling the grader
    # in that case just produces hallucination from calibration examples or
    # the task criteria itself.
    if not conv_str.strip() or not _has_agent_signal(conv_str):
        return {
            "pass": False,
            "proposed_command": "",
            "evidence": "",
            "reasoning": "Agent produced no usable output — conversation trace has no assistant turns or tool calls. Not sent to grader.",
            "suggestions": [
                {
                    "cause": "Runner emitted no agent activity in the parsed trace.",
                    "fix": "Inspect <run>/<runner>/<base>.stderr.log and the raw .jsonl. If the CLI emitted events but the runner didn't fold them into the trace, the runner's parser is missing a code path.",
                }
            ],
        }

    # Autofail code-gen tasks that produced no files. A code-gen task whose
    # workdir is empty hasn't done its primary deliverable; the grader would
    # otherwise pattern-match against Skill Reference / Reference Material
    # and report fabricated code as evidence.
    if task.get("category") == "code-gen" and not workdir_content.strip():
        run_error = _detect_run_error(conversation, conv_str)
        if run_error:
            return {
                "pass": False,
                "proposed_command": "",
                "evidence": "",
                "reasoning": f"Code-gen task produced no files: the agent run errored ({run_error}). Not sent to grader.",
                "suggestions": [
                    {
                        "cause": f"The agent run failed before writing any files: {run_error}",
                        "fix": "Resolve the agent error shown in the conversation (e.g. fix the API key / auth in your Modal secret or local environment), then re-run. See <run>/<runner>/<base>.md and .stderr.log.",
                    }
                ],
            }
        return {
            "pass": False,
            "proposed_command": "",
            "evidence": "",
            "reasoning": "Code-gen task produced no files in the captured workdir. Not sent to grader.",
            "suggestions": [
                {
                    "cause": "Agent didn't write any files (workdir is empty after the run).",
                    "fix": "Verify Write/Edit are actually in the agent's tool list — `jq -r '.tools' <jsonl> | head -1` on the init event. If they're missing despite --allowedTools, the runner's flag parsing may be dropping them silently.",
                }
            ],
        }

    return None


def grade_one(
    client: Anthropic,
    model: str,
    task: dict,
    conversation: dict,
    examples_block: str,
    skill_content: str = "",
    workdir_content: str = "",
    max_tokens: int = GRADER_MAX_TOKENS,
) -> dict[str, Any]:
    conv_str = conversation.get("conversation_md", "")

    auto = _pregrade_autofail(task, conversation, conv_str, workdir_content)
    if auto is not None:
        return auto

    if len(conv_str) > GRADER_CONV_CAP:
        conv_str = conv_str[:GRADER_CONV_CAP] + "\n... (truncated)"

    verify_output = conversation.get("verify_output", "")
    refs_content = load_context_refs(task.get("ground_truth", {}).get("context_refs", []))
    prompt = build_grader_prompt(
        task, conv_str, examples_block, skill_content, verify_output, workdir_content, refs_content
    )

    response = client.messages.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        **_grader_request_kwargs(model, max_tokens),
    )

    # The SDK's content blocks are a discriminated union; only TextBlock has
    # .text. Grader prompts don't use tools, but models that think by default
    # (and can't be told not to, e.g. Fable 5 / Mythos 5) legitimately prepend
    # one or more thinking blocks — tolerate those specifically instead of
    # assuming content[0] is the reply, but still fail loudly on anything else
    # (e.g. a tool_use block, which would mean something is misconfigured).
    text_block = None
    for block in response.content:
        if isinstance(block, TextBlock):
            text_block = block
            break
        if not isinstance(block, (ThinkingBlock, RedactedThinkingBlock)):
            raise RuntimeError(
                f"Grader response contained an unexpected {type(block).__name__} block. "
                "Did someone enable tool use on the grader call?"
            )
    if text_block is None:
        raise RuntimeError(
            "Grader response contained only thinking blocks, no text reply. The model "
            "likely spent its entire max_tokens budget thinking before producing any "
            "output — try raising --max-tokens or lowering the grading model's effort."
        )
    text = text_block.text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove opening fence (```json, ```, etc.)
        lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        grade = json.loads(text)
    except json.JSONDecodeError:
        grade = _salvage_truncated_grade(text)

    if isinstance(grade.get("pass"), str):
        grade["pass"] = grade["pass"].lower() == "true"

    grade["suggestions"] = _normalize_suggestions(grade.get("suggestions"))
    grade["grader_backend"] = "claude"
    grade["grader_model"] = model

    return grade


def _grade_conversation_safely(
    client: Any,
    model: str,
    task: dict,
    conversation: dict,
    examples_block: str,
    skill_content: str,
    workdir_content: str,
    label: str,
    max_tokens: int = GRADER_MAX_TOKENS,
    backend: str = BACKEND_CLAUDE,
) -> dict[str, Any]:
    """Call grade_one, turning any exception into a FAIL grade instead of propagating it.

    main()'s loop grades every conversation in a results dir before writing
    grades.json once at the end -- an uncaught exception from grade_one on
    one file would abort the whole run and discard every grade already
    computed for it. This used to be a low-risk assumption (grade_one's few
    ways to raise were essentially unreachable), but a thinking-by-default
    model can genuinely raise if it exhausts its budget on thinking before
    producing any text, so the call is now guarded.

    Auth/permission errors are deliberately excluded from that guard: a bad
    or revoked API key fails the same way on every remaining conversation, so
    swallowing it here would grind through the whole batch producing a wall
    of identical misleading FAIL grades instead of surfacing the real
    problem once, immediately -- the way the CLI already did before this
    wrapper existed.
    """
    abort_on = _auth_error_types(backend)
    try:
        if backend == BACKEND_TYPESAFE:
            from evals.framework.typesafe_grader import grade_one_typesafe

            return grade_one_typesafe(client, model, task, conversation, skill_content, workdir_content)
        return grade_one(client, model, task, conversation, examples_block, skill_content, workdir_content, max_tokens)
    except abort_on:
        raise
    except Exception as e:
        console.print(f"[red]Grader call failed for {label}: {e}[/red]")
        return {
            "pass": False,
            "proposed_command": "",
            "evidence": "",
            "reasoning": f"Grader call raised {type(e).__name__}: {e}",
            "suggestions": [
                {
                    "cause": f"grade_one() raised {type(e).__name__} instead of returning a grade.",
                    "fix": "Re-run `cultivar grade` for this results dir once the cause is fixed — "
                    "other conversations' grades in this run were not affected.",
                }
            ],
        }


def _salvage_truncated_grade(text: str) -> dict:
    """Best-effort field extraction when the grader's JSON is malformed/truncated.

    The verdict (`pass: true/false`) is almost always the first key the model
    emits, so a truncated response usually still has a recoverable PASS/FAIL
    signal even when the trailing `evidence` / `reasoning` strings are cut off
    mid-quote. Without this salvage, a real PASS becomes a fake FAIL — we hit
    exactly that in practice, where `"pass": true` was the very first key but
    the framework dropped the verdict because of an unclosed `evidence` string.
    """
    pass_match = re.search(r'"pass"\s*:\s*"?(true|false)"?', text, re.I)
    cmd_match = re.search(r'"proposed_command"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if pass_match:
        return {
            "pass": pass_match.group(1).lower() == "true",
            "proposed_command": cmd_match.group(1) if cmd_match else "",
            "evidence": "(grader response truncated; verdict salvaged from JSON header)",
            "reasoning": (
                "Grader response exceeded max_tokens or was malformed; recovered "
                "the pass/fail verdict from the start of the JSON. Evidence and "
                "reasoning fields lost — pass a higher --max-tokens or tighten the "
                "criteria's evidence cap if this recurs."
            ),
        }
    return {
        "pass": False,
        "proposed_command": "",
        "evidence": "",
        "reasoning": f"Grader returned invalid JSON and no 'pass' field could be salvaged: {text[:200]}",
    }


def _normalize_suggestions(raw) -> list[dict]:
    """Coerce grader suggestions into a list of {cause, fix} dicts.

    Tolerates the LLM returning a string, a list of strings, or omitting the
    field entirely — anything we can't shape into cause/fix is dropped.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        return [{"cause": "", "fix": raw.strip()}] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            cause = str(item.get("cause", "")).strip()
            fix = str(item.get("fix", "")).strip()
            if cause or fix:
                out.append({"cause": cause, "fix": fix})
        elif isinstance(item, str) and item.strip():
            out.append({"cause": "", "fix": item.strip()})
    return out


@app.command()
def main(
    results_dir: str = typer.Argument("latest", help="Results dir to grade. Use 'latest' for the most recent."),
    skill: str = typer.Option(
        "",
        help="Skill name for loading SKILL.md + calibration examples. Auto-detected from <results_dir>/tasks.json if omitted.",
    ),
    model: str = typer.Option(DEFAULT_MODEL, help=f"Anthropic model id for grading. Default: {DEFAULT_MODEL}."),
    backend: str = typer.Option(
        BACKEND_CLAUDE,
        "--backend",
        help=(
            "Grading backend: 'claude' (default, Anthropic API) or 'typesafe' "
            "(TypeSafe System One / Jev; needs TYPESAFE_API_KEY)."
        ),
    ),
    typesafe_model: str = typer.Option(
        "",
        "--typesafe-model",
        help="Model id for --backend typesafe. Default: jev-latest. Ignored by the claude backend.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "With --backend typesafe: print the per-section token breakdown of each grader "
            "state and flag any that exceed the model's state budget. No API calls, no grades.json. "
            "Exits 1 if any state is over budget, so it works as a CI preflight."
        ),
    ),
    max_tokens: int = typer.Option(
        GRADER_MAX_TOKENS,
        "--max-tokens",
        help=(
            "Max tokens for each grader reply. Doubled automatically for models that "
            f"can't disable thinking (e.g. claude-fable-5). Default: {GRADER_MAX_TOKENS}."
        ),
    ),
    report: bool = typer.Option(
        True, help="Print the report after grading. Use --no-report to only write grades.json."
    ),
    skills_dir: str = typer.Option(
        "", "--skills-dir", help="Skills root dir. Overrides CULTIVAR_SKILLS_DIR env; default ./.claude/skills."
    ),
    fail_under: float | None = typer.Option(
        None,
        "--fail-under",
        help=(
            "Exit 1 if the with-skill pass rate is below this percentage (0-100). "
            "Omit to always exit 0. Intended for CI gates."
        ),
    ),
    gate_variant: str = typer.Option(
        "with-skill", "--gate-variant", help="Variant that --fail-under measures. Default: with-skill."
    ),
):
    """Grade an existing results dir; writes grades.json and prints a report.

    Reads each runner conversation and grades it with one of two backends,
    writing <results_dir>/grades.json.

    claude (default): builds a text prompt (skill ref + criteria + calibration
    examples + conversation + verify output + workdir files) and calls the
    Anthropic API. Needs ANTHROPIC_API_KEY.

    typesafe: sends the same evidence as typed state to TypeSafe System One and
    composes the grade from its judgments. Calibration examples are not used,
    and the grade carries no quoted evidence or model-written reasoning. Needs
    TYPESAFE_API_KEY and the `cultivar[typesafe]` extra.

    Keys are auto-loaded from .env in cwd, and only the backends actually used
    are required. A task can pin its own backend with `grader_backend:` in the
    task YAML, which overrides --backend.

    Examples:
      cultivar grade latest                                  # regrade the most recent run
      cultivar grade results/2026-04-22T11-31-47__baseline   # grade a specific run
      cultivar grade latest --model claude-sonnet-4-6        # use a stronger grader
      cultivar grade latest --no-report                      # write grades.json, skip the report
      cultivar grade latest --fail-under 80                  # exit 1 if with-skill pass rate < 80%
      cultivar grade latest --max-tokens 8192                # give the grader more room per reply
      cultivar grade latest --backend typesafe               # grade with TypeSafe System One (Jev)
      cultivar grade latest --backend typesafe --typesafe-model jev-1.13.0
      cultivar grade latest --backend typesafe --dry-run     # token budget only; no calls, no grades.json

    See docs/grader.md for the backend comparison, prompt structure, and calibration tips.
    """
    if backend not in BACKENDS:
        console.print(f"[red]Unknown --backend '{backend}'. Choose one of: {', '.join(BACKENDS)}.[/red]")
        raise typer.Exit(1)

    results_path = resolve_results_dir(results_dir)
    console.print(f"[bold]Grading:[/bold] {results_path.name}")

    tasks_file = results_path / "tasks.json"
    if not tasks_file.exists():
        console.print(f"[red]No tasks.json in {results_path}[/red]")
        raise typer.Exit(1)

    with open(tasks_file) as f:
        tasks_data = json.load(f)

    # Auto-detect skill from tasks.json if not explicitly provided
    if not skill:
        skill = tasks_data.get("skill", "")
        if skill:
            console.print(f"[dim]Auto-detected skill: {skill}[/dim]")

    tasks_by_id = {t["id"]: t for t in tasks_data["tasks"]}
    # Count total examples available (per-task filtering happens at grading time)
    all_examples = load_examples(skill)
    if all_examples:
        n_examples = all_examples.count("###")
        console.print(f"[dim]{n_examples} calibration examples available[/dim]")
        if backend == BACKEND_TYPESAFE:
            console.print(
                f"[yellow]Note: the typesafe backend ignores all {n_examples} calibration "
                "example(s) — they anchor a text model's pass/fail threshold, whereas "
                "System One is steered by its own probability threshold. Verdicts here are "
                "NOT calibrated by those examples the way --backend claude's are.[/yellow]"
            )

    # Load SKILL.md for grader context
    base_dir = resolve_skills_base(skills_dir)
    skill_md = base_dir / skill / "SKILL.md"
    skill_content = ""
    if skill_md.exists():
        skill_content = skill_md.read_text()
        console.print(f"[dim]Loaded skill reference: {skill_md}[/dim]")
    else:
        # Silent before: SKILL.md is usually the largest grader input, so losing
        # it changes verdicts while everything still looks like it worked. The
        # usual cause is a skills dir that isn't the default.
        console.print(
            f"[yellow]No SKILL.md at {skill_md} — grading without the skill reference.[/yellow]\n"
            f"[yellow]Point at the right tree with --skills-dir or CULTIVAR_SKILLS_DIR "
            f"if that isn't what you want.[/yellow]"
        )

    _clients: dict[str, Any] = {}

    def model_for(name: str) -> str:
        """The model id a backend would use. Pure -- builds no client and needs no key."""
        if name == BACKEND_TYPESAFE:
            from evals.framework.typesafe_grader import DEFAULT_TYPESAFE_MODEL

            return typesafe_model or DEFAULT_TYPESAFE_MODEL
        return model

    def client_for(name: str) -> tuple[Any, str]:
        """Lazily build (client, model) per backend, so a run only authenticates
        against the services its tasks actually use."""
        if name == BACKEND_TYPESAFE:
            from evals.framework.typesafe_grader import DEFAULT_TYPESAFE_MODEL, make_client

            chosen = typesafe_model or DEFAULT_TYPESAFE_MODEL
            if name not in _clients:
                _clients[name] = None if dry_run else make_client(chosen)
            return _clients[name], chosen
        if name not in _clients:
            _require_anthropic_key()
            _clients[name] = Anthropic()
        return _clients[name], model

    needed = {resolve_task_backend(t, backend) for t in tasks_by_id.values()}

    # Checked against the resolved backends rather than the flag alone, so a run
    # whose tasks all pin `grader_backend: typesafe` can be dry-run without
    # also passing --backend typesafe.
    if dry_run and BACKEND_TYPESAFE not in needed:
        console.print(
            "[red]--dry-run inspects the TypeSafe state budget, but no task in this run "
            "resolves to the typesafe backend.[/red]\n"
            "[red]Pass --backend typesafe, or pin `grader_backend: typesafe` on a task.[/red]"
        )
        raise typer.Exit(1)

    if not dry_run:
        if BACKEND_CLAUDE in needed:
            _require_anthropic_key()
        if BACKEND_TYPESAFE in needed:
            from evals.framework.typesafe_grader import _require_typesafe_key

            _require_typesafe_key()
    if needed == {backend}:
        console.print(f"[dim]Backend: {backend}[/dim]")
    else:
        per_task = ", ".join(f"{t['id']}={resolve_task_backend(t, backend)}" for t in tasks_by_id.values())
        console.print(f"[dim]Backend: {backend} (default); per-task: {per_task}[/dim]")

    grades = []
    inspected = 0
    oversized = 0
    skipped_claude = 0

    # Scan for conversation JSONs: new layout is {runner}/{task}__{variant}.json,
    # but also support legacy flat layout {task}__{runner}__{variant}.json
    conversation_files = sorted(results_path.glob("**/*.json"))
    conversation_files = [f for f in conversation_files if f.name not in ("tasks.json", "grades.json")]

    status_ctx: Any = nullcontext() if dry_run else console.status("[bold]Grading conversations...")
    with status_ctx as status:
        for conv_file in conversation_files:
            stem = conv_file.stem
            parts = stem.split("__")

            # New namespaced layout: {runner}/{task}__{variant}[__N].json
            # The runner name comes from the parent directory
            if conv_file.parent != results_path:
                runner_name = conv_file.parent.name
                if len(parts) < 2:
                    continue
                if parts[-1].isdigit():
                    run_num = int(parts[-1])
                    variant = parts[-2]
                    task_id = "__".join(parts[:-2])
                else:
                    run_num = 1
                    variant = parts[-1]
                    task_id = "__".join(parts[:-1])
            else:
                # Legacy flat layout: {task}__{runner}__{variant}[__N].json
                if len(parts) < 3:
                    continue
                if parts[-1].isdigit():
                    run_num = int(parts[-1])
                    variant = parts[-2]
                    runner_name = parts[-3]
                    task_id = "__".join(parts[:-3])
                else:
                    run_num = 1
                    variant = parts[-1]
                    runner_name = parts[-2]
                    task_id = "__".join(parts[:-2])

            if task_id not in tasks_by_id:
                continue

            task = tasks_by_id[task_id]
            task_backend = resolve_task_backend(task, backend)

            with open(conv_file) as f:
                conversation = json.load(f)

            if dry_run:
                if task_backend != BACKEND_TYPESAFE:
                    skipped_claude += 1
                    continue
                from evals.framework.typesafe_grader import build_state_for_dry_run, print_state_budget

                workdir = conv_file.parent / f"{conv_file.stem}.workdir"
                state = build_state_for_dry_run(
                    task, conversation, skill_content, load_workdir_files(workdir)
                )
                if not print_state_budget(state, f"{task_id} / {runner_name}/{variant} (run {run_num})"):
                    oversized += 1
                inspected += 1
                continue

            grade: dict[str, Any]
            if "error" in conversation:
                grade = {
                    "pass": False,
                    "proposed_command": "",
                    "evidence": f"Runner error: {conversation.get('error')}",
                    "reasoning": f"Runner failed: {conversation.get('stderr', '')}",
                    "suggestions": [
                        {
                            "cause": f"Runner subprocess failed: {conversation.get('error', 'unknown')}",
                            "fix": f"Inspect {conv_file.stem}.stderr.log and rerun once the underlying CLI error is resolved.",
                        }
                    ],
                }
            else:
                if status:
                    status.update(f"[bold]Grading {task_id} / {runner_name}/{variant}...")
                examples_block = load_examples(skill, task_id=task_id)
                workdir = conv_file.parent / f"{conv_file.stem}.workdir"
                workdir_content = load_workdir_files(workdir)
                task_client, task_model = client_for(task_backend)
                grade = _grade_conversation_safely(
                    task_client,
                    task_model,
                    task,
                    conversation,
                    examples_block,
                    skill_content,
                    workdir_content,
                    label=f"{task_id} / {runner_name}/{variant}",
                    max_tokens=max_tokens,
                    backend=task_backend,
                )

            # Extract run stats from the JSON output
            usage = conversation.get("usage") or {}
            grade["duration_s"] = round((conversation.get("duration_ms") or 0) / 1000, 1)
            grade["cost_usd"] = round(conversation.get("total_cost_usd") or 0, 4)
            grade["num_turns"] = conversation.get("num_turns") or 0
            grade["input_tokens"] = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
            )
            grade["output_tokens"] = usage.get("output_tokens") or 0

            # setdefault, not assignment: a grade that reached a model already
            # carries the authoritative pair. This backfills the paths that never
            # called one -- trace autofails, runner errors, grader exceptions --
            # so every row says who was responsible and an all-autofail run still
            # reports a backend instead of printing no banner at all.
            grade.setdefault("grader_backend", task_backend)
            grade.setdefault("grader_model", model_for(task_backend))

            grade["task_id"] = task_id
            grade["runner"] = runner_name
            grade["variant"] = variant
            grade["run_num"] = run_num
            grade["category"] = task.get("category") or "uncategorized"
            grade["session_id"] = conversation.get("session_id") or ""
            if conversation.get("sandbox_timing"):
                grade["sandbox_timing"] = conversation["sandbox_timing"]
            grades.append(grade)

    if dry_run:
        skipped_note = f"; skipped {skipped_claude} graded by claude" if skipped_claude else ""
        console.print(
            f"\n[bold]Dry run:[/bold] inspected {inspected} conversation(s){skipped_note}; "
            f"{oversized} exceed the TypeSafe state budget."
        )
        if not inspected:
            console.print("[yellow]Nothing to inspect — no task resolves to the typesafe backend.[/yellow]")
            return
        if not skill_content:
            console.print(
                "[yellow]Note: SKILL.md was not found, so no `skill_reference` row appears above. "
                "It is usually the largest section — these totals understate a real grade.[/yellow]"
            )
        if oversized:
            console.print(
                "[yellow]Oversized states are truncated before grading — TypeSafe would judge "
                "those runs on less evidence than the claude backend sees. Shrink the inputs "
                "(smaller context_refs, a tighter SKILL.md) or grade them with --backend claude."
                "[/yellow]"
            )
        elif skill_content:
            console.print("[green]Every state fits; TypeSafe would see the full evidence.[/green]")
        else:
            console.print("[green]Every state fits.[/green]")
        console.print("[dim]No API calls made, no grades.json written.[/dim]")
        # Nonzero on truncation so this is usable as a CI preflight: "would
        # TypeSafe see everything the claude backend does?" is a pass/fail
        # question, and a green exit on a truncated state would answer it wrong.
        if oversized:
            raise typer.Exit(1)
        return

    out_path = results_path / "grades.json"
    with open(out_path, "w") as f:
        json.dump(grades, f, indent=2)

    console.print(f"[green]Grades saved to {out_path.name}[/green]\n")

    if report:
        notes = None
        notes_file = results_path / "notes.md"
        if notes_file.exists():
            notes = notes_file.read_text()
        print_report(grades, notes)

    # CI gate. Nothing above this line exits nonzero on a bad result, so without
    # --fail-under a failing eval still reports success to the caller.
    if fail_under is not None:
        ok, summary = gate_verdict(grades, fail_under, gate_variant)
        if not ok:
            console.print(f"[red]Gate failed: {summary}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]Gate passed: {summary}[/green]")


if __name__ == "__main__":
    app()
