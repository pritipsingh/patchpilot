"""
AgentOS Entrypoint
==================

Serves the PR agent over HTTP, and over Slack when a bot token and signing
secret are set.
"""

from contextlib import AsyncExitStack, asynccontextmanager
from os import getenv

from agno.os import AgentOS
from agno.utils.log import log_info, log_warning

from agents.pr_agent import agntid_bridge, credentials, pr_agent, workspace_manager
from app.settings import allowed_repos, branch_prefix, slack_bot_token, slack_signing_secret, slack_streaming
from db import get_db

runtime_env = getenv("RUNTIME_ENV", "dev")

# ---------------------------------------------------------------------------
# Interfaces
# - The agent shows up in Slack once both Slack env vars are set.
# - Slack maps one thread to one session, so each thread is its own
#   conversation with its own history.
# ---------------------------------------------------------------------------
interfaces: list = []
if slack_bot_token() and slack_signing_secret():
    from agno.os.interfaces.slack import Slack

    interfaces.append(
        Slack(
            agent=pr_agent,
            streaming=slack_streaming(),
            token=slack_bot_token(),
            signing_secret=slack_signing_secret(),
            resolve_user_identity=True,
            loading_text="Reading the thread...",
            suggested_prompts=[
                {
                    "title": "Review a PR",
                    "message": "Review https://github.com/agno-agi/agno/pull/1 and flag anything risky.",
                },
                {"title": "Explain a change", "message": "What does this PR actually change, in two sentences?"},
                {"title": "Open a PR", "message": "Open a draft PR fixing the typo described above."},
            ],
        )
    )
    log_info("Slack interface enabled at /slack/events")
else:
    log_warning("SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET not set -- Slack interface is off")

log_info(f"Repos: {', '.join(allowed_repos())} | branch prefix: {branch_prefix()}")
log_info(f"GitHub identity: {credentials.describe()}")


# ---------------------------------------------------------------------------
# Lifespan
# - Checkouts live in memory, so a restart orphans whatever is on the volume.
#   Sweeping on startup keeps a long-lived volume from filling up with them.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    reclaimed = workspace_manager.gc()
    log_info(f"Workspace root: {workspace_manager.root} ({reclaimed} stale checkout(s) reclaimed)")
    if getenv("PREWARM_REPOS", "True").lower() in ("1", "true", "yes"):
        workspace_manager.prewarm(allowed_repos())
    async with AsyncExitStack() as stack:
        if agntid_bridge is not None:
            from app.agntid_executor import serve_executor

            await stack.enter_async_context(serve_executor(agntid_bridge))
            log_info("AgntID repository tool enforcement enabled; callback executor ready")
        try:
            yield
        finally:
            log_info("Shutting down")


# ---------------------------------------------------------------------------
# Create AgentOS
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="PR Agent OS",
    description="A Slack-native agent that reads and opens pull requests.",
    agents=[pr_agent],
    interfaces=interfaces,
    db=get_db(),
    lifespan=lifespan,
    tracing=True,
    # Local dev serves without auth; production requires a JWT verification key.
    authorization=runtime_env != "dev",
)
app = agent_os.get_app()


if __name__ == "__main__":
    agent_os.serve(app="app.main:app", port=int(getenv("PORT", "8000")), reload=runtime_env == "dev")
