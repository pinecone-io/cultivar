"""Grade eval results with TypeSafe's System One API (Jev) instead of Claude.

Opt-in alternative to the Claude grader, selected with `cultivar grade
--backend typesafe`. Claude remains the default.

The two backends are not interchangeable in kind. Claude is asked to return a
JSON object and writes every field itself, including free-text `evidence` and
`reasoning`. A System One model does not generate text at all -- it returns
typed judgments and calibrated probabilities. So this backend asks Jev the two
things it is actually good at (did the agent complete the task, and if not what
kind of failure was it) and this module composes the surrounding grade dict in
code. `evidence` and `proposed_command` are therefore not quotes from the run;
they say so in plain text rather than inventing one.

The returned dict has the same shape the Claude path returns, so grades.json,
report.py, show.py, and the CI gate need no backend-specific handling.
"""

import os
from typing import Any

import typer

from evals.framework.grader import GRADER_CONV_CAP, _normalize_suggestions, _pregrade_autofail
from evals.framework.reporting import console

DEFAULT_TYPESAFE_MODEL = "jev-latest"

# The SDK default is 10s, tuned for the short states System One usually gets. A
# grader state carries up to GRADER_CONV_CAP of conversation plus the captured
# workdir, so the default would time out on the largest runs.
TYPESAFE_TIMEOUT_S = 120.0

# Deliberately the plain even split rather than a tuned value: calibrate it
# against your own tasks before moving it.
PASS_THRESHOLD = 0.5

# Jev budgets this many tokens for `state` plus the longest question, where the
# Claude path only has to fit a much larger context window. The grader's own
# caps (50k conversation + 40k workdir + 100k context_refs + SKILL.md) can far
# exceed it, so the state is trimmed or the request is rejected outright.
# Check https://docs.typesafe.ai/models for the current model's figure.
TYPESAFE_STATE_MAX_TOKENS = 32_000

# The SDK takes text but the budget is in tokens, so both numbers below are
# estimates -- and deliberately different ones. Reporting uses the realistic
# density; the char cap is DERIVED from the pessimistic one, which keeps the
# invariant that even all-dense content stays under budget. Deriving it rather
# than hardcoding a second number is what stops the two from contradicting.
_CHARS_PER_TOKEN = 3.5
_DENSE_CHARS_PER_TOKEN = 3.0

# The questions share the token budget with the state.
_QUESTION_HEADROOM_TOKENS = 1_500

TYPESAFE_STATE_CAP_CHARS = int((TYPESAFE_STATE_MAX_TOKENS - _QUESTION_HEADROOM_TOKENS) * _DENSE_CHARS_PER_TOKEN)

# Shed order when the state overflows, least valuable first. The trace being
# judged is last because a grade made without it is worthless; documentation the
# agent merely had access to goes first.
_SHED_ORDER = ("skill_reference", "reference_material", "generated_code_files", "agent_conversation")

# Every section the Claude prompt builds, mapped to the state field carrying it.
# A value of None marks a section this backend deliberately does NOT send; the
# parity test asserts against this table, so adding a section to one backend
# and forgetting the other fails a test instead of silently skewing a
# cross-backend comparison.
SECTION_PARITY: dict[str, str | None] = {
    "Skill Reference": "skill_reference",
    "Criteria": "task_criteria",
    "Expected commands": "expected_commands",
    "Flexibility notes": "acceptable_variations",
    "Expected outcome": "expected_outcome",
    "Reference Material": "reference_material",
    "Agent Conversation": "agent_conversation",
    "Verification Output": "verification_output",
    "Generated Code Files": "generated_code_files",
    # Few-shot PASS/FAIL examples anchor a text model's threshold. A System One
    # model is calibrated already and is steered by PASS_THRESHOLD instead, so
    # these are withheld — the one intentional asymmetry, warned about at
    # runtime whenever examples actually exist.
    "Calibration Examples": None,
}

_COMPLETED_Q = "completed"
_FAILURE_Q = "failure_kind"

# Jev picks one label; code turns the pick into the {cause, fix} pair the report
# renders, since System One can't write the suggestion prose itself.
_FAILURE_KINDS: dict[str, dict[str, str]] = {
    "not_attempted": {
        "cause": "The agent never attempted the task described by the criteria.",
        "fix": "Check that the task prompt actually reaches the agent — run `cultivar run --dry-run` and read the prompt it prints.",
    },
    "wrong_approach": {
        "cause": "The agent attempted the task but took an approach the criteria don't accept.",
        "fix": "Make the expected approach explicit in the skill, or widen `ground_truth.flexible` if the approach is in fact acceptable.",
    },
    "incomplete": {
        "cause": "The agent did part of the task and stopped before finishing it.",
        "fix": "Check whether the run hit its turn or timeout budget — raise `--max-turns` / `--timeout`, or split the task.",
    },
    "incorrect_result": {
        "cause": "The agent finished, but the commands or files it produced don't satisfy the criteria.",
        "fix": "Read `cultivar show <run> --workdir` against the criteria; tighten the skill where the output drifted.",
    },
    "environment_error": {
        "cause": "The run was blocked by a tool, auth, or environment failure rather than by the agent's judgment.",
        "fix": "Inspect `<run>/<runner>/<base>.stderr.log` and fix the underlying CLI or credential error, then re-run.",
    },
    "no_failure": {
        "cause": "",
        "fix": "",
    },
}


def _require_typesafe_key() -> None:
    """Fail fast with a clear message if TYPESAFE_API_KEY isn't set.

    Mirrors the Claude path's `_require_anthropic_key`. Deliberately a hard
    failure rather than a silent fallback to Claude: a run the user asked
    TypeSafe to grade must not end up with Claude's verdicts written into
    grades.json under a `typesafe` label.
    """
    if not os.environ.get("TYPESAFE_API_KEY"):
        console.print(
            "[red]TYPESAFE_API_KEY is not set.[/red]\n"
            "--backend typesafe needs this to call the TypeSafe System One API.\n\n"
            "Set it one of these ways:\n"
            "  - Add [bold]TYPESAFE_API_KEY=...[/bold] to a .env file "
            "in your current directory (it's auto-loaded)\n"
            "  - [bold]export TYPESAFE_API_KEY=...[/bold] in your shell\n"
            "  - Create a key at https://console.typesafe.ai/\n\n"
            "Or drop the flag to grade with Claude (the default)."
        )
        raise typer.Exit(1)


def make_client(model: str = DEFAULT_TYPESAFE_MODEL):
    """Build a TypeSafeClient, importing the SDK lazily.

    The import is deferred so that `evals.cli` — which imports the grader
    module eagerly for every subcommand — keeps working when typesafe-sdk
    isn't installed. Same reason run.py defers its grader import.
    """
    _require_typesafe_key()
    try:
        from typesafe_sdk import TypeSafeClient
    except ImportError:
        console.print(
            "[red]typesafe-sdk is not installed.[/red]\n"
            "Install it with [bold]uv add typesafe-sdk[/bold] (or [bold]pip install typesafe-sdk[/bold]), "
            "or drop --backend typesafe to grade with Claude."
        )
        raise typer.Exit(1) from None
    return TypeSafeClient(model=model, timeout=TYPESAFE_TIMEOUT_S)


def auth_error_types() -> tuple[type[BaseException], ...]:
    """TypeSafe exceptions that must abort the whole batch, not fail one grade.

    A revoked key fails identically on every remaining conversation, so the
    caller re-raises these instead of writing a wall of identical FAILs.
    Returns an empty tuple if the SDK isn't importable — there is then nothing
    backend-specific to special-case.
    """
    try:
        from typesafe_sdk import TypeSafeAuthenticationError, TypeSafePermissionDeniedError
    except ImportError:
        return ()
    return (TypeSafeAuthenticationError, TypeSafePermissionDeniedError)


def build_state(
    task: dict,
    conv_str: str,
    verify_output: str = "",
    workdir_content: str = "",
    refs_content: str = "",
    skill_content: str = "",
    fit: bool = True,
) -> dict[str, Any]:
    """Assemble the System One state as named JSON fields.

    Named fields rather than one concatenated prompt: the questions reference
    them by path, which is how a System One model is told which part of the
    state a judgment is about. Calibration examples are deliberately left out —
    they exist to anchor a text model's pass/fail threshold, and Jev takes the
    criteria directly.
    """
    gt = task.get("ground_truth", {})
    state: dict[str, Any] = {
        "task_criteria": gt.get("criteria", "") or "(no specific criteria provided)",
        "agent_conversation": conv_str,
    }
    if gt.get("commands"):
        state["expected_commands"] = gt["commands"]
    if gt.get("flexible"):
        state["acceptable_variations"] = gt["flexible"]
    if gt.get("outcome"):
        state["expected_outcome"] = gt["outcome"]
    if verify_output:
        state["verification_output"] = verify_output
    if workdir_content:
        state["generated_code_files"] = workdir_content
    if refs_content:
        state["reference_material"] = refs_content
    if skill_content:
        state["skill_reference"] = skill_content
    return _fit_state_to_budget(state) if fit else state


def state_chars(state: dict[str, Any]) -> int:
    """Total characters across the state's string fields."""
    return sum(len(v) for v in state.values() if isinstance(v, str))


def estimate_tokens(chars: int) -> int:
    """Rough token count for `chars` of grader state. An estimate, not a count."""
    return int(chars / _CHARS_PER_TOKEN)


def state_token_report(state: dict[str, Any]) -> list[tuple[str, int, int]]:
    """Per-field (name, chars, estimated tokens), largest first.

    Feeds both the oversize warning and `--dry-run`, so the number the user is
    warned about is the same one the breakdown shows.
    """
    rows = [(k, len(v), estimate_tokens(len(v))) for k, v in state.items() if isinstance(v, str)]
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _fit_state_to_budget(state: dict[str, Any], cap: int = TYPESAFE_STATE_CAP_CHARS) -> dict[str, Any]:
    """Trim the bulky state fields until the whole state fits Jev's budget.

    Sheds in `_SHED_ORDER` rather than trimming whichever field is largest, so
    a big SKILL.md is spent before the trace it would otherwise crowd out.
    `task_criteria` and the expectations are never trimmed — losing those
    changes the question being asked.
    """
    if state_chars(state) <= cap:
        return state

    trimmed = dict(state)
    marker = "\n... (truncated to fit the TypeSafe state budget)"
    for field in _SHED_ORDER:
        if state_chars(trimmed) <= cap:
            break
        body = trimmed.get(field)
        if not isinstance(body, str) or not body:
            continue
        overshoot = state_chars(trimmed) - cap
        keep = len(body) - overshoot - len(marker)
        trimmed[field] = body[:keep] + marker if keep > 0 else marker.strip()

    before, after = state_chars(state), state_chars(trimmed)
    dropped = [f for f in _SHED_ORDER if len(trimmed.get(f, "")) < len(state.get(f, ""))]
    console.print(
        f"[yellow]TypeSafe state trimmed {before:,} → {after:,} chars "
        f"(~{estimate_tokens(before):,} → ~{estimate_tokens(after):,} tokens) to fit the "
        f"{TYPESAFE_STATE_MAX_TOKENS:,}-token state budget. Truncated: {', '.join(dropped)}.[/yellow]\n"
        f"[yellow]The grade is made on less evidence than the Claude backend would see. "
        f"Run `cultivar grade ... --backend typesafe --dry-run` for a per-section breakdown.[/yellow]"
    )
    return trimmed


def build_questions() -> dict:
    """The two judgments the grade is composed from.

    Both are asked in a single request over the same state. They are
    independent: `failure_kind` states its own premise ("assuming it did not
    fully satisfy...") so it can be asked speculatively and read only when
    `completed` comes back below threshold.
    """
    from typesafe_sdk import Choice, Noul

    return {
        _COMPLETED_Q: Noul(
            instructions=(
                "Judge whether the AI agent successfully completed the task. The agent's run is in "
                "`agent_conversation`; any files it wrote are in `generated_code_files` and any "
                "post-run check is in `verification_output`. Judge only against `task_criteria`, "
                "`expected_commands`, `expected_outcome`, and `acceptable_variations`. "
                "`reference_material` and `skill_reference` show what correct behavior looks "
                "like — they are documentation the agent had access to, not its output, so "
                "never treat code or commands appearing there as work the agent did."
            ),
            criteria={
                "true": "The run satisfies the task criteria, allowing for any stated acceptable variations.",
                "false": "The run does not satisfy the criteria, whether it was wrong, unfinished, or never attempted.",
            },
        ),
        _FAILURE_Q: Choice(
            instructions=(
                "Assuming the agent did NOT fully satisfy `task_criteria`, classify what kind of "
                "failure the run in `agent_conversation` shows. Answer this even if the run looks "
                "successful; choose 'no_failure' in that case."
            ),
            criteria={
                "not_attempted": "The agent never attempted the task at all.",
                "wrong_approach": "The agent attempted the task but used commands or an approach the criteria don't accept.",
                "incomplete": "The agent started the task correctly and stopped before finishing it.",
                "incorrect_result": "The agent finished, but its commands or files don't satisfy the criteria.",
                "environment_error": "A tool, auth, network, or environment failure blocked the run.",
                "no_failure": "The run satisfies the criteria; there is no failure to classify.",
            },
        ),
    }


def print_state_budget(state: dict[str, Any], label: str) -> bool:
    """Print the per-section token breakdown for one conversation's state.

    Returns True if the state fits the budget. Used by `--dry-run` to answer
    "will TypeSafe see everything the Claude backend would?" without spending
    a request, since the answer is a property of the inputs alone.
    """
    rows = state_token_report(state)
    chars = state_chars(state)
    tokens = estimate_tokens(chars)
    fits = chars <= TYPESAFE_STATE_CAP_CHARS

    console.print(f"\n[bold]{label}[/bold]")
    for name, n_chars, n_tokens in rows:
        share = (n_chars / chars * 100) if chars else 0
        console.print(f"  {name:<24} {n_chars:>8,} chars  ~{n_tokens:>6,} tok  {share:>5.1f}%")
    verdict = "[green]fits[/green]" if fits else "[red]OVER BUDGET — will be truncated[/red]"
    console.print(
        f"  {'TOTAL':<24} {chars:>8,} chars  ~{tokens:>6,} tok  "
        f"(cap ~{estimate_tokens(TYPESAFE_STATE_CAP_CHARS):,} tok) {verdict}"
    )
    return fits


def build_state_for_dry_run(
    task: dict,
    conversation: dict,
    skill_content: str = "",
    workdir_content: str = "",
) -> dict[str, Any]:
    """The untrimmed state a real grade would build, for inspection only."""
    from evals.framework.grader import load_context_refs

    conv_str = conversation.get("conversation_md", "")
    if len(conv_str) > GRADER_CONV_CAP:
        conv_str = conv_str[:GRADER_CONV_CAP] + "\n... (truncated)"
    return build_state(
        task,
        conv_str,
        verify_output=conversation.get("verify_output", ""),
        workdir_content=workdir_content,
        refs_content=load_context_refs(task.get("ground_truth", {}).get("context_refs", [])),
        skill_content=skill_content,
        fit=False,
    )


def grade_one_typesafe(
    client,
    model: str,
    task: dict,
    conversation: dict,
    skill_content: str = "",
    workdir_content: str = "",
) -> dict[str, Any]:
    """Grade one conversation with System One; returns the Claude path's dict shape.

    `skill_content` (SKILL.md) goes into the state under `skill_reference` —
    the same input the Claude backend gets — framed in the questions as
    documentation rather than the agent's output. Withholding it would make the
    two backends judge from different evidence and quietly invalidate any
    comparison between them.
    """
    from evals.framework.grader import load_context_refs

    conv_str = conversation.get("conversation_md", "")

    auto = _pregrade_autofail(task, conversation, conv_str, workdir_content)
    if auto is not None:
        return auto

    if len(conv_str) > GRADER_CONV_CAP:
        conv_str = conv_str[:GRADER_CONV_CAP] + "\n... (truncated)"

    state = build_state(
        task,
        conv_str,
        verify_output=conversation.get("verify_output", ""),
        workdir_content=workdir_content,
        refs_content=load_context_refs(task.get("ground_truth", {}).get("context_refs", [])),
        skill_content=skill_content,
    )

    response = client.system_one(state=state, questions=build_questions(), model=model)

    p_completed = response.nouls[_COMPLETED_Q].noul
    passed = p_completed >= PASS_THRESHOLD

    failure = response.choices.get(_FAILURE_Q)
    failure_kind = failure.choice if failure else ""
    failure_confidence = failure.confidence if failure else 0.0

    suggestions = []
    if not passed:
        mapped = _FAILURE_KINDS.get(failure_kind)
        if mapped and mapped["fix"]:
            suggestions = [dict(mapped)]
        else:
            suggestions = [
                {
                    "cause": f"Graded as a failure (P(completed)={p_completed:.2f}) with no recognized failure kind"
                    + (f" (model answered '{failure_kind}')." if failure_kind else "."),
                    "fix": "Read the run with `cultivar show <run>` and compare it against the task criteria by hand.",
                }
            ]

    reasoning = (
        f"TypeSafe {model} judged P(task completed) = {p_completed:.2f} "
        f"(threshold {PASS_THRESHOLD:.2f} → {'PASS' if passed else 'FAIL'})."
    )
    if not passed and failure_kind and failure_kind != "no_failure":
        reasoning += f" Failure classified as '{failure_kind}' (confidence {failure_confidence:.2f})."

    grade: dict[str, Any] = {
        "pass": passed,
        # System One returns typed judgments, not text: there is no model-written
        # quote or command to report, and synthesizing one in code would be a
        # worse lie than saying so.
        "proposed_command": "",
        "evidence": (
            f"(no quoted evidence: graded by TypeSafe {model}, which returns typed judgments "
            f"rather than text. P(completed)={p_completed:.2f}.)"
        ),
        "reasoning": reasoning,
        "suggestions": _normalize_suggestions(suggestions),
        "grader_backend": "typesafe",
        "grader_model": model,
        "typesafe_p_completed": round(p_completed, 4),
    }
    if failure_kind:
        grade["typesafe_failure_kind"] = failure_kind
        grade["typesafe_failure_confidence"] = round(failure_confidence, 4)
    return grade
