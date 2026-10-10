"""
GitHub Credentials
==================

One place decides who the agent is on GitHub, and it is the only holder of the
secret. Two modes:

- **GitHub App** (`GITHUB_APP_ID` + a private key). Pull requests are authored by
  the app -- `PatchPilot[bot]` -- not by a person. Installation tokens are minted on
  demand, expire in an hour, are scoped to the repositories the app is installed
  on, and are never written to disk.
- **Token** (`GITHUB_ACCESS_TOKEN`). A personal or machine-user token. Pull
  requests are authored by whoever owns it.

App mode is resolved per repository: an app installed on two organisations gets a
different installation, and a different token, for each.

Nothing here is reachable from the agent. The workspace shell has no credentials
in its environment, and git fetches take their token from this module for the
length of one subprocess.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agno.utils.log import log_debug, log_info

# Mint a new installation token once the current one is inside this of expiry.
REFRESH_MARGIN = timedelta(minutes=5)


class CredentialError(RuntimeError):
    """No usable GitHub credentials."""


class GitHubCredentials:
    """Resolves a GitHub API client and a git token for a repository."""

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        installation_id: int | None = None,
        access_token: str | None = None,
        base_url: str | None = None,
    ):
        self.app_id = (app_id or "").strip()
        self.private_key = self._normalize_key(private_key)
        self.installation_id = installation_id
        self.access_token = (access_token or "").strip()
        self.base_url = base_url

        if self.app_id and not self.private_key:
            raise CredentialError("GITHUB_APP_ID is set but no private key was provided.")
        if not self.app_id and not self.access_token:
            raise CredentialError(
                "No GitHub credentials. Set GITHUB_APP_ID with a private key, or GITHUB_ACCESS_TOKEN."
            )

        self._app_auth: Any = None
        self._integration: Any = None
        self._installations: dict[str, int] = {}
        self._clients: dict[str, Any] = {}
        self._tokens: dict[int, tuple[str, datetime]] = {}

    @property
    def mode(self) -> str:
        return "app" if self.app_id else "token"

    @staticmethod
    def _normalize_key(private_key: str | None) -> str:
        """Accept a PEM inline, with escaped newlines, or as a path to a key file."""
        if not private_key:
            return ""
        key = private_key.strip()
        if "BEGIN" not in key:
            path = Path(key).expanduser()
            if path.is_file():
                return path.read_text().strip()
            return ""
        # Env vars often carry the PEM with literal backslash-n instead of newlines.
        return key.replace("\\n", "\n")

    def describe(self) -> str:
        if self.mode == "app":
            return f"GitHub App {self.app_id} (pull requests authored by the app)"
        return "GitHub token (pull requests authored by the token's owner)"

    # -----------------------------------------------------------------------
    # App mode
    # -----------------------------------------------------------------------
    def _app(self) -> Any:
        if self._app_auth is None:
            from github import Auth, GithubIntegration

            self._app_auth = Auth.AppAuth(self.app_id, self.private_key)
            if self.base_url:
                self._integration = GithubIntegration(auth=self._app_auth, base_url=self.base_url)
            else:
                self._integration = GithubIntegration(auth=self._app_auth)
        return self._app_auth

    def _installation_id_for(self, repo: str) -> int:
        """Which installation covers this repository."""
        if self.installation_id:
            return self.installation_id
        cached = self._installations.get(repo)
        if cached is not None:
            return cached

        self._app()
        owner, name = repo.split("/", 1)
        installation = self._integration.get_repo_installation(owner, name)
        log_debug(f"Installation {installation.id} covers {repo}")
        self._installations[repo] = installation.id
        return int(installation.id)

    # -----------------------------------------------------------------------
    # What callers use
    # -----------------------------------------------------------------------
    def client_for(self, repo: str) -> Any:
        """An authenticated PyGithub client for this repository."""
        from github import Auth, Github

        if self.mode == "token":
            key = "token"
            if key not in self._clients:
                auth = Auth.Token(self.access_token)
                self._clients[key] = Github(base_url=self.base_url, auth=auth) if self.base_url else Github(auth=auth)
            return self._clients[key]

        installation_id = self._installation_id_for(repo)
        key = f"app:{installation_id}"
        if key not in self._clients:
            # PyGithub mints and refreshes the installation token behind this auth.
            auth = self._app().get_installation_auth(installation_id)
            self._clients[key] = Github(base_url=self.base_url, auth=auth) if self.base_url else Github(auth=auth)
        return self._clients[key]

    def token_for(self, repo: str) -> str:
        """A raw token for one git fetch. Cached until it is close to expiring."""
        if self.mode == "token":
            return self.access_token

        installation_id = self._installation_id_for(repo)
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - REFRESH_MARGIN > datetime.now(UTC):
            return cached[0]

        self._app()
        authorization = self._integration.get_access_token(installation_id)
        log_info(f"Minted an installation token for {repo}, expires {authorization.expires_at.isoformat()}")
        self._tokens[installation_id] = (authorization.token, authorization.expires_at)
        return str(authorization.token)
