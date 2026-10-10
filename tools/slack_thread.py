"""
Slack Thread Toolkit
====================

The Slack interface maps one thread to one session, so the agent already
remembers everything said to it in a thread. What it cannot see is the part of
the thread that happened before anyone mentioned it -- the report, the stack
trace, the back-and-forth that is the actual context for the ask.

`read_slack_thread` fetches the whole thread. It takes no arguments: the
channel and thread timestamp come from the session id the Slack interface
builds, which is `{entity_id}:{channel_id}:{thread_ts}`.
"""

from __future__ import annotations

import json
import re
from os import getenv
from typing import Any

from agno.run import RunContext
from agno.tools import Toolkit
from agno.utils.log import log_debug

from tools._offload import async_twins

CHANNEL_RE = re.compile(r"^[CDG][A-Z0-9]+$")
THREAD_TS_RE = re.compile(r"^\d+\.\d+$")


def split_session_id(session_id: str | None) -> tuple[str | None, str | None]:
    """Pull the channel and thread timestamp out of a Slack-built session id."""
    if not session_id:
        return None, None
    parts = session_id.split(":")
    if len(parts) < 3:
        return None, None
    channel, thread_ts = parts[-2], parts[-1]
    if not CHANNEL_RE.match(channel) or not THREAD_TS_RE.match(thread_ts):
        return None, None
    return channel, thread_ts


class SlackThreadTools(Toolkit):
    """Read the Slack thread the current run is happening in."""

    def __init__(self, token: str | None = None, message_limit: int = 50, **kwargs: Any):
        self.token = token or getenv("SLACK_BOT_TOKEN") or getenv("SLACK_TOKEN")
        if not self.token:
            raise ValueError("SLACK_BOT_TOKEN is not set")
        self.message_limit = message_limit
        self._slack: Any = None

        super().__init__(
            name="slack_thread",
            tools=[self.read_slack_thread],
            async_tools=async_twins(self, ["read_slack_thread"]),
            **kwargs,
        )

    @property
    def slack(self) -> Any:
        """Agno's SlackTools, used here purely as a client for conversations.replies."""
        if self._slack is None:
            from agno.tools.slack import SlackTools

            self._slack = SlackTools(
                token=self.token,
                enable_get_thread=True,
                enable_send_message=False,
                enable_send_message_thread=False,
                enable_list_channels=False,
                enable_get_channel_history=False,
                enable_upload_file=False,
                enable_download_file=False,
                thread_message_limit=self.message_limit,
            )
        return self._slack

    def read_slack_thread(self, run_context: RunContext, limit: int = 50) -> str:
        """Read every message in the Slack thread this conversation is happening in.

        Use this whenever the ask leans on context you have not been told directly --
        "make a PR for this", "fix what Sam described above", a pasted error, a link
        someone shared earlier in the thread.

        Args:
            limit: Maximum number of messages to fetch. Defaults to 50.

        Returns:
            A JSON object with the thread's messages, oldest first.
        """
        channel, thread_ts = split_session_id(getattr(run_context, "session_id", None))
        if not channel or not thread_ts:
            return json.dumps(
                {
                    "error": "Not running inside a Slack thread, so there is no thread to read.",
                    "session_id": getattr(run_context, "session_id", None),
                }
            )
        log_debug(f"Reading Slack thread {channel}/{thread_ts}")
        return self.slack.get_thread(channel=channel, thread_ts=thread_ts, limit=limit)
