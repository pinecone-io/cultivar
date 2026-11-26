import os
from collections.abc import Mapping
from pathlib import Path

PASSTHROUGH_VAR = "CULTIVAR_SANDBOX_ENV_PASSTHROUGH"
TOKEN_VAR = "ANTHROPIC_IDENTITY_TOKEN"
TOKEN_FILE_VAR = "ANTHROPIC_IDENTITY_TOKEN_FILE"


class SandboxEnvError(RuntimeError):
    pass


def sandbox_passthrough_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the variables named in CULTIVAR_SANDBOX_ENV_PASSTHROUGH from this process's env.

    A passed-through ANTHROPIC_IDENTITY_TOKEN_FILE names a path on this machine that
    the sandbox doesn't have, so its contents are sent as ANTHROPIC_IDENTITY_TOKEN
    instead, replacing any passed-through literal token, as the Anthropic SDK does.
    The file is read at every call, so each sandbox gets the token as it stands when
    that sandbox is created.
    """
    env = os.environ if environ is None else environ
    names = [n.strip() for n in env.get(PASSTHROUGH_VAR, "").split(",") if n.strip()]
    passed = {n: env[n] for n in names if env.get(n)}

    token_file = passed.pop(TOKEN_FILE_VAR, None)
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as e:
            raise SandboxEnvError(f"can't read {TOKEN_FILE_VAR} ({token_file}): {e.strerror}") from None
        if not token:
            raise SandboxEnvError(f"{TOKEN_FILE_VAR} ({token_file}) is empty")
        passed[TOKEN_VAR] = token
    return passed
