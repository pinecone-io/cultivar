import subprocess
from abc import ABC, abstractmethod


class Runner(ABC):
    name: str

    @abstractmethod
    def run(
        self,
        intent: str,
        variant: str,
        max_turns: int = 10,
        cwd: str | None = None,
        docs_context: str = "",
        timeout: int = 90,
    ) -> dict:
        """Run intent, return conversation as dict.

        If cwd is provided, the CLI subprocess is invoked there — the agent writes
        any files it generates into that directory so the orchestrator can capture them.

        `docs_context` is the resolved with-docs prefix (typically built by
        ``evals.framework.grader.load_runner_refs``) and is only non-empty when
        ``variant == "with-docs"``. Runners prepend it to the intent prompt.

        `timeout` is the subprocess wall-clock budget in seconds.
        """
        ...

    @abstractmethod
    def variants(self) -> list[str]:
        """Return supported variants, e.g. ['with-skill', 'without-skill', 'with-docs']."""
        ...

    @abstractmethod
    def build_command(
        self, intent: str, variant: str, max_turns: int = 10, docs_context: str = ""
    ) -> tuple[list[str], str]:
        """Return (cmd, prompt) that would be executed. Used by --dry-run."""
        ...


def run_cli(cmd: list[str], timeout: int, **kwargs) -> tuple[str, str]:
    """Run a CLI command, return (stdout, stderr). Handles timeout gracefully."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return stdout, stderr
