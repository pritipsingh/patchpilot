"""
Settings
========

Every knob the PR agent reads from the environment lives here.
"""

from functools import lru_cache
from os import getenv
from pathlib import Path

from agno.models.openai import OpenAIResponses
from dotenv import load_dotenv

from app.credentials import GitHubCredentials

# Load .env from the project root, whatever the working directory is. Real
# environment variables win, so a container's config is never overridden by a
# stray .env that got baked into the image.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# The repos the agent is allowed to touch. Anything outside this list is
# refused by the toolkit before a request reaches GitHub. First one wins when the
# agent has to guess which repository an ask is about.
DEFAULT_REPOS = "agno-agi/agno-os,agno-agi/agno"

# Every branch the agent pushes starts with this. The toolkit refuses to write
# to a branch without it, which is what keeps the agent off main and off the
# branches humans are working on.
DEFAULT_BRANCH_PREFIX = "pr-agent/"


def default_model() -> OpenAIResponses:
    """Fresh model instance per agent -- avoids shared-state footguns."""
    return OpenAIResponses(id=getenv("MODEL_ID", "gpt-5.6"))


@lru_cache(maxsize=1)
def allowed_repos() -> list[str]:
    """Repo allowlist, as `owner/name` strings."""
    raw = getenv("GITHUB_REPOS", DEFAULT_REPOS)
    return [repo.strip() for repo in raw.split(",") if repo.strip()]


def branch_prefix() -> str:
    prefix = getenv("PR_BRANCH_PREFIX", DEFAULT_BRANCH_PREFIX)
    return prefix if prefix.endswith("/") else f"{prefix}/"


def open_prs_as_draft() -> bool:
    return getenv("PR_DRAFT_BY_DEFAULT", "True").lower() in ("1", "true", "yes")


def require_confirmation() -> bool:
    """Ask in Slack before any write reaches GitHub. Leave this on."""
    return getenv("REQUIRE_CONFIRMATION", "True").lower() in ("1", "true", "yes")


def github_token() -> str:
    return getenv("GITHUB_ACCESS_TOKEN", "")


@lru_cache(maxsize=1)
def github_credentials() -> GitHubCredentials:
    """Who the agent is on GitHub. A GitHub App when configured, a token otherwise."""
    installation = getenv("GITHUB_APP_INSTALLATION_ID", "").strip()
    return GitHubCredentials(
        app_id=getenv("GITHUB_APP_ID", ""),
        private_key=getenv("GITHUB_APP_PRIVATE_KEY", "") or getenv("GITHUB_APP_PRIVATE_KEY_FILE", ""),
        installation_id=int(installation) if installation else None,
        access_token=github_token(),
        base_url=getenv("GITHUB_BASE_URL") or None,
    )


def slack_bot_token() -> str:
    return getenv("SLACK_BOT_TOKEN", "")


def slack_signing_secret() -> str:
    return getenv("SLACK_SIGNING_SECRET", "")


def slack_streaming() -> bool:
    """Stream the reply token by token instead of posting it once it is done.

    Off by default: streaming uses Slack's `chat.startStream`, part of the AI
    Assistant APIs, which only works for an app configured as an agent (an
    `assistant_view` in the manifest, and the `assistant_thread_started` event).
    Without that it fails with `team_not_found` and the reply never arrives.
    Turn it on once the app is set up as an agent.
    """
    return getenv("SLACK_STREAMING", "False").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
def workspace_root() -> Path:
    """Where mirrors and per-session worktrees live.

    On a container this needs to be a persistent volume: a cold start otherwise
    re-clones every repository.
    """
    return Path(getenv("WORKSPACE_ROOT", "~/.agno-pr-agent")).expanduser()


def workspace_max_age_hours() -> float:
    """How long an idle checkout survives before it is reclaimed."""
    return float(getenv("WORKSPACE_MAX_AGE_HOURS", "12"))


def agntid_settings():  # type: ignore[no-untyped-def]
    """Optional runtime path. A missing URL is an error, never an implicit bypass."""
    if getenv("AGNTID_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return None
    from app.agntid_bridge import BridgeSettings

    if not require_confirmation():
        raise ValueError("AgntID mode requires REQUIRE_CONFIRMATION=true")
    return BridgeSettings(
        url=getenv("AGNTID_MCP_URL", ""),
        tool_prefix=getenv("AGNTID_TOOL_PREFIX", "pr_agent_"),
        access_token=getenv("AGNTID_ACCESS_TOKEN") or None,
        timeout=float(getenv("AGNTID_CALL_TIMEOUT", "300")),
        executor_host=getenv("AGNTID_EXECUTOR_HOST", "127.0.0.1"),
        executor_port=int(getenv("AGNTID_EXECUTOR_PORT", "8091")),
    )
