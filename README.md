# PR Agent

Private personal demo copy of [agno-agi/agno-pr-agent](https://github.com/agno-agi/agno-pr-agent),
based on local commit `7f8cd88` with the AgntID integration. Original project
attribution and documentation are retained. This is a standalone repository,
not a GitHub fork. See [the local Slack guide](docs/slack-local.md) for current
verification results and the unresolved intent-classifier limitation.

A Slack-native [AgentOS](https://docs.agno.com) service that works on your repositories
the way a person does: it checks the code out, reads it, changes it, runs the tests, and
opens a pull request once you approve.

The thread is the interface. Someone reports a bug, the discussion happens, you say
"open a PR for this" — the agent reads the whole thread, checks out the repo, greps for
the real cause, makes the change, runs the linter and the tests, shows you the diff, and
waits for your approval before anything reaches GitHub.

```
you   @PR Agent the Gemini image handler hardcodes image/jpeg, can you fix it
bot   Reading the thread...
bot   Checked out agno-agi/agno at a1b3f9c (main). Read CLAUDE.md and .cursorrules.
bot   libs/agno/agno/models/google/gemini.py:214 hardcodes the MIME type.
      Changed it to resolve from the image bytes. Ran:
        ruff check libs/agno/agno/models/google/ — passed
        pytest libs/agno/tests/unit/models/test_gemini.py -q — 14 passed
      1 file, +9 −3.  [ Approve ]  [ Deny ]
you   (Approve)
bot   Opened agno-agi/agno#9912 (draft) — https://github.com/agno-agi/agno/pull/9912
```

## How it works

```
Slack thread ──▶ Agent ──▶ worktree (pinned to an exact SHA)
                   │         read · grep · edit · run tests
                   │         no credentials · no network · no git
                   │
                   └──▶ publish tools ──▶ GitHub API ──▶ pull request
                             ▲
                        your approval
```

The split is the point. The agent does its work in a real checkout, where it can grep
and run things. It has no way to publish anything itself — no `git`, no token, no push.
The only path out is a publish tool, and that tool stops and asks you first.

### The workspace

`open_workspace` gives the agent a git worktree cut from a bare mirror, checked out
**detached at an exact commit** — a branch head, or a pull request's head. Each Slack
thread gets its own, resolved from the session id, so two threads on two branches never
share a checkout.

The first call also hands the agent the repository's own rules — `AGENTS.md`,
`CLAUDE.md`, `.cursorrules`, `CONTRIBUTING.md` — and a depth-2 map of the tree. That is
what stops it writing code that gets rejected on conventions it never read.

### What the agent can run

| | |
|---|---|
| Read | `read_file` with line numbers, `list_files` with globs, `search_code` (grep over the real checkout, so it sees the PR's own changes) |
| History | `git_log` on a path, `git_diff` against the pinned commit |
| Write | `edit_file` (exact substring), `write_file`, `delete_file` |
| Verify | `run_command` — the repo's formatter, linter, test suite |
| Review | `get_pull_request`, `get_pull_request_diff` for what git doesn't hold: the description, the review comments |
| Publish | `review_changes`, then `open_pull_request` or `update_pull_request` |

## What happens when you ask for a PR

1. You @mention it in a thread. Slack POSTs to `/slack/events`; the request signature is
   verified against `SLACK_SIGNING_SECRET`.
2. The thread becomes a session (`pr-agent:{channel}:{thread_ts}`), which is also how the
   agent finds its worktree.
3. It works: `open_workspace` → reads the repo's conventions → greps → edits → runs the
   linter and tests → `review_changes`.
4. It calls `open_pull_request`. The run **pauses** and Slack renders Approve / Deny
   buttons in the thread. Nothing has touched GitHub yet.
5. You click Approve. Slack posts the interaction back to the same endpoint — this is why
   **Interactivity must be enabled**, not just Events — the paused run resumes, and only
   then does the branch, the commit and the PR get created.
6. It posts the URL in the thread.

Deny resumes the run with the refusal, and the agent is told not to route around it.

## The boundaries

These are enforced in code, not asked for in the prompt:

- **The agent cannot publish anything.** `git` is not on its command allowlist, its shell
  environment holds no token, and the worktree's remote carries no credentials. Changes
  live in the working tree until a publish tool sends them through the API.
- **Publishing stops for approval.** `open_pull_request` and `update_pull_request` pause
  the run and render approve/deny buttons in the thread.
- **Only the repos on the allowlist.** `GITHUB_REPOS` gates both the checkout and the
  publish.
- **Only its own branches.** Every write goes to a branch under `PR_BRANCH_PREFIX`
  (default `pr-agent/`), cut from the exact commit the workspace was pinned to — so if a
  file moved on the base branch meanwhile, you get a merge conflict to resolve rather
  than a silent revert.
- **The shell is allowlisted and credential-free.** `SHELL_ALLOWED_COMMANDS` is checked
  against every command, including each segment of a `bash -c` script; command
  substitution and redirects outside the workspace are refused. `HOME` and `TMPDIR` point
  outside the checkout, so a stray cache file can't ride along into a PR.
- **Secrets are unreadable.** `.env`, `*.pem`, `*.key`, `.netrc` and `.git` are refused
  by name, not merely hidden from listings.

What it still can't do: merge, review-approve, or push to a fork.

## Setup

### Optional AgntID enforcement

The helper can route its existing repository tools through an AgntID runtime,
retaining Slack approval before publishing. See [setup and the before/after
demo](docs/agntid-demo.md). This mode is opt-in, requires the `agntid` extra, and
fails closed if the runtime is unavailable. The default local behavior is unchanged.

### 1. Install

```bash
git clone <this repo> && cd agno-pr-agent && ./scripts/venv_setup.sh && cp example.env .env
```

Fill in `OPENAI_API_KEY`, your GitHub identity (below) and `GITHUB_REPOS` — which
defaults to `agno-agi/agno-os,agno-agi/agno`. `git` must be on PATH, and `node`/`pnpm`
too if you want it to verify changes to a TypeScript repo like agno-os.

### 2. GitHub identity

This decides **who authors the pull requests**, and it is not the Slack user who asked.

**A GitHub App (recommended).** PRs are authored by the app — `PR Agent[bot]`. No org
seat, per-repo installation control, and installation tokens are minted on demand and
expire in an hour rather than sitting in `.env` forever.

1. Create it at `https://github.com/organizations/<org>/settings/apps`.
2. Repository permissions: **Contents** read+write, **Pull requests** read+write,
   **Metadata** read-only.
3. Install it on the repos in `GITHUB_REPOS`, and download a private key.
4. Set `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` (a path to the `.pem`, or the PEM
   inline — escaped newlines are handled).

The installation is resolved per repository, so an app installed across two orgs gets the
right token for each.

**Or a token.** Set `GITHUB_ACCESS_TOKEN` to a fine-grained token with the same three
permissions, scoped to the repos in `GITHUB_REPOS`. `agno-agi/agno-os` is private, so it
must be selected in the token's repository access list and its owner must already have
access. PRs will read as authored by that account.

Either way the credential lives only in the manager process. It is never written to disk,
never in the worktree's git config, and never in the agent's shell environment.

**Provenance.** Because the account is shared, every PR body gets a trailer naming the
person who asked — `Requested by @sam in Slack, and opened by PR Agent on their behalf.`
It is built from the Slack identity in run metadata, not from anything the model says, so
it cannot be talked into crediting someone else. That needs `resolve_user_identity=True`,
which is already on.

### 3. Run it

```bash
cd agno-pr-agent && source .venv/bin/activate && python -m app.main
```

AgentOS comes up on `http://localhost:8000`. Connect it at
[os.agno.com](https://os.agno.com) to try the agent in a browser before wiring up Slack.

### 4. Slack app

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps) → **From scratch**.
2. **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`, `channels:history`,
   `groups:history`, `im:history`, `im:read`, `im:write`, `chat:write`, `users:read`,
   `files:read`, `assistant:write`.
3. Install, then copy the **Bot User OAuth Token** (`xoxb-…`) into `SLACK_BOT_TOKEN` and
   the **Signing Secret** into `SLACK_SIGNING_SECRET`.
4. **Event Subscriptions** → Request URL `https://<your-host>/slack/events`, bot events
   `app_mention` and `message.im`.
5. **Interactivity & Shortcuts** → on, URL `https://<your-host>/slack/interactions`.
   This is what makes the approve buttons work. Agno 3.0.5 uses a separate route.
6. `/invite @PR Agent`.

For local testing, follow [the local Slack guide](docs/slack-local.md). It includes
a tunnel policy that exposes only the signed Slack routes, plus AgntID setup.

### 5. Deploy

**Railway** — push the repo, add a Postgres plugin (it sets `DATABASE_URL`), **attach a
volume at `/workspace`**, and set `OPENAI_API_KEY`, your GitHub identity vars, `GITHUB_REPOS`,
`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `RUNTIME_ENV=prd`. `railway.json` handles the build.

Without the volume it still works, but every cold start re-clones every repo.

**Docker Compose**, locally with Postgres and a workspace volume:

```bash
cd agno-pr-agent && docker compose up
```

> `RUNTIME_ENV` other than `dev` turns on AgentOS authorization, which needs a
> `JWT_VERIFICATION_KEY` from [os.agno.com](https://os.agno.com). The Slack endpoint is
> exempt — it verifies Slack's own request signature instead.

## Running the repo's tests

`run_command` runs inside the checkout with the *image's* toolchain, not the repo's. Out
of the box that covers anything on `PATH` — `ruff`, `mypy`, `pytest` — but a repo whose
suite needs its own dependencies installed will fail until they are. Two options: bake
them into the image, or add the repo's setup command to `SHELL_ALLOWED_COMMANDS` (`uv` is
allowed by default) and let the agent install into the workspace.

**The repo's own scripts are refused by default.** `./scripts/format.sh` is not an
allowlisted program, and allowing arbitrary repo scripts would be the same as allowing
arbitrary code — which is the whole point of the allowlist. Either add the script's name
to `SHELL_ALLOWED_COMMANDS`, or let the agent run the underlying tools directly, which is
what it does on its own. In a real run against agno it tried `./scripts/format.sh`, was
refused, said so, and ran `ruff format` and `ruff check` instead.

The agent is told never to report a test as passing without its output in front of it —
in that same run it reported `mypy` as *not run* because it was not installed, rather
than quietly skipping it.

## Layout

```
agents/pr_agent.py         The agent: model, tools, instructions
app/main.py                AgentOS, the Slack interface, the startup sweep
app/settings.py            Every env var, in one place
app/credentials.py         Who the agent is on GitHub: App installation, or token
workspace/manager.py       Mirrors, worktrees, git reads — and the only holder of the token
tools/workspace_tools.py   The agent's hands: read/grep/edit/run, sandboxed
tools/publish.py           The one path from workspace to GitHub
tools/github_repo.py       PR metadata and the API client
tools/slack_thread.py      read_slack_thread, resolved from the run's session id
db/__init__.py             Postgres when configured, SQLite otherwise
```

### How the thread becomes context

Agno's Slack interface maps one thread to one session, with a session id of
`{agent_id}:{channel_id}:{thread_ts}`. Two things follow:

- The agent remembers a thread across messages for free — that's session history.
- `read_slack_thread` takes no arguments. It reads the channel and thread timestamp back
  out of `run_context.session_id`, so it can pull the part of the thread that happened
  *before* anyone mentioned it. The workspace tools resolve their checkout the same way.

### Adding a repo

```bash
GITHUB_REPOS=agno-agi/agno,agno-agi/agno-docs
```

The token's scope has to cover it too, or the agent passes the allowlist and then fails
at the API.

## Notes

- A change set is capped at 25 files and 400KB per file. Binary files are uploaded as
  blobs; executable bits are preserved.
- The agent never commits locally. `git status` against the pinned SHA is the whole
  change set, which is why `review_changes` can show you exactly what will be published.
- Content the agent reads — PR bodies, diffs, code comments, test fixtures, other
  people's Slack messages — is data, not instructions. It is told to report embedded
  instructions rather than follow them.
- `Workspace` scopes paths; it is not a process sandbox. The command allowlist and the
  scrubbed environment narrow it a long way, but for genuinely untrusted input run the
  whole service in a container, which is what the Dockerfile is for.
