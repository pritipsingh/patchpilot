"""
Workspace Toolkit
=================

The agent's hands inside the checkout: read, glob, grep, write, edit, run
commands, and read git history -- all scoped to one session's worktree.

Two things sit on top of Agno's `Workspace` toolkit, which already enforces the
path boundary:

- Commands are checked against an allowlist before they run, including each
  segment of a `bash -c` script.
- Commands run with a scrubbed environment. The process this agent runs in holds
  the GitHub, OpenAI and Slack credentials; the agent's shell sees none of them.

Every tool resolves its worktree from `run_context.session_id`, so two Slack
threads working on two different branches never share a checkout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from agno.run import RunContext
from agno.tools import Toolkit
from agno.tools.workspace import Workspace
from agno.utils.log import log_info, log_warning

from tools._offload import async_twins
from workspace.manager import GitError, WorkspaceManager, WorkspaceSession

# Commands the agent may run. Everything else is refused, including anything that
# could reach the network or publish -- curl, ssh, gh, git push.
DEFAULT_ALLOWED_COMMANDS = (
    "python,python3,pytest,ruff,mypy,black,isort,uv,"
    "ls,cat,head,tail,wc,grep,rg,find,sed,awk,sort,uniq,cut,tr,diff,echo,pwd,basename,dirname,"
    "make,node,npm,pnpm,yarn,npx,corepack,tsc,eslint,prettier,jest,vitest,go,cargo,"
    "bash,sh"
)
SHELL_WRAPPERS = {"bash", "sh", "zsh"}
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Never listed, never readable by name, whatever the agent asks for.
EXCLUDE_PATTERNS = [
    ".git",
    ".env",
    ".env.*",
    "!.env.example",
    "!example.env",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".netrc",
    "node_modules",
    ".venv",
    ".venvs",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
]


class CommandRefused(Exception):
    """The command is not on the allowlist."""


def scratch_dirs(root: Path) -> tuple[Path, Path]:
    """A HOME and a TMPDIR for the agent's commands, beside the checkout, never inside it.

    Inside the checkout, everything a tool writes to `~` or `$TMPDIR` -- a pytest cache,
    a pip log -- would show up as an untracked file and ride along into the pull request.
    """
    home = root.parent / ".home"
    tmp = root.parent / ".tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return home, tmp


def shared_cache_dir() -> Path:
    """One package cache for every session, on the volume.

    Per-session caches would re-download a repository's whole dependency tree for
    each thread, and lose it when the checkout is reclaimed. This sits next to the
    mirrors instead, so the second install of a repo is fast.
    """
    root = Path(os.environ.get("WORKSPACE_ROOT", "~/.patchpilot")).expanduser()
    caches = root / "caches"
    caches.mkdir(parents=True, exist_ok=True)
    return caches


def scrubbed_env(root: Path, allowed_commands: set[str]) -> dict[str, str]:
    """A minimal environment: no API keys, no tokens, no home directory secrets."""
    home, tmp = scratch_dirs(root)
    caches = shared_cache_dir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # A HOME of our own, so ~/.netrc, ~/.gitconfig and ~/.ssh from the host
        # are out of reach.
        "HOME": str(home),
        "TMPDIR": str(tmp),
        # A repository that pins its package manager ("packageManager": "pnpm@10.34.1")
        # makes corepack fetch that exact version, and corepack asks before downloading.
        # There is no terminal here to answer it, so without this the install dies and
        # every one of the repo's own checks -- jest, tsc, prettier, eslint -- is
        # unavailable, since they are all devDependencies.
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "COREPACK_HOME": str(caches / "corepack"),
        "PNPM_HOME": str(caches / "pnpm"),
        "npm_config_store_dir": str(caches / "pnpm-store"),
        "npm_config_cache": str(caches / "npm"),
        "XDG_CACHE_HOME": str(caches / "xdg"),
        "UV_CACHE_DIR": str(caches / "uv"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NO_COLOR": "1",
        "CI": "1",
    }


def validate_command(args: list[str], allowed: set[str]) -> None:
    """Refuse anything not on the allowlist, looking inside `bash -c` too."""
    if not args:
        raise CommandRefused("No command given.")

    head = Path(args[0]).name
    if head not in allowed:
        raise CommandRefused(f"'{head}' is not an allowed command. Allowed: {', '.join(sorted(allowed))}")

    if head not in SHELL_WRAPPERS:
        return

    if len(args) < 3 or args[1] != "-c":
        raise CommandRefused("A shell must be invoked as ['bash', '-c', '<script>'].")

    script = args[2]
    if "`" in script or "$(" in script:
        raise CommandRefused("Command substitution is not allowed; run the command directly instead.")

    for segment in SEGMENT_SPLIT.split(script):
        try:
            tokens = shlex.split(segment)
        except ValueError as exc:
            raise CommandRefused(f"Could not parse the command: {exc}") from exc

        # Skip leading VAR=value assignments.
        while tokens and ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue

        name = Path(tokens[0]).name
        if name not in allowed:
            raise CommandRefused(f"'{name}' is not an allowed command. Allowed: {', '.join(sorted(allowed))}")

        for index, token in enumerate(tokens):
            if token in (">", ">>", "<") and index + 1 < len(tokens):
                target = tokens[index + 1]
                if target.startswith(("/", "~")) or ".." in target.split("/"):
                    raise CommandRefused(f"Refusing to redirect outside the workspace: {target}")


class SandboxedWorkspace(Workspace):
    """Agno's Workspace with an allowlisted, credential-free shell."""

    def __init__(self, *args: Any, allowed_commands: set[str], **kwargs: Any):
        self.allowed_commands = allowed_commands
        super().__init__(*args, **kwargs)

    def run_command(self, args: list[str], tail: int = 100, timeout: int = 180) -> str:
        try:
            validate_command(args, self.allowed_commands)
        except CommandRefused as exc:
            log_warning(f"Refused command {args}: {exc}")
            return f"Refused: {exc}"

        log_info(f"run_command: {args}")
        try:
            result = subprocess.run(
                args,
                cwd=str(self.root),
                env=scrubbed_env(self.root, self.allowed_commands),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout} seconds"
        except Exception as exc:
            return f"Error running command: {exc}"

        stream = result.stdout if result.returncode == 0 else (result.stderr or result.stdout)
        body = "\n".join(stream.splitlines()[-tail:])
        if result.returncode != 0:
            return f"Error (exit {result.returncode}):\n{body}"
        return body

    async def arun_command(self, args: list[str], tail: int = 100, timeout: int = 180) -> str:
        return await asyncio.to_thread(self.run_command, args, tail, timeout)


class RepoWorkspaceTools(Toolkit):
    """Session-scoped access to a checkout of an allowed repository."""

    def __init__(
        self,
        manager: WorkspaceManager,
        allowed_repos: list[str],
        allowed_commands: str | None = None,
        **kwargs: Any,
    ):
        self.manager = manager
        self.allowed_repos = allowed_repos
        raw: str = allowed_commands or os.environ.get("SHELL_ALLOWED_COMMANDS") or DEFAULT_ALLOWED_COMMANDS
        self.allowed_commands = {part.strip() for part in raw.split(",") if part.strip()}
        self._workspaces: dict[str, SandboxedWorkspace] = {}

        tool_names = [
            "open_workspace",
            "workspace_status",
            "list_files",
            "search_code",
            "read_file",
            "write_file",
            "edit_file",
            "delete_file",
            "run_command",
            "git_diff",
            "git_log",
            "discard_workspace",
        ]
        super().__init__(
            name="repo_workspace",
            tools=[getattr(self, name) for name in tool_names],
            # A clone, an install or a test run must not freeze the server.
            async_tools=async_twins(self, tool_names),
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

    def _workspace(self, run_context: RunContext) -> SandboxedWorkspace:
        session = self._session(run_context)
        key = self._session_id(run_context)
        cached = self._workspaces.get(key)
        if cached is None or cached.root != session.path:
            cached = SandboxedWorkspace(
                root=session.path,
                allowed=["read", "list", "search", "write", "edit", "delete", "shell"],
                exclude_patterns=EXCLUDE_PATTERNS,
                require_read_before_write=True,
                allowed_commands=self.allowed_commands,
            )
            self._workspaces[key] = cached
        return cached

    @staticmethod
    def _error(exc: Exception) -> str:
        log_warning(f"repo_workspace: {exc}")
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    # -----------------------------------------------------------------------
    # Workspace lifecycle
    # -----------------------------------------------------------------------
    def open_workspace(
        self,
        run_context: RunContext,
        repo: str,
        branch: str | None = None,
        pr_number: int | None = None,
    ) -> str:
        """Prepare a local repository checkout for reading, searching, or subsequent work.

        Required before search_code, read_file, and other workspace tools. Fetches
        repository data and creates a local checkout at an exact commit, then returns
        its file map and contribution rules. Opening alone does not edit source text,
        create commits, push to GitHub, or publish a pull request.

        This writes local checkout/cache files. The same repository and commit reuse
        the existing checkout with its edits preserved. Switching repository or commit
        replaces this session's old checkout; expired local sessions are also cleaned up.

        Args:
            repo: Repository as `owner/name`, or any GitHub URL that names it.
            branch: Branch to check out. Defaults to the repository's default branch.
            pr_number: Check out this pull request's head instead of a branch.

        Returns:
            The workspace details, a shallow repo map, and the repository's convention files.
        """
        try:
            from tools.github_repo import normalize_repo

            full_name = normalize_repo(repo)
            allowed = {name.lower() for name in self.allowed_repos}
            if allowed and full_name.lower() not in allowed:
                raise PermissionError(f"{full_name} is not on the allowlist. Allowed: {', '.join(self.allowed_repos)}.")

            self.manager.gc()
            session = self.manager.open(
                session_id=self._session_id(run_context),
                repo=full_name,
                ref=branch,
                pr_number=pr_number,
            )
            self._workspaces.pop(self._session_id(run_context), None)
            return self.manager.summary(session)
        except Exception as exc:
            return self._error(exc)

    def workspace_status(self, run_context: RunContext) -> str:
        """Show the open workspace and every change made to it so far.

        Returns:
            A JSON object with the checkout details and the list of changed files.
        """
        try:
            session = self._session(run_context)
            return json.dumps({"workspace": session.to_dict(), "changes": self.manager.status(session)}, indent=2)
        except Exception as exc:
            return self._error(exc)

    def discard_workspace(self, run_context: RunContext) -> str:
        """Throw away the workspace and every uncommitted change in it. Start over.

        Returns:
            A JSON object confirming what was discarded.
        """
        try:
            closed = self.manager.close(self._session_id(run_context))
            self._workspaces.pop(self._session_id(run_context), None)
            return json.dumps({"discarded": closed})
        except Exception as exc:
            return self._error(exc)

    # -----------------------------------------------------------------------
    # Files
    # -----------------------------------------------------------------------
    def list_files(
        self,
        run_context: RunContext,
        directory: str = ".",
        pattern: str | None = None,
        recursive: bool = False,
        max_depth: int = 3,
    ) -> str:
        """List files in the workspace, optionally matching a glob.

        Args:
            directory: Directory to list, relative to the repository root.
            pattern: Glob to match, e.g. `*.py` or `test_*.py`.
            recursive: Walk subdirectories. Defaults to False.
            max_depth: How deep to walk when recursive. Defaults to 3.

        Returns:
            The matching paths.
        """
        try:
            return self._workspace(run_context).list_files(directory, pattern, recursive, max_depth)
        except Exception as exc:
            return self._error(exc)

    def search_code(self, run_context: RunContext, query: str, directory: str = ".", limit: int = 20) -> str:
        """Search the workspace for a string, and get back the matching lines with their paths.

        This is a grep over the real checkout, so it sees the branch you are on --
        including a pull request's own changes.

        Args:
            query: The text to search for.
            directory: Directory to search in. Defaults to the whole repository.
            limit: Maximum matches to return. Defaults to 20.

        Returns:
            The matching lines with file paths and line numbers.
        """
        try:
            return self._workspace(run_context).search_content(query, directory, limit)
        except Exception as exc:
            return self._error(exc)

    def read_file(
        self,
        run_context: RunContext,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a file from the workspace, with line numbers.

        Read a file before you edit it: the line-numbered output gives you the exact
        substring to pass to edit_file.

        Args:
            path: Path relative to the repository root.
            start_line: First line to read. Reads the whole file when omitted.
            end_line: Last line to read.

        Returns:
            The file's contents.
        """
        try:
            return self._workspace(run_context).read_file(path, start_line, end_line)
        except Exception as exc:
            return self._error(exc)

    def write_file(self, run_context: RunContext, path: str, content: str, overwrite: bool = True) -> str:
        """Write a file in the workspace, creating it or replacing it whole.

        Use edit_file for a change to part of an existing file -- it is far harder to
        get wrong.

        Args:
            path: Path relative to the repository root.
            content: The complete file contents.
            overwrite: Replace the file if it exists. Defaults to True.

        Returns:
            Confirmation, or the reason the write was refused.
        """
        try:
            return self._workspace(run_context).write_file(path, content, overwrite)
        except Exception as exc:
            return self._error(exc)

    def edit_file(
        self,
        run_context: RunContext,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> str:
        """Replace an exact substring in a file. Read the file first.

        Args:
            path: Path relative to the repository root.
            old_str: The exact text to replace, copied from read_file's output.
            new_str: What to put in its place.
            replace_all: Replace every occurrence instead of requiring a unique match.

        Returns:
            Confirmation, or the reason the edit was refused.
        """
        try:
            return self._workspace(run_context).edit_file(path, old_str, new_str, replace_all)
        except Exception as exc:
            return self._error(exc)

    def delete_file(self, run_context: RunContext, path: str) -> str:
        """Delete a file from the workspace.

        Args:
            path: Path relative to the repository root.

        Returns:
            Confirmation, or the reason the delete was refused.
        """
        try:
            return self._workspace(run_context).delete_file(path)
        except Exception as exc:
            return self._error(exc)

    # -----------------------------------------------------------------------
    # Shell and git
    # -----------------------------------------------------------------------
    def run_command(self, run_context: RunContext, args: list[str], tail: int = 100, timeout: int = 180) -> str:
        """Run a command in the workspace -- the tests, the linter, the formatter.

        The command is a list of strings and is not passed through a shell. For pipes or
        redirection use `["bash", "-c", "..."]`. Only allowlisted programs run, the
        environment carries no credentials, and there is no network access to rely on.

        Args:
            args: The command as a list, e.g. `["pytest", "libs/agno/tests/unit", "-q"]`.
            tail: Trailing lines of output to return. Defaults to 100.
            timeout: Seconds before the command is killed. Defaults to 180.

        Returns:
            The command's output, or its error output and exit code.
        """
        try:
            return self._workspace(run_context).run_command(args, tail, timeout)
        except Exception as exc:
            return self._error(exc)

    def git_diff(self, run_context: RunContext, path: str | None = None, stat: bool = False) -> str:
        """Show what you have changed, as a diff against the commit the workspace is pinned to.

        Args:
            path: Limit the diff to this path. Defaults to everything.
            stat: Return a summary of changed files instead of the full diff.

        Returns:
            The diff, or `(no changes)`.
        """
        try:
            session = self._session(run_context)
            diff = self.manager.diff(session, path, stat)
            return diff or "(no changes)"
        except (GitError, LookupError) as exc:
            return self._error(exc)
        except Exception as exc:
            return self._error(exc)

    def git_log(self, run_context: RunContext, path: str | None = None, limit: int = 15) -> str:
        """Show the commit history behind the workspace, optionally for one path.

        Use it to see how a file got the way it is, and who has been touching it.

        Args:
            path: Limit history to this path.
            limit: Number of commits. Defaults to 15.

        Returns:
            One line per commit.
        """
        try:
            return self.manager.log(self._session(run_context), path, limit)
        except Exception as exc:
            return self._error(exc)
