import os
from collections.abc import Mapping

FEDERATION_REQUIRED = ("ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID")
IDENTITY_TOKEN_VARS = ("ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE")
FEDERATION = "workload identity federation"

ACCEPTED_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    f"{FEDERATION}: ANTHROPIC_FEDERATION_RULE_ID + ANTHROPIC_ORGANIZATION_ID "
    "+ ANTHROPIC_IDENTITY_TOKEN (or ANTHROPIC_IDENTITY_TOKEN_FILE)",
)


def anthropic_credential_source(environ: Mapping[str, str] | None = None) -> str | None:
    """Name the credential a zero-argument `Anthropic()` client would use, or None.

    Follows the SDK's env order, so a static key wins over federation when both are set.
    """
    env = os.environ if environ is None else environ
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if env.get(name):
            return name
    if all(env.get(n) for n in FEDERATION_REQUIRED) and any(env.get(n) for n in IDENTITY_TOKEN_VARS):
        return FEDERATION
    return None


def missing_credentials_help() -> str:
    options = "\n".join(f"  - {c}" for c in ACCEPTED_CREDENTIALS)
    return (
        "No Anthropic credentials found. Set one of:\n"
        f"{options}\n"
        "Put them in a .env file in your current directory (it's auto-loaded) or export them in your shell."
    )
