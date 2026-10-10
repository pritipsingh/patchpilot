"""
GitHub Toolkit
==============

Repo-scoped GitHub access for the PR agent.

Two guardrails sit under every call:

- The repo allowlist. A repo that is not on it is refused before a request
  reaches GitHub, however the agent phrases the ask.
- The branch prefix. Writes only ever land on a branch that starts with it, so
  the agent cannot commit to a default branch or to a branch a human owns.

Writes are a single commit built through the git data API, so a five-file
change arrives as one commit rather than five.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agno.tools import Toolkit
from agno.utils.log import log_debug, log_info, log_warning

from app.credentials import GitHubCredentials
from tools._offload import async_twins

# A change set larger than this is a sign the agent is guessing rather than
# making a considered edit.
MAX_FILES_PER_COMMIT = 25
MAX_FILE_BYTES = 400_000
MAX_DIFF_CHARS = 60_000
MAX_PATCH_CHARS_PER_FILE = 12_000

WRITE_TOOLS = ["comment_on_pull_request"]


def normalize_repo(value: str) -> str:
    """Turn anything that names a repo into `owner/name`.

    Accepts `owner/name`, a clone URL, or any github.com URL that carries the
    repo in its path (a PR link, a blob link, an issue link).
    """
    text = (value or "").strip()
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text)
    text = re.sub(r"^git@github\.com:", "", text)
    if text.endswith(".git"):
        text = text[: -len(".git")]
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Not a repository reference: {value!r}. Use owner/name.")
    return f"{parts[0]}/{parts[1]}"


def slugify(text: str, max_length: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_length].strip("-") or "change"


class GitHubRepoTools(Toolkit):
    """Read pull requests and repository files; open and update pull requests."""

    def __init__(
        self,
        credentials: GitHubCredentials,
        repos: list[str] | None = None,
        branch_prefix: str = "pr-agent/",
        draft_by_default: bool = True,
        require_confirmation: bool = True,
        cache_results: bool = True,
        cache_ttl: int = 600,
        **kwargs: Any,
    ):
        self.credentials = credentials
        self.allowed_repos = [normalize_repo(repo) for repo in (repos or [])]
        self.branch_prefix = branch_prefix if branch_prefix.endswith("/") else f"{branch_prefix}/"
        self.draft_by_default = draft_by_default

        tool_names = [
            "list_allowed_repositories",
            "get_pull_request",
            "get_pull_request_diff",
            "list_pull_requests",
            "comment_on_pull_request",
        ]
        tools: list[Callable[..., Any]] = [getattr(self, name) for name in tool_names]

        super().__init__(
            name="github_repo",
            tools=tools,
            async_tools=async_twins(self, tool_names),
            requires_confirmation_tools=WRITE_TOOLS if require_confirmation else [],
            cache_results=cache_results,
            cache_ttl=cache_ttl,
            **kwargs,
        )

        # Reads of a pull request repeat constantly within a thread and are worth
        # caching. A cached write is a different thing entirely: the second identical
        # comment would return the first one's result and never be posted.
        for name in WRITE_TOOLS:
            function = self.functions.get(name)
            if function is not None:
                function.cache_results = False

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------
    def _repo(self, repo: str) -> Any:
        """Resolve a repo reference, refusing anything off the allowlist.

        The client comes from the credentials layer, which under a GitHub App means
        the installation that actually covers this repository.
        """
        full_name = normalize_repo(repo)
        allowed = {name.lower() for name in self.allowed_repos}
        if allowed and full_name.lower() not in allowed:
            raise PermissionError(
                f"{full_name} is not on the allowlist. Allowed: {', '.join(self.allowed_repos)}. "
                "Add it to GITHUB_REPOS to work on it."
            )
        return self.credentials.client_for(full_name).get_repo(full_name)

    def _check_branch(self, branch: str) -> None:
        if not branch.startswith(self.branch_prefix):
            raise PermissionError(
                f"Refusing to write to '{branch}': this agent only writes to branches "
                f"starting with '{self.branch_prefix}'."
            )

    @staticmethod
    def _existing_modes(repo: Any, tree_sha: str) -> dict[str, str]:
        """Map path -> file mode for the tree, so an edit keeps a script executable."""
        try:
            tree = repo.get_git_tree(tree_sha, recursive=True)
            return {item.path: item.mode for item in tree.tree if item.type == "blob"}
        except Exception as exc:
            log_debug(f"Could not read tree modes ({exc}); defaulting to 100644")
            return {}

    def _commit(self, repo: Any, branch: str, changes: list[dict[str, Any]], message: str) -> dict[str, Any]:
        """Write every change as one commit on the tip of `branch`."""
        from github import InputGitTreeElement

        ref = repo.get_git_ref(f"heads/{branch}")
        parent = repo.get_git_commit(ref.object.sha)
        modes = self._existing_modes(repo, parent.tree.sha)

        elements = []
        for change in changes:
            mode = change.get("mode") or modes.get(change["path"], "100644")
            if change.get("delete"):
                # sha=None is how the git data API spells "remove this path".
                elements.append(InputGitTreeElement(path=change["path"], mode=mode, type="blob", sha=None))
            elif change.get("sha"):
                # A blob created ahead of time -- that is how binary content gets in.
                elements.append(InputGitTreeElement(path=change["path"], mode=mode, type="blob", sha=change["sha"]))
            else:
                elements.append(
                    InputGitTreeElement(path=change["path"], mode=mode, type="blob", content=change["content"])
                )

        tree = repo.create_git_tree(elements, repo.get_git_tree(parent.tree.sha))
        commit = repo.create_git_commit(message, tree, [parent])
        ref.edit(commit.sha)
        log_info(f"Committed {len(changes)} file(s) to {repo.full_name}@{branch} ({commit.sha[:7]})")
        return {"sha": commit.sha, "files": [change["path"] for change in changes]}

    @staticmethod
    def _error(exc: Exception) -> str:
        log_warning(f"github_repo: {exc}")
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # -----------------------------------------------------------------------
    # Read
    # -----------------------------------------------------------------------
    def list_allowed_repositories(self) -> str:
        """List the repositories this agent is allowed to read and open pull requests against.

        Returns:
            A JSON array of `owner/name` strings.
        """
        return json.dumps({"repositories": self.allowed_repos, "branch_prefix": self.branch_prefix})

    def get_pull_request(self, repo: str, pr_number: int) -> str:
        """Get a pull request: its title, description, state, changed files and review discussion.

        Args:
            repo: Repository as `owner/name`, or any GitHub URL that names it.
            pr_number: The pull request number.

        Returns:
            A JSON object describing the pull request.
        """
        try:
            repository = self._repo(repo)
            pr = repository.get_pull(pr_number)
            files = [
                {
                    "filename": file.filename,
                    "status": file.status,
                    "additions": file.additions,
                    "deletions": file.deletions,
                }
                for file in pr.get_files()
            ]
            comments = [
                {"author": comment.user.login if comment.user else None, "body": comment.body}
                for comment in pr.get_issue_comments()
            ]
            review_comments = [
                {
                    "author": comment.user.login if comment.user else None,
                    "path": comment.path,
                    "body": comment.body,
                }
                for comment in pr.get_review_comments()
            ]
            return json.dumps(
                {
                    "repo": repository.full_name,
                    "number": pr.number,
                    "title": pr.title,
                    "body": pr.body,
                    "state": pr.state,
                    "draft": pr.draft,
                    "author": pr.user.login if pr.user else None,
                    "url": pr.html_url,
                    "base": pr.base.ref,
                    "head": pr.head.ref,
                    "merged": pr.merged,
                    "mergeable": pr.mergeable,
                    "changed_files": files,
                    "comments": comments,
                    "review_comments": review_comments,
                },
                indent=2,
            )
        except Exception as exc:
            return self._error(exc)

    def get_pull_request_diff(self, repo: str, pr_number: int) -> str:
        """Get the patch (the actual line-level diff) for every file in a pull request.

        Args:
            repo: Repository as `owner/name`, or any GitHub URL that names it.
            pr_number: The pull request number.

        Returns:
            A JSON object with one patch per file. Long diffs are truncated, and the
            response says so when that happens.
        """
        try:
            repository = self._repo(repo)
            pr = repository.get_pull(pr_number)
            patches: list[dict[str, Any]] = []
            total = 0
            truncated = False
            for file in pr.get_files():
                patch = file.patch or ""
                if len(patch) > MAX_PATCH_CHARS_PER_FILE:
                    patch = patch[:MAX_PATCH_CHARS_PER_FILE] + "\n... [patch truncated]"
                    truncated = True
                if total + len(patch) > MAX_DIFF_CHARS:
                    truncated = True
                    break
                total += len(patch)
                patches.append({"filename": file.filename, "status": file.status, "patch": patch})
            return json.dumps(
                {"repo": repository.full_name, "number": pr.number, "files": patches, "truncated": truncated},
                indent=2,
            )
        except Exception as exc:
            return self._error(exc)

    def list_pull_requests(self, repo: str, state: str = "open", limit: int = 20) -> str:
        """List pull requests in a repository.

        Args:
            repo: Repository as `owner/name`, or any GitHub URL that names it.
            state: One of `open`, `closed`, `all`. Defaults to `open`.
            limit: Maximum number of pull requests to return. Defaults to 20.

        Returns:
            A JSON array of pull request summaries.
        """
        try:
            repository = self._repo(repo)
            pulls = repository.get_pulls(state=state, sort="updated", direction="desc")
            items = []
            for pr in pulls[:limit]:
                items.append(
                    {
                        "number": pr.number,
                        "title": pr.title,
                        "author": pr.user.login if pr.user else None,
                        "state": pr.state,
                        "draft": pr.draft,
                        "url": pr.html_url,
                        "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                    }
                )
            return json.dumps({"repo": repository.full_name, "pull_requests": items}, indent=2)
        except Exception as exc:
            return self._error(exc)

    def publish_new_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        changes: list[dict[str, Any]],
        base_branch: str,
        base_sha: str,
        branch: str | None = None,
        draft: bool | None = None,
    ) -> dict[str, Any]:
        """Create a branch, commit the change set, and open the pull request.

        Internal: the change set comes from the workspace, not from the model. The
        branch is cut from `base_sha` -- the exact commit the workspace was pinned to --
        so a file that moved on the base branch since then shows up as a merge conflict
        for a human to resolve, rather than being silently reverted by our whole-file write.
        """
        repository = self._repo(repo)
        if not changes:
            raise ValueError("Nothing to publish: the workspace has no changes.")
        if len(changes) > MAX_FILES_PER_COMMIT:
            raise ValueError(f"Too many files in one change set ({len(changes)} > {MAX_FILES_PER_COMMIT}).")

        head = branch if branch else f"{self.branch_prefix}{slugify(title)}-{uuid4().hex[:6]}"
        if not head.startswith(self.branch_prefix):
            head = f"{self.branch_prefix}{head}"
        self._check_branch(head)

        try:
            repository.get_git_ref(f"heads/{head}")
            log_debug(f"Branch {head} already exists, committing onto it")
        except Exception:
            repository.create_git_ref(ref=f"refs/heads/{head}", sha=base_sha)
            log_info(f"Created branch {head} at {base_sha[:8]}")

        commit = self._commit(repository, head, changes, title.strip() or f"Update {len(changes)} file(s)")

        pr = repository.create_pull(
            title=title,
            body=body,
            head=head,
            base=base_branch,
            draft=self.draft_by_default if draft is None else draft,
        )
        log_info(f"Opened {repository.full_name}#{pr.number}: {pr.html_url}")
        return {
            "repo": repository.full_name,
            "number": pr.number,
            "url": pr.html_url,
            "branch": head,
            "base": base_branch,
            "base_sha": base_sha,
            "draft": pr.draft,
            "commit": commit["sha"],
            "files": commit["files"],
        }

    def publish_commit_to_pull_request(
        self, repo: str, pr_number: int, changes: list[dict[str, Any]], message: str
    ) -> dict[str, Any]:
        """Add one commit to a pull request this agent opened.

        Internal. Refuses a fork, and refuses any branch outside the agent's prefix, so
        it can never push to a branch a human owns.
        """
        repository = self._repo(repo)
        if not changes:
            raise ValueError("Nothing to publish: the workspace has no changes.")

        pr = repository.get_pull(pr_number)
        if pr.head.repo is None or pr.head.repo.full_name != repository.full_name:
            raise PermissionError("This pull request comes from a fork; this agent cannot push to it.")
        self._check_branch(pr.head.ref)

        commit = self._commit(repository, pr.head.ref, changes, message)
        return {
            "repo": repository.full_name,
            "number": pr.number,
            "url": pr.html_url,
            "branch": pr.head.ref,
            "commit": commit["sha"],
            "files": commit["files"],
        }

    def create_blob(self, repo: str, content_base64: str) -> str:
        """Create a blob for binary content and return its SHA. Internal."""
        return self._repo(repo).create_git_blob(content_base64, "base64").sha

    def comment_on_pull_request(self, repo: str, pr_number: int, body: str) -> str:
        """Post a comment on a pull request.

        Args:
            repo: Repository as `owner/name`, or any GitHub URL that names it.
            pr_number: The pull request number.
            body: The comment, in markdown.

        Returns:
            A JSON object with the comment URL.
        """
        try:
            repository = self._repo(repo)
            issue = repository.get_issue(pr_number)
            comment = issue.create_comment(body)
            return json.dumps(
                {
                    "repo": repository.full_name,
                    "number": pr_number,
                    "comment_url": comment.html_url,
                    "posted_at": datetime.now(UTC).isoformat(),
                }
            )
        except Exception as exc:
            return self._error(exc)
