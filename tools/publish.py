"""
Publish Toolkit
===============

The only path from the workspace to GitHub, and the only place a credential is
used to write.

The agent edits files in its worktree and cannot push: `git` is not on its shell
allowlist, its environment holds no token, and the worktree's remote carries no
credentials. To get a change out, it calls a tool here, which reads the change
set back out of the working tree and sends it through the GitHub API.

Both publishing tools pause for approval before anything is created.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from agno.run import RunContext
from agno.tools import Toolkit
from agno.utils.log import log_info, log_warning

from tools._offload import async_twins
from tools.github_repo import MAX_FILE_BYTES, GitHubRepoTools
from workspace.manager import WorkspaceManager, WorkspaceSession

PUBLISH_TOOLS = ["open_pull_request", "update_pull_request"]


class PublishTools(Toolkit):
    """Turn the workspace's changes into a pull request."""

    def __init__(
        self,
        manager: WorkspaceManager,
        github: GitHubRepoTools,
        require_confirmation: bool = True,
        **kwargs: Any,
    ):
        self.manager = manager
        self.github = github

        tool_names = ["review_changes", "open_pull_request", "update_pull_request"]
        super().__init__(
            name="publish",
            tools=[getattr(self, name) for name in tool_names],
            async_tools=async_twins(self, tool_names),
            requires_confirmation_tools=PUBLISH_TOOLS if require_confirmation else [],
            **kwargs,
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------
    @staticmethod
    def _session_id(run_context: RunContext) -> str:
        return getattr(run_context, "session_id", None) or "default"

    def _session(self, run_context: RunContext) -> WorkspaceSession:
        session = self.manager.get(self._session_id(run_context))
        if session is None:
            raise LookupError("No workspace is open. Call open_workspace first.")
        return session

    def _change_set(self, session: WorkspaceSession) -> list[dict[str, Any]]:
        """Read the working tree's changes into something the GitHub API can commit.

        Text goes as content. Binary goes as a blob created ahead of the commit. A
        rename becomes a delete plus an add, which is what git records anyway.
        """
        changes: list[dict[str, Any]] = []
        for entry in self.manager.status(session):
            path, status = entry["path"], entry["status"]

            if status == "renamed" and entry.get("from"):
                changes.append({"path": entry["from"], "delete": True})
            if status == "deleted":
                changes.append({"path": path, "delete": True})
                continue

            file_path = session.path / path
            if not file_path.is_file():
                continue
            raw = file_path.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes; publish it by hand.")

            change: dict[str, Any] = {"path": path}
            try:
                change["content"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                log_info(f"{path} is binary; uploading it as a blob")
                change["sha"] = self.github.create_blob(session.repo, base64.b64encode(raw).decode("ascii"))
            if file_path.stat().st_mode & 0o111:
                change["mode"] = "100755"
            changes.append(change)
        return changes

    @staticmethod
    def _provenance(run_context: RunContext) -> str:
        """Who asked for this, appended to the pull request body.

        Every pull request is authored by the account behind the token, so the body is
        the only place the actual requester survives. The Slack interface resolves the
        identity into run metadata when `resolve_user_identity` is on; the model never
        supplies it, so it cannot be talked into naming someone else.
        """
        metadata = getattr(run_context, "metadata", None) or {}
        name = metadata.get("user_name")
        if not name:
            return ""
        who = f"@{name}" if not str(name).startswith("@") else str(name)
        return f"\n\n---\nRequested by {who} in Slack, and opened by PatchPilot on their behalf."

    @staticmethod
    def _error(exc: Exception) -> str:
        log_warning(f"publish: {exc}")
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # -----------------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------------
    def review_changes(self, run_context: RunContext) -> str:
        """Show exactly what would be published: every file, and whether it is added, changed or deleted.

        Call this before opening a pull request, and put the list in the thread so the
        person approving can see what they are approving.

        Returns:
            A JSON object with the workspace details and the change set.
        """
        try:
            session = self._session(run_context)
            changes = self.manager.status(session)
            return json.dumps(
                {
                    "workspace": session.to_dict(),
                    "changes": changes,
                    "diffstat": self.manager.diff(session, stat=True) or "(no changes)",
                },
                indent=2,
            )
        except Exception as exc:
            return self._error(exc)

    def open_pull_request(
        self,
        run_context: RunContext,
        title: str,
        body: str,
        base: str | None = None,
        branch: str | None = None,
        draft: bool | None = None,
    ) -> str:
        """Open a pull request from the changes in the workspace.

        The branch is cut from the exact commit the workspace was pinned to, so anything
        that moved on the base branch since then surfaces as a merge conflict instead of
        being quietly overwritten.

        Args:
            title: Pull request title. Follow the repository's convention.
            body: Description in markdown -- what changed, why, and what still needs checking.
            base: Branch to merge into. Defaults to the repository's default branch.
            branch: Name for the new branch. A prefix is added when it is missing.
            draft: Open as a draft. Defaults to the configured default.

        Returns:
            A JSON object with the pull request number and URL.
        """
        try:
            session = self._session(run_context)
            changes = self._change_set(session)
            base_branch = base or self.manager.default_branch(self.manager._mirror_path(session.repo))
            result = self.github.publish_new_pull_request(
                repo=session.repo,
                title=title,
                body=body + self._provenance(run_context),
                changes=changes,
                base_branch=base_branch,
                base_sha=session.base_sha,
                branch=branch,
                draft=draft,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return self._error(exc)

    def update_pull_request(self, run_context: RunContext, pr_number: int, message: str) -> str:
        """Push the workspace's current changes to a pull request this agent opened.

        Use this to act on review feedback: open the workspace on the pull request, make
        the change, then push it.

        Args:
            pr_number: The pull request number.
            message: The commit message.

        Returns:
            A JSON object with the new commit SHA and the files it touched.
        """
        try:
            session = self._session(run_context)
            changes = self._change_set(session)
            result = self.github.publish_commit_to_pull_request(
                repo=session.repo, pr_number=pr_number, changes=changes, message=message
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return self._error(exc)
