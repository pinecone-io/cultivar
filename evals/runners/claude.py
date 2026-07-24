import json
import os
from pathlib import Path

from .base import Runner, run_cli


class ClaudeRunner(Runner):
    name = "claude"

    def __init__(self, skill_dir: str | None = None, model: str = ""):
        self.skill_dir = skill_dir
        self.model = model

    def variants(self) -> list[str]:
        return [
            "with-skill",
            "without-skill",
            "with-docs",
            "with-docs-mcp",
            "with-docs-fetch",
        ]

    def build_command(
        self, intent: str, variant: str, max_turns: int = 10, docs_context: str = ""
    ) -> tuple[list[str], str]:
        if variant == "with-skill":
            skill_name = Path(self.skill_dir).name if self.skill_dir else ""
            prompt = f"Use the /{skill_name} skill. {intent}" if skill_name else intent
        elif variant == "with-docs":
            prompt = f"{docs_context}{intent}" if docs_context else intent
        elif variant == "with-docs-mcp":
            # Agent gets the docs assistant via the Pinecone MCP server's
            # search-docs tool. The orchestrator is responsible for installing
            # the MCP server and dropping an MCP config in the sandbox at the
            # path indicated by SKILL_EVAL_MCP_CONFIG (defaults to
            # /workspace/.mcp.json for the modal runner).
            prompt = (
                "You have access to the `search-docs` tool from the docs MCP server, "
                "which searches the live documentation for the SDK you are working with. "
                "Use it to look up anything you're not sure about (modern SDK patterns, "
                "exact method names, API parameters). Then complete the task.\n\n"
                f"{intent}"
            )
        elif variant == "with-docs-fetch":
            # Agent fetches the live docs directly via WebFetch. We don't
            # hardcode a base URL — the task intent should reference the docs
            # site the agent should consult. (For Pinecone tasks: docs.pinecone.io.)
            prompt = (
                "You have access to the `WebFetch` tool. If the docs site for the "
                "SDK you are working with has an `llms.txt` index of doc URLs, use "
                "that as a starting point. Any page can usually be fetched as "
                "markdown by appending `.md` to the URL. Look up modern SDK patterns, "
                "exact method names, and API parameters as needed.\n\n"
                f"{intent}"
            )
        else:  # without-skill
            prompt = intent

        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
        ]

        # Pin the agent model when set (e.g. claude-sonnet-5). Empty => CLI default.
        if self.model:
            cmd.extend(["--model", self.model])

        if variant == "with-docs-mcp":
            mcp_cfg = os.environ.get("SKILL_EVAL_MCP_CONFIG", "/workspace/.mcp.json")
            cmd.extend(["--mcp-config", mcp_cfg])

        # --allowedTools takes a single comma-joined value; passing tools as
        # separate argv tokens silently drops everything after the first.
        #
        # NOTE: --bare is intentionally NOT used by any variant.
        # --bare strips Write from the tool set regardless of --allowedTools,
        # which silently breaks code-gen tasks. Skill leakage isn't a concern
        # here: skills are only mounted into the sandbox when skill_dir is
        # non-empty (with-skill only), and bare-flavor variants don't include
        # "Use the /<skill-name> skill." in their prompt, so the Skill tool
        # stays unused even when present.
        #
        # If you want --bare semantics later (e.g., to study system-prompt
        # cost in isolation, or to measure a tighter no-system-prompt
        # baseline), add it as a per-task opt-in rather than
        # re-enabling globally. A new variant ("bare-baseline") or a YAML
        # flag (`use_bare: true`) is the right shape; baking it into every
        # without-skill / with-docs run is what just bit us.
        if variant == "with-docs-mcp":
            # Whitelist the search-docs MCP tool. The MCP server name is
            # configured by the orchestrator's mcp config (default: pinecone-docs).
            mcp_server_name = os.environ.get("SKILL_EVAL_MCP_SERVER_NAME", "pinecone-docs")
            cmd.extend(["--allowedTools", f"Bash,Read,Write,Edit,mcp__{mcp_server_name}__search-docs"])
        elif variant == "with-docs-fetch":
            cmd.extend(["--allowedTools", "Bash,Read,Write,Edit,WebFetch"])
        elif variant in ("without-skill", "with-docs"):
            cmd.extend(["--allowedTools", "Bash,Read,Write,Edit"])
        else:
            cmd.extend(["--allowedTools", "Bash,Read,Write,Edit,Skill,ToolSearch"])

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

        stdout, stderr = run_cli(cmd, timeout=timeout, cwd=cwd)

        if not stdout.strip():
            return {"error": "no_output", "stderr": stderr.strip()}

        # Parse all events, keep raw JSONL for debugging
        raw_lines = stdout.strip().splitlines()
        events = []
        for line in raw_lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Find result event for stats, init event for session_id
        result_event = {}
        session_id = ""
        for e in events:
            if e.get("type") == "result":
                result_event = e
            elif e.get("type") == "system" and e.get("subtype") == "init":
                session_id = e.get("session_id", "")

        # Build readable conversation trace
        md = [f"# {variant}\n", f"**User:** {intent}\n"]

        for e in events:
            etype = e.get("type", "")

            if etype == "assistant":
                for block in e.get("message", {}).get("content", []):
                    btype = block.get("type", "")
                    if btype == "text" and block.get("text"):
                        md.append(f"**Assistant:** {block['text']}\n")
                    elif btype == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        if name == "Bash":
                            md.append(f"**Bash:** `{inp.get('command', '')}`\n")
                        elif name == "Skill":
                            md.append(f"**Skill invoked:** `{inp.get('skill', '')}` args=`{inp.get('args', '')}`\n")
                        elif name == "Read":
                            md.append(f"**Read:** `{inp.get('file_path', '')}`\n")
                        elif name == "ToolSearch":
                            md.append(f"**ToolSearch:** `{inp.get('query', '')}`\n")
                        else:
                            md.append(f"**{name}:** `{json.dumps(inp)[:200]}`\n")

            elif etype == "tool_result":
                content = e.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
                if content:
                    preview = content[:500] + ("..." if len(content) > 500 else "")
                    md.append(f"**Output:**\n```\n{preview}\n```\n")

        if result_event.get("result"):
            md.append(f"**Final result:** {result_event['result']}\n")

        output = {
            "conversation_md": "\n".join(md),
            "raw_events": raw_lines,
            **result_event,
        }
        # Ensure session_id is always present (result event has it, but on timeout it won't)
        if not output.get("session_id"):
            output["session_id"] = session_id
        return output
