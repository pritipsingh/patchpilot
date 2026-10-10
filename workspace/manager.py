"""
Workspace Manager
=================

Owns the git side of the agent's working copy, and owns the credentials.

One bare mirror per repository is cached under `<root>/mirrors`, and each
session gets its own worktree under `<root>/sessions/<session id>`, checked out
detached at an exact SHA -- a branch head, a tag, or a pull request's head.

The token never lands on disk and never reaches the agent:

- Remotes are written without credentials (`https://github.com/owner/repo.git`).
- Fetches pass a credential helper through `GIT_CONFIG_*` env vars that reads the
  token from the environment of that one subprocess.
- Nothing in this module runs on behalf of the agent's shell, which gets a
  scrubbed environment (see `tools/workspace_tools.py`).

The agent never commits and never pushes. Its edits stay in the working tree
until the publish tools read them back out as a change set.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agno.utils.log import log_debug, log_info, log_warning

# Files that tell an agent how to work in a repository, most specific first.
# Not every repo keeps them at the root: agno-docs has no root CLAUDE.md at all
# and puts its guidance under .claude/ and .agents/ instead.
CONVENTION_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    "CONTRIBUTING.md",
    ".claude/CLAUDE.md",
    ".agents/AGENTS.md",
    ".github/copilot-instructions.md",
)
MAX_CONVENTION_CHARS = 20_000

# Skills are a repository's own instructions for agents, one directory each with a
# SKILL.md inside. Listing them costs a few lines and tells the agent what is there
# to read; inlining every one would cost more context than the whole convention set.
SKILL_DIRS = (".agents/skills", ".claude/skills")

# Directories that carry no signal in a repo map.
MAP_SKIP = {
    ".git",
    ".github",
    "node_modules",
    ".venv",
    ".venvs",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".idea",
    ".vscode",
}

GIT_TIMEOUT = 900  # A first clone of a large repository is slow.


class GitError(RuntimeError):
    """A git command failed."""


@dataclass
class WorkspaceSession:
    """One session's checkout."""

    session_id: str
    repo: str
    path: Path
    base_sha: str
    ref: str
    description: str
    created_at: float = field(default_factory=time.time)
    pr_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "ref": self.ref,
            "base_sha": self.base_sha,
            "description": self.description,
            "pr_number": self.pr_number,
            "path": str(self.path),
        }


class WorkspaceManager:
    """Clones, worktrees, and the git reads the agent's tools are built on."""

    def __init__(
        self,
        root: Path | str,
        token_provider: Callable[[str], str],
        max_session_age_hours: float = 12.0,
    ):
        self.root = Path(root).expanduser().resolve()
        self.mirrors = self.root / "mirrors"
        self.sessions_dir = self.root / "sessions"
        self.mirrors.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._token_provider = token_provider
        self.max_session_age_hours = max_session_age_hours
        self._sessions: dict[str, WorkspaceSession] = {}
        # One lock per session: two runs in the same Slack thread must not race
        # to create or destroy the same checkout.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -----------------------------------------------------------------------
    # git plumbing
    # -----------------------------------------------------------------------
    def _git_env(self, with_credentials: bool = False, repo: str | None = None) -> dict[str, str]:
        """A minimal environment for git. Credentials are added only when fetching.

        The token is resolved per repository: under a GitHub App, two organisations
        mean two installations and two different tokens.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        if with_credentials:
            if repo is None:
                raise GitError("A credentialed git command must name its repository.")
            token = self._token_provider(repo)
            if not token:
                raise GitError(f"No GitHub token available for {repo}.")
            env["PR_AGENT_GIT_TOKEN"] = token
            # A helper defined through env vars: the token stays out of .git/config
            # and out of the process argument list.
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = '!f() { echo username=x-access-token; echo "password=$PR_AGENT_GIT_TOKEN"; }; f'
        return env

    def _git(
        self,
        args: list[str],
        cwd: Path | None = None,
        with_credentials: bool = False,
        ok_codes: tuple[int, ...] = (0,),
        repo: str | None = None,
    ) -> str:
        """Run git. `ok_codes` widens what counts as success -- diff exits 1 when it
        finds differences, which is the normal case here, not a failure."""
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            env=self._git_env(with_credentials, repo),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        if result.returncode not in ok_codes:
            # Never echo the command's environment; stderr from git is safe.
            raise GitError(f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:500]}")
        return result.stdout

    def _mirror_path(self, repo: str) -> Path:
        owner, name = repo.split("/", 1)
        return self.mirrors / f"{owner}__{name}.git"

    def ensure_mirror(self, repo: str) -> Path:
        """Clone the repository's mirror if it is missing, then fetch it."""
        mirror = self._mirror_path(repo)
        if not (mirror / "HEAD").exists():
            log_info(f"Cloning {repo} (first time, this can take a while)")
            if mirror.exists():
                shutil.rmtree(mirror, ignore_errors=True)
            self._git(
                ["clone", "--bare", f"https://github.com/{repo}.git", str(mirror)],
                with_credentials=True,
                repo=repo,
            )
            # Pull request heads are not in the default refspec.
            self._git(
                ["config", "--add", "remote.origin.fetch", "+refs/pull/*/head:refs/remotes/pull/*"],
                cwd=mirror,
            )
            self._git(["config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/heads/*"], cwd=mirror)

        log_debug(f"Fetching {repo}")
        self._git(["fetch", "--prune", "origin"], cwd=mirror, with_credentials=True, repo=repo)
        return mirror

    def _resolve_sha(self, mirror: Path, ref: str) -> tuple[str, str]:
        """Resolve a ref to a SHA. Returns (sha, what it resolved through)."""
        candidates = [ref, f"refs/heads/{ref}", f"refs/remotes/pull/{ref}", f"refs/tags/{ref}"]
        for candidate in candidates:
            try:
                sha = self._git(["rev-parse", "--verify", f"{candidate}^{{commit}}"], cwd=mirror).strip()
                return sha, candidate
            except GitError:
                continue
        raise GitError(f"Could not resolve '{ref}' in this repository.")

    def default_branch(self, mirror: Path) -> str:
        try:
            head = self._git(["symbolic-ref", "--short", "HEAD"], cwd=mirror).strip()
            return head or "main"
        except GitError:
            return "main"

    # -----------------------------------------------------------------------
    # Sessions
    # -----------------------------------------------------------------------
    def open(
        self,
        session_id: str,
        repo: str,
        ref: str | None = None,
        pr_number: int | None = None,
    ) -> WorkspaceSession:
        """Check out a worktree for this session, pinned to an exact SHA.

        Opening the same repository at the same commit again reuses the checkout
        that is already there, uncommitted edits and all. Two things depend on
        that: a second message in a Slack thread is the same session, so
        recreating would delete the checkout a run in flight is still using; and
        a follow-up ask ("also fix the test") must build on the work already
        done rather than silently discarding it. `discard_workspace` is how you
        deliberately start over.
        """
        with self._lock_for(session_id):
            return self._open(session_id, repo, ref, pr_number)

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def _open(
        self,
        session_id: str,
        repo: str,
        ref: str | None = None,
        pr_number: int | None = None,
    ) -> WorkspaceSession:
        mirror = self.ensure_mirror(repo)

        if pr_number:
            wanted = str(pr_number)
            description = f"pull request #{pr_number}"
        else:
            wanted = ref or self.default_branch(mirror)
            description = f"branch {wanted}"

        sha, resolved_through = self._resolve_sha(mirror, wanted)

        existing = self._sessions.get(session_id) or self._recover(session_id)
        if existing and existing.repo == repo and existing.base_sha == sha and existing.path.exists():
            log_info(f"Reusing checkout for {session_id}: {repo} @ {sha[:8]}")
            return existing

        # A different repo or a different commit: the old checkout is the wrong
        # tree to work in, so it goes.
        self.close(session_id)
        path = self.sessions_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) / repo.replace("/", "__")
        if path.exists():
            try:
                self._git(["worktree", "remove", "--force", str(path)], cwd=mirror)
            except GitError:
                shutil.rmtree(path, ignore_errors=True)
        # Registrations outlive the directory across a restart or a wiped volume, and a
        # stale one blocks `worktree add` at the same path.
        self._git(["worktree", "prune"], cwd=mirror)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._git(["worktree", "add", "--detach", str(path), sha], cwd=mirror)
        log_info(f"Workspace for {session_id}: {repo} @ {sha[:8]} ({description})")

        session = WorkspaceSession(
            session_id=session_id,
            repo=repo,
            path=path,
            base_sha=sha,
            ref=resolved_through,
            description=description,
            pr_number=pr_number,
        )
        self._sessions[session_id] = session
        return session

    def prewarm(self, repos: list[str]) -> None:
        """Clone the allowed repositories in the background, at startup.

        A first clone of a large repository takes minutes. Without this, that cost
        lands on whoever sends the first message. Fail-soft: a repository that cannot
        be cloned here is cloned on demand later, and reports its error then.
        """

        def run() -> None:
            for repo in repos:
                try:
                    self.ensure_mirror(repo)
                    log_info(f"Mirror ready: {repo}")
                except Exception as exc:
                    log_warning(f"Could not pre-clone {repo}: {exc}")

        threading.Thread(target=run, name="workspace-prewarm", daemon=True).start()

    def get(self, session_id: str) -> WorkspaceSession | None:
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        return self._recover(session_id)

    def _recover(self, session_id: str) -> WorkspaceSession | None:
        """Rebuild a session from a checkout an earlier process left on disk.

        A paused run survives a restart -- it lives in the database -- but this
        map does not. Without recovery, approving a pull request after a restart
        finds no workspace and the agent redoes work it had already done.
        """
        base = self.sessions_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        if not base.is_dir():
            return None
        for path in sorted(base.iterdir()):
            if not (path / ".git").exists():
                continue
            try:
                # HEAD is still the pinned base: the agent never commits.
                sha = self._git(["rev-parse", "HEAD"], cwd=path).strip()
            except GitError:
                continue
            repo = path.name.replace("__", "/", 1)
            log_info(f"Recovered checkout for {session_id}: {repo} @ {sha[:8]}")
            session = WorkspaceSession(
                session_id=session_id,
                repo=repo,
                path=path,
                base_sha=sha,
                ref=sha,
                description=f"recovered checkout of {repo}",
            )
            self._sessions[session_id] = session
            return session
        return None

    def close(self, session_id: str) -> bool:
        """Discard a session's worktree and everything uncommitted in it."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            mirror = self._mirror_path(session.repo)
            self._git(["worktree", "remove", "--force", str(session.path)], cwd=mirror)
        except GitError as exc:
            log_warning(f"worktree remove failed ({exc}); removing the directory")
            shutil.rmtree(session.path, ignore_errors=True)
        return True

    def gc(self) -> int:
        """Drop sessions older than the configured age, this process's and older runs'."""
        cutoff = time.time() - self.max_session_age_hours * 3600
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for session_id in stale:
            self.close(session_id)
        return len(stale) + self.sweep_orphans(cutoff)

    def sweep_orphans(self, cutoff: float) -> int:
        """Remove checkouts left behind by a previous process.

        Sessions live in memory, so a restart orphans every directory under
        `sessions/`. Anything this process does not know about and that has gone
        untouched past the cutoff is reclaimed, and every mirror is pruned so the
        registrations go with it.
        """
        live = {session.path for session in self._sessions.values()}
        removed = 0
        for session_dir in self.sessions_dir.glob("*/*"):
            if not session_dir.is_dir() or session_dir in live:
                continue
            try:
                if session_dir.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(session_dir, ignore_errors=True)
                removed += 1
            except OSError as exc:
                log_warning(f"Could not reclaim {session_dir}: {exc}")
        if removed:
            for mirror in self.mirrors.glob("*.git"):
                try:
                    self._git(["worktree", "prune"], cwd=mirror)
                except GitError as exc:
                    log_warning(f"worktree prune failed for {mirror.name}: {exc}")
            log_info(f"Reclaimed {removed} checkout(s) from an earlier run")
        return removed

    # -----------------------------------------------------------------------
    # Reads the agent's tools are built on
    # -----------------------------------------------------------------------
    def status(self, session: WorkspaceSession) -> list[dict[str, str]]:
        """Every change in the working tree, against the pinned base SHA."""
        out = self._git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=session.path)
        entries: list[dict[str, str]] = []
        fields = [f for f in out.split("\0") if f]
        index = 0
        while index < len(fields):
            field_value = fields[index]
            code, path = field_value[:2], field_value[3:]
            if code[0] == "R":
                # A rename is followed by its source path.
                index += 1
                entries.append(
                    {"status": "renamed", "path": path, "from": fields[index] if index < len(fields) else ""}
                )
            elif "D" in code:
                entries.append({"status": "deleted", "path": path})
            elif code == "??":
                entries.append({"status": "added", "path": path})
            else:
                entries.append({"status": "modified", "path": path})
            index += 1
        return entries

    def diff(self, session: WorkspaceSession, path: str | None = None, stat: bool = False) -> str:
        """The working tree's diff against the pinned base SHA, untracked files included."""
        args = ["diff", session.base_sha]
        if stat:
            args.append("--stat")
        if path:
            args += ["--", path]
        tracked = self._git(args, cwd=session.path)

        untracked = [entry["path"] for entry in self.status(session) if entry["status"] == "added"]
        if path:
            untracked = [candidate for candidate in untracked if candidate == path or candidate.startswith(f"{path}/")]
        if not untracked:
            return tracked

        if stat:
            extra = "\n".join(f" {name} | (new file)" for name in untracked)
            return f"{tracked}\n{extra}".strip()

        # `git diff` ignores untracked files, so each one is diffed against /dev/null
        # separately -- `--no-index` takes exactly two paths, and exits 1 on a difference.
        blocks = []
        for name in untracked:
            blocks.append(
                self._git(
                    ["diff", "--no-index", "--", os.devnull, name],
                    cwd=session.path,
                    ok_codes=(0, 1),
                )
            )
        return "\n".join([tracked, *blocks]).strip()

    def log(self, session: WorkspaceSession, path: str | None = None, limit: int = 15) -> str:
        args = ["log", f"-{max(1, min(limit, 100))}", "--pretty=format:%h %ad %an: %s", "--date=short"]
        if path:
            args += ["--", path]
        return self._git(args, cwd=session.path)

    def file_at_base(self, session: WorkspaceSession, path: str) -> str | None:
        """The file's content at the pinned base SHA, or None when it did not exist."""
        try:
            return self._git(["show", f"{session.base_sha}:{path}"], cwd=session.path)
        except GitError:
            return None

    def skills(self, session: WorkspaceSession) -> list[str]:
        """Paths to the repository's own agent skills, for the agent to read on demand."""
        found: list[str] = []
        for directory in SKILL_DIRS:
            root = session.path / directory
            if not root.is_dir():
                continue
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and (entry / "SKILL.md").is_file():
                    found.append(f"{directory}/{entry.name}/SKILL.md")
                elif entry.is_file() and entry.suffix == ".md":
                    found.append(f"{directory}/{entry.name}")
        return found

    def conventions(self, session: WorkspaceSession) -> dict[str, str]:
        """The repository's own instructions for people working in it."""
        found: dict[str, str] = {}
        for name in CONVENTION_FILES:
            candidate = session.path / name
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > MAX_CONVENTION_CHARS:
                text = text[:MAX_CONVENTION_CHARS] + "\n... [truncated]"
            found[name] = text
        return found

    def repo_map(self, session: WorkspaceSession, depth: int = 2) -> list[str]:
        """A shallow directory map, so the agent knows where things live."""
        lines: list[str] = []

        def walk(directory: Path, level: int, prefix: str) -> None:
            if level > depth:
                return
            try:
                entries = sorted(
                    (entry for entry in directory.iterdir() if entry.name not in MAP_SKIP),
                    key=lambda entry: (entry.is_file(), entry.name),
                )
            except OSError:
                return
            directories = [entry for entry in entries if entry.is_dir()]
            files = [entry for entry in entries if entry.is_file()]
            for entry in directories:
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, level + 1, prefix + "  ")
            if level == 1:
                for entry in files[:25]:
                    lines.append(f"{prefix}{entry.name}")

        walk(session.path, 1, "")
        return lines

    def summary(self, session: WorkspaceSession) -> str:
        """What the agent gets when it opens a workspace: where it is, and how to work here."""
        conventions = self.conventions(session)
        skills = self.skills(session)
        payload = {
            "workspace": session.to_dict(),
            "repo_map": self.repo_map(session),
            "convention_files": list(conventions.keys()),
            "skills": skills,
        }
        blocks = [json.dumps(payload, indent=2)]
        for name, text in conventions.items():
            blocks.append(f"\n===== {name} (this repository's own rules -- follow them) =====\n{text}")
        if skills:
            blocks.append(
                "\n===== this repository's agent skills =====\n"
                "Read the one that covers your task before you start:\n" + "\n".join(f"- {s}" for s in skills)
            )
        return "\n".join(blocks)
