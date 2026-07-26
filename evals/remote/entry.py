"""Entry script that runs inside a Modal sandbox.

Imports the real runner, calls .run(), prints the result as JSON to stdout.
This ensures Modal produces identical output to local runs.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, "/workspace/evals")
# These imports resolve only inside the Modal sandbox (where the runners
# package is mounted at /workspace/evals/runners). ty can't see that path.
from runners.claude import ClaudeRunner  # ty: ignore[unresolved-import]
from runners.copilot import CopilotRunner  # ty: ignore[unresolved-import]
from runners.gemini import GeminiRunner  # ty: ignore[unresolved-import]

RUNNERS = {
    "claude": ClaudeRunner,
    "copilot": CopilotRunner,
    "gemini": GeminiRunner,
}

parser = argparse.ArgumentParser()
parser.add_argument("--runner", required=True, choices=RUNNERS.keys())
parser.add_argument("--intent", required=True)
parser.add_argument("--variant", required=True)
parser.add_argument("--max-turns", type=int, default=10)
parser.add_argument("--skill-dir", default="")
parser.add_argument(
    "--cwd", default="/workspace/app", help="Working directory for the agent. Any files it writes here are captured."
)
parser.add_argument(
    "--docs-file",
    default="",
    help="Path inside the sandbox to a file containing the with-docs prompt prefix. "
    "Passed via file (not argv) since the prefix can be tens of KB.",
)
parser.add_argument(
    "--timeout", type=int, default=90, help="Subprocess wall-clock budget in seconds for the agent CLI invocation."
)
parser.add_argument("--model", default="", help="Agent model id to pin (e.g. claude-sonnet-5). Empty uses the CLI default.")
args = parser.parse_args()

os.makedirs(args.cwd, exist_ok=True)

docs_context = ""
if args.docs_file:
    with open(args.docs_file) as f:
        docs_context = f.read()

runner_cls = RUNNERS[args.runner]
# Only ClaudeRunner accepts a model kwarg today; keep copilot/gemini construction unchanged.
if args.runner == "claude":
    r = runner_cls(skill_dir=args.skill_dir, model=args.model)
else:
    r = runner_cls(skill_dir=args.skill_dir)
result = r.run(
    args.intent,
    args.variant,
    max_turns=args.max_turns,
    cwd=args.cwd,
    docs_context=docs_context,
    timeout=args.timeout,
)

# Print as JSON to stdout — the orchestrator reads this
print(json.dumps(result))
