"""GitHub Copilot CLI runner for evals.

Wraps GitHub's Copilot CLI in the same interface as the Claude runner
so tasks can be run against both agents and graded with the same criteria.

Copilot CLI discovers skills from .claude/skills/ (same as Claude Code),
so the /skill-name invocation pattern works identically.

Copilot CLI flags used:
  -p "prompt"                   Headless/non-interactive mode
  --autopilot                   Multi-step autonomous execution
  --yolo                        Auto-approve all tools/paths/URLs
  --max-autopilot-continues N   Equivalent to --max-turns
  --output-format json          JSONL event stream
  --no-ask-user                 Suppress clarifying questions
  --no-custom-instructions      Disable AGENTS.md loading (bare mode)

Auth: COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN env var.
Install: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli
"""

import json
from pathlib import Path

from .base import Runner, run_cli


class CopilotRunner(Runner):
    name = "copilot"

    def __init__(self, skill_dir: str | None = None):
        self.skill_dir = skill_dir

    def variants(self) -> list[str]:
        return ["with-skill", "without-skill", "with-docs"]

    def build_command(
        self,
        intent: str,
        variant: str,
        max_turns: int = 10,
        docs_context: str = "",
        extra_tools: list[str] | None = None,
        model: str | None = None,
    ) -> tuple[list[str], str]:
        # extra_tools (task YAML opt-in) has no matching mechanism here yet —
        # Copilot has no allow-list, only --excluded-tools, and its baseline
        # already permits web access. model (cultivar run --model override) isn't
        # wired up either, though Copilot does have its own model flag — do that
        # if/when Copilot is actually in scope for a model comparison. Both
        # accepted for interface parity with ClaudeRunner.
        if variant == "with-skill" and self.skill_dir:
            skill_name = Path(self.skill_dir).name
            prompt = f"Use the /{skill_name} skill. {intent}" if skill_name else intent
        elif variant == "with-docs":
            prompt = f"{docs_context}{intent}" if docs_context else intent
        else:
            prompt = intent

        cmd = [
            "copilot",
            "--autopilot",
            "--yolo",
            "--max-autopilot-continues",
            str(max_turns),
            "--output-format",
            "json",
            "--no-ask-user",
        ]

        if variant in ("without-skill", "with-docs"):
            cmd.append("--no-custom-instructions")
            cmd.extend(["--excluded-tools", "skill"])

        # -p must come last to avoid flag parsing issues
        cmd.extend(["-p", prompt])
        return cmd, prompt

    def run(
        self,
        intent: str,
        variant: str,
        max_turns: int = 10,
        cwd: str | None = None,
        docs_context: str = "",
        timeout: int = 90,
        extra_tools: list[str] | None = None,
        model: str | None = None,
    ) -> dict:
        cmd, prompt = self.build_command(intent, variant, max_turns, docs_context, extra_tools, model)

        try:
            stdout, stderr = run_cli(cmd, timeout=timeout, cwd=cwd)
        except FileNotFoundError:
            return {
                "error": "copilot_cli_not_found",
                "conversation_md": (
                    "# Error\n\n"
                    "The `copilot` CLI was not found on PATH.\n"
                    "Install: https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli\n"
                ),
            }

        # Check stderr for errors (e.g. policy blocks, auth failures)
        if stderr.strip() and not stdout.strip():
            return {
                "error": "cli_error",
                "stderr": stderr.strip(),
                "conversation_md": (f"# {variant}\n\n**User:** {intent}\n\n**Error:**\n```\n{stderr.strip()}\n```\n"),
            }

        if not stdout.strip():
            return {"error": "no_output"}

        # Parse JSONL events
        raw_lines = stdout.strip().splitlines()
        events = []
        for line in raw_lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Build readable conversation trace.
        # Copilot events are nested under "data" with types like:
        #   assistant.message (data.content, data.toolRequests[])
        #   tool.execution_start (data.toolName, data.arguments)
        #   tool.execution_complete (data.result.content)
        #   assistant.message_delta (data.deltaContent)
        md = [f"# {variant}\n", f"**User:** {intent}\n"]
        session_id = ""
        last_assistant_msg = ""
        num_turns = 0
        total_output_tokens = 0
        # Map toolCallId -> toolName from execution_start events
        tool_names = {}

        for e in events:
            if not isinstance(e, dict):
                continue
            etype = e.get("type", "")
            data = e.get("data", {})

            if etype == "assistant.message":
                # Text content comes via message_delta events — skip here
                # to avoid duplication. Only extract tool requests.
                # Tool requests
                for req in data.get("toolRequests", []):
                    name = req.get("name", "")
                    args = req.get("arguments", {})
                    if name == "bash":
                        md.append(f"**Bash:** `{args.get('command', '')}`\n")
                    elif name == "skill":
                        md.append(f"**Skill invoked:** `{args.get('skill', '')}`\n")
                    elif name in ("read", "read_file"):
                        md.append(f"**Read:** `{args.get('file_path', args.get('path', ''))}`\n")
                    elif name in ("write", "write_file", "edit"):
                        md.append(f"**{name}:** `{args.get('file_path', args.get('path', ''))}`\n")
                    elif name in ("report_intent", "task_complete"):
                        pass  # Internal copilot events, skip
                    else:
                        md.append(f"**{name}:** `{json.dumps(args)[:200]}`\n")
                # Track output tokens
                total_output_tokens += data.get("outputTokens", 0)

            elif etype == "tool.execution_start":
                tool_call_id = data.get("toolCallId", "")
                tool_name = data.get("toolName", "")
                if tool_call_id and tool_name:
                    tool_names[tool_call_id] = tool_name

            elif etype == "tool.execution_complete":
                result_data = data.get("result", {})
                content = result_data.get("content", "")
                tool_call_id = data.get("toolCallId", "")
                tool_name = tool_names.get(tool_call_id, "")
                if content and tool_name not in ("report_intent", "task_complete"):
                    preview = content[:500] + ("..." if len(content) > 500 else "")
                    md.append(f"**Output:**\n```\n{preview}\n```\n")

            elif etype == "assistant.message_delta":
                # Collect final assistant message from deltas
                delta = data.get("deltaContent", "")
                last_assistant_msg += delta

            elif etype == "assistant.turn_end":
                # Flush accumulated deltas as a complete message
                if last_assistant_msg.strip():
                    md.append(f"**Assistant:** {last_assistant_msg.strip()}\n")
                    last_assistant_msg = ""
                num_turns += 1

            # Extract session_id from id field or data
            if e.get("session_id"):
                session_id = e["session_id"]
            if data.get("session_id"):
                session_id = data["session_id"]

        output = {
            "conversation_md": "\n".join(md),
            "raw_events": raw_lines,
            "num_turns": num_turns,
            "usage": {
                "output_tokens": total_output_tokens,
            },
        }
        if stderr.strip():
            output["stderr"] = stderr.strip()
        if not output.get("session_id"):
            output["session_id"] = session_id
        return output
