"""Gemini CLI runner for evals.

Wraps Google's Gemini CLI in the same interface as the Claude runner
so tasks can be run against both agents and graded with the same criteria.

Gemini CLI discovers skills from .gemini/skills/ and .agents/skills/.
The runner uses `gemini skills link <path>` to register the skill
without copying files.

Gemini CLI flags used:
  -p "prompt"                   Headless/non-interactive mode
  --approval-mode=yolo          Auto-approve all tool actions
  --output-format stream-json   JSONL event stream
  --sandbox                     Sandboxed execution

No --bare or --max-turns flags exist. For without-skill, we run in a
temp directory with no skills. For turn limits, we rely on the subprocess
wall-clock timeout (the orchestrator's `--timeout` flag).

Auth: GEMINI_API_KEY env var, or OAuth via `gemini auth`.
Install: npm install -g @google/gemini-cli
"""

import json
import subprocess
import tempfile
from pathlib import Path

from .base import Runner, run_cli


class GeminiRunner(Runner):
    name = "gemini"

    def __init__(self, skill_dir: str | None = None):
        self.skill_dir = skill_dir

    def variants(self) -> list[str]:
        return ["with-skill", "without-skill", "with-docs"]

    def build_command(
        self, intent: str, variant: str, max_turns: int = 10, docs_context: str = ""
    ) -> tuple[list[str], str]:
        if variant == "with-skill" and self.skill_dir:
            skill_name = Path(self.skill_dir).name
            prompt = f"Use the /{skill_name} skill. {intent}" if skill_name else intent
        elif variant == "with-docs":
            prompt = f"{docs_context}{intent}" if docs_context else intent
        else:
            prompt = intent

        cmd = [
            "gemini",
            "--approval-mode=yolo",
            "--output-format",
            "stream-json",
            "-p",
            prompt,
        ]
        return cmd, prompt

    def run(
        self,
        intent: str,
        variant: str,
        max_turns: int = 10,
        cwd: str | None = None,
        docs_context: str = "",
        timeout: int = 90,
    ) -> dict:
        cmd, prompt = self.build_command(intent, variant, max_turns, docs_context)

        # For with-skill, link the skill and invoke via /skill-name.
        # For without-skill, run in a clean dir with no workspace skills.
        # If the orchestrator passed a cwd, use it for both variants — it's
        # already a per-task isolated tempdir. Otherwise, fall back to our
        # own tempdir for without-skill (direct-callers still get isolation).
        temp_dir_obj = None

        if variant == "with-skill" and self.skill_dir:
            # Link the skill so Gemini discovers it.
            # `gemini skills link` is interactive, so pipe "Y" to confirm.
            try:
                subprocess.run(
                    ["gemini", "skills", "link", self.skill_dir],
                    input="Y\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                pass  # Skill may already be linked
            except FileNotFoundError:
                return {
                    "error": "gemini_cli_not_found",
                    "conversation_md": (
                        "# Error\n\n"
                        "The `gemini` CLI was not found on PATH.\n"
                        "Install: npm install -g @google/gemini-cli\n"
                    ),
                }

        elif variant in ("without-skill", "with-docs") and cwd is None:
            # Caller didn't isolate us — make our own empty dir so no workspace
            # GEMINI.md or skills are discovered. User-level skills
            # (~/.gemini/skills/) still exist but without a /skill-name prompt
            # the agent won't invoke them, which is a valid baseline.
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="gemini-eval-bare-")
            cwd = temp_dir_obj.name

        try:
            stdout, stderr = run_cli(cmd, timeout=timeout, cwd=cwd)
        except FileNotFoundError:
            return {
                "error": "gemini_cli_not_found",
                "conversation_md": (
                    "# Error\n\nThe `gemini` CLI was not found on PATH.\nInstall: npm install -g @google/gemini-cli\n"
                ),
            }
        finally:
            if temp_dir_obj is not None:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass

        # Check stderr for errors
        if stderr.strip() and not stdout.strip():
            return {
                "error": "cli_error",
                "stderr": stderr.strip(),
                "conversation_md": (f"# {variant}\n\n**User:** {intent}\n\n**Error:**\n```\n{stderr.strip()}\n```\n"),
            }

        if not stdout.strip():
            return {"error": "no_output", "stderr": stderr.strip()}

        # Parse JSONL events (same format as Claude's stream-json)
        raw_lines = stdout.strip().splitlines()
        events = []
        for line in raw_lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Build readable conversation trace
        md = [f"# {variant}\n", f"**User:** {intent}\n"]
        session_id = ""
        result_event = {}

        for e in events:
            if not isinstance(e, dict):
                continue
            etype = e.get("type", "")

            if etype == "init":
                session_id = e.get("session_id", e.get("sessionId", ""))

            elif etype == "message":
                role = e.get("role", "")
                content = e.get("content", "")
                # Skip delta chunks that just continue a previous message
                if role == "assistant" and content and not e.get("delta"):
                    md.append(f"**Assistant:** {content}\n")
                elif role == "assistant" and content and e.get("delta"):
                    # Collect deltas — only append non-trivial ones
                    if content.strip():
                        md.append(f"**Assistant:** {content}\n")

            elif etype == "tool_use":
                name = e.get("tool_name", e.get("name", ""))
                params = e.get("parameters", e.get("input", {}))
                if name in ("run_shell_command", "bash", "shell"):
                    md.append(f"**Bash:** `{params.get('command', '')}`\n")
                elif name in ("read_file", "read"):
                    md.append(f"**Read:** `{params.get('file_path', params.get('path', ''))}`\n")
                elif name in ("write_file", "write", "edit"):
                    md.append(f"**{name}:** `{params.get('file_path', params.get('path', ''))}`\n")
                elif name == "activate_skill":
                    md.append(f"**Skill activated:** `{params.get('name', '')}`\n")
                else:
                    md.append(f"**{name}:** `{json.dumps(params)[:200]}`\n")

            elif etype == "tool_result":
                content = e.get("output", e.get("content", ""))
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )
                if content:
                    preview = str(content)[:500] + ("..." if len(str(content)) > 500 else "")
                    md.append(f"**Output:**\n```\n{preview}\n```\n")

            elif etype == "result":
                result_event = e

            # Fallback session_id extraction
            if e.get("session_id") or e.get("sessionId"):
                session_id = e.get("session_id", e.get("sessionId", session_id))

        if result_event.get("result") or result_event.get("response"):
            final = result_event.get("result", result_event.get("response", ""))
            md.append(f"**Final result:** {final}\n")

        # Gemini puts stats under result.stats with different field names
        stats = result_event.get("stats", {})
        output = {
            "conversation_md": "\n".join(md),
            "raw_events": raw_lines,
            "duration_ms": stats.get("duration_ms", 0),
            "num_turns": stats.get("tool_calls", 0),
            "usage": {
                "input_tokens": stats.get("input_tokens", 0),
                "output_tokens": stats.get("output_tokens", 0),
                "cache_read_input_tokens": stats.get("cached", 0),
            },
        }
        if stderr.strip():
            output["stderr"] = stderr.strip()
        if not output.get("session_id"):
            output["session_id"] = session_id
        return output
