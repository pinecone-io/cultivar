"""Modal sandbox runner — executes evals in isolated cloud containers.

Each sandbox gets its own filesystem, so CLI auth state doesn't conflict
between parallel runs. The actual runner code (ClaudeRunner, etc.) runs
unchanged inside the sandbox via entry.py.
"""

import json
import os
import time
import uuid
from pathlib import Path

import modal

# Name of the Modal secret mounted into each sandbox. Override with env var
# to use a different secret name without patching the source.
SECRET_NAME = os.environ.get("CULTIVAR_MODAL_SECRET", "eval-sandbox-secrets")
_MODAL_APP_NAME = os.environ.get("CULTIVAR_MODAL_APP", "cultivar")

EVALS_DIR = Path(__file__).parent.parent

# Image: debian + node + claude/copilot/gemini CLIs + python deps
# Skill-specific CLIs (e.g. `pc`, `gh`, `aws`) belong in the task `setup` field,
# not here — see docs/sandbox.md for the pattern.
eval_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "git", "jq", "procps")
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -",
        "apt-get install -y nodejs",
    )
    .run_commands(
        "npm install -g @anthropic-ai/claude-code @google/gemini-cli @github/copilot @pinecone-database/mcp"
    )
    .pip_install("pyyaml")
    .add_local_dir(str(EVALS_DIR / "runners"), remote_path="/workspace/evals/runners")
    .add_local_file(str(EVALS_DIR / "remote" / "entry.py"), remote_path="/workspace/evals/remote/entry.py")
    .add_local_file(str(EVALS_DIR / "remote" / "eval_isolate.py"), remote_path="/workspace/evals/remote/eval_isolate.py")
)

app = modal.App.lookup(_MODAL_APP_NAME, create_if_missing=True)

# Wall-clock budget the sandbox itself gets, on top of the agent's per-call
# timeout. The buffer covers image cold-start, setup, verify, teardown, and
# workdir extraction — everything the sandbox does outside the agent run.
# Raised from 60s: execution-based verify actually runs the generated code
# (real Pinecone index create + ready-poll + upsert + queries), which can take
# a few minutes. Override with SKILL_EVAL_SANDBOX_BUFFER_S.
SANDBOX_BUFFER_S = int(os.environ.get("SKILL_EVAL_SANDBOX_BUFFER_S", "360"))


def run_one_remote(
    runner: str,
    intent: str,
    variant: str,
    max_turns: int = 10,
    skill_dir: str = "",
    setup: str = "",
    teardown: str = "",
    verify: str = "",
    workdir_out: Path | None = None,
    workdir_src: str = "/workspace/app",
    docs_context: str = "",
    timeout: int = 90,
    model: str = "",
) -> dict:
    """Run a single eval task in a Modal sandbox. Returns the runner result dict."""
    secrets = [modal.Secret.from_name(SECRET_NAME)]
    # The named eval-sandbox-secrets covers the agent CLI (ANTHROPIC_API_KEY).
    # Execution-based `verify` runs the agent's generated code against the real
    # service, so the sandbox also needs the data-plane keys. Inject whichever
    # are present in the local env (loaded from .env) as an ephemeral, run-scoped
    # secret — NOT persisted as a named Modal secret.
    _extra: dict[str, str | None] = {
        k: os.environ[k] for k in ("PINECONE_API_KEY", "COHERE_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)
    }
    # Per-run index-name prefix: lets parallel runs share one Pinecone project
    # without colliding on the hard-coded index names in the generated code.
    # eval_isolate.apply()/teardown() read this from the sandbox env.
    _extra["EVAL_IDX_PREFIX"] = "ev" + uuid.uuid4().hex[:6] + "-"
    secrets.append(modal.Secret.from_dict(_extra))
    image = eval_image

    # Bake skill files into the image if needed
    if skill_dir:
        local_skill = Path(skill_dir)
        if not local_skill.exists():
            return {
                "error": "skill_dir_not_found",
                "conversation_md": f"# Error\n\nSkill directory not found: {skill_dir}\n",
            }
        skill_name = local_skill.name
        image = image.add_local_dir(
            str(local_skill),
            remote_path=f"/workspace/.claude/skills/{skill_name}",
        )
        # Remote skill_dir is the path inside the sandbox
        skill_dir = f"/workspace/.claude/skills/{skill_name}"

    t_start = time.time()
    sb = modal.Sandbox.create(
        app=app,
        image=image,
        timeout=timeout + SANDBOX_BUFFER_S,
        secrets=secrets,
        workdir="/workspace",
    )
    t_sandbox = time.time()

    try:
        # Run setup command inside sandbox
        if setup:
            p = sb.exec("bash", "-c", setup)
            p.stdout.read()  # drain pipe so wait() doesn't block
            setup_stderr = p.stderr.read()
            p.wait()
            if p.returncode != 0:
                return {
                    "error": f"setup_failed (exit {p.returncode})",
                    "stderr": setup_stderr[:2000],
                    "conversation_md": f"# Error\n\nSetup command failed (exit {p.returncode})\n```\n$ {setup}\n{setup_stderr[:1000]}\n```\n",
                }
        t_setup = time.time()

        # Drop the with-docs prompt prefix as a file in the sandbox so we can
        # pass it to entry.py without bloating argv (~100 KB cap, but argv is
        # noisy and harder to inspect than a file the entry script reads).
        # NOTE: sb.filesystem.write_bytes raises TypeError on the Modal versions
        # we hit in practice (signature drift). Using sb.open emits a
        # PendingDeprecationWarning but actually works. Revisit when Modal
        # stabilizes the filesystem API.
        docs_file_remote = ""
        if docs_context:
            docs_file_remote = "/workspace/.docs_context.txt"
            with sb.open(docs_file_remote, "wb") as f:
                f.write(docs_context.encode("utf-8"))

        # with-docs-mcp variant: write an MCP config the agent's CLI can load.
        # The Pinecone MCP server is installed in the image; PINECONE_API_KEY
        # comes from the sandbox secret.
        if variant == "with-docs-mcp":
            mcp_config = {
                "mcpServers": {
                    "pinecone-docs": {
                        "command": "pinecone-mcp",
                        "args": [],
                    }
                }
            }
            with sb.open("/workspace/.mcp.json", "wb") as f:
                f.write(json.dumps(mcp_config).encode("utf-8"))

        # Run the eval via entry.py
        cmd_args = [
            "python3",
            "/workspace/evals/remote/entry.py",
            "--runner",
            runner,
            "--intent",
            intent,
            "--variant",
            variant,
            "--max-turns",
            str(max_turns),
            "--skill-dir",
            skill_dir,
            "--cwd",
            workdir_src,
            "--timeout",
            str(timeout),
        ]
        if model:
            cmd_args.extend(["--model", model])
        if docs_file_remote:
            cmd_args.extend(["--docs-file", docs_file_remote])
        p = sb.exec(*cmd_args)
        stdout = p.stdout.read()
        stderr = p.stderr.read()
        p.wait()
        t_eval = time.time()

        if p.returncode != 0:
            return {
                "error": f"sandbox_exit_{p.returncode}",
                "stderr": stderr[:2000],
                "conversation_md": f"# Error\n\nSandbox exited with code {p.returncode}\n```\n{stderr[:1000]}\n```\n",
            }

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "error": "invalid_json_from_sandbox",
                "stderr": stderr[:2000],
                "conversation_md": f"# Error\n\nSandbox returned invalid JSON\n```\n{stdout[:1000]}\n```\n",
            }

        # Run verify inside sandbox
        if verify:
            p = sb.exec("bash", "-c", verify)
            verify_stdout = p.stdout.read()
            verify_stderr = p.stderr.read()
            p.wait()
            result["verify_output"] = verify_stdout.strip()
            result["verify_exit_code"] = p.returncode
            if verify_stderr.strip():
                result["verify_stderr"] = verify_stderr.strip()

        # Pull agent-written files out of the sandbox before teardown runs
        if workdir_out is not None:
            p = sb.exec("find", workdir_src, "-type", "f")
            find_stdout = p.stdout.read()
            p.wait()
            files = [line for line in find_stdout.strip().splitlines() if line]
            for abs_path in files:
                rel = Path(abs_path).relative_to(workdir_src)
                # Skip noise dirs — matches the local ignore list
                if any(part in {"__pycache__", ".venv", "node_modules", ".git"} for part in rel.parts):
                    continue
                dest = workdir_out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # sb.filesystem.copy_to_local raises TypeError on the Modal
                # versions we use; sb.open emits a deprecation warning but
                # actually pulls bytes back to local disk. Without this the
                # workdir capture silently no-ops and code-gen runs autofail
                # despite the agent having written the file.
                with sb.open(abs_path, "rb") as src:
                    dest.write_bytes(src.read())

        # Run teardown inside sandbox
        if teardown:
            p = sb.exec("bash", "-c", teardown)
            p.stdout.read()
            p.wait()
        t_teardown = time.time()

        result["sandbox_timing"] = {
            "create_s": round(t_sandbox - t_start, 1),
            "setup_s": round(t_setup - t_sandbox, 1),
            "eval_s": round(t_eval - t_setup, 1),
            "teardown_s": round(t_teardown - t_eval, 1),
            "total_s": round(t_teardown - t_start, 1),
        }
        return result

    finally:
        sb.terminate()
