# PatchPilot

A Slack coding assistant that reads repositories, prepares changes, and asks for
approval before opening or updating a pull request. Optional AgntID enforcement
checks repository tool calls against runtime policies.

## PatchPilot + AgntID

[AgntID](https://agntid.ai/) provides the runtime policy layer for PatchPilot's
repository tools. PatchPilot handles the coding workflow; AgntID evaluates the
protected tool calls before their local implementations execute.

Approval before publishing covers the final GitHub action. AgntID adds checks
on intermediate operations, such as code searches and file changes. In protected
mode, each repository tool call carries the user's request and identity to the
runtime so its decision can be traced in Reporting.

| Control | How PatchPilot uses it |
| --- | --- |
| Tool availability | Checks the runtime's tool list; unavailable tools stop before execution. |
| Argument policies | Applies configured limits, such as a maximum of 20 search results. |
| Intent validation | Supplies the original request for the runtime's intent check; current false positives are documented below. |
| Audit trail | Opens a task for each tool call and links denials to its task ID and Reporting evidence. |
| Private execution | Uses a single-use ticket binding the approved callback to the exact tool, arguments, and session. |

If the runtime is unavailable or denies a request, the bridge does not fall back
to unprotected execution. Human approval before publishing remains a separate
requirement. Slack thread-reading itself is outside this repository-tool bridge.

### Verified policy demonstration

The deterministic replay uses temporary sample files and the real AgntID runtime:

| Action | Observed result |
| --- | --- |
| Search with `limit=5` | Completed |
| Read and edit a sample function | Completed; resulting file checked |
| Search with `limit=100` | Denied by the configured 1–20 range policy |
| Delete the sample deployment file | Tool excluded by the Medium profile; file preserved |

The excluded delete call never reached runtime execution; the oversized search
is the recorded policy denial. This replay is separate from an end-to-end Slack
demo. See [setup, architecture, and recorded evidence](docs/agntid-demo.md), or
jump to [enabling AgntID](#enable-agntid).

## What it does

- Reads code, searches files, and inspects pull requests.
- Prepares changes in a separate checkout for each Slack thread.
- Runs permitted development commands and reports their results.
- Shows the proposed changes and requests approval before publishing.
- Routes repository tools through AgntID when enforcement is enabled.

Example workflow (illustrative, not a recorded test):

```text
you   @PatchPilot Find why the image handler always returns image/jpeg.
bot   Found the hardcoded type in src/images.py. Here is the proposed fix.
you   Prepare the change and run the relevant tests.
bot   Here are the diff and test results. Approve publishing a draft PR?
you   Approve
bot   Here is the draft pull request.
```

## How it works

```text
With AgntID enabled:
Slack → PatchPilot → repository tool call
                         ↓
                 human approval (publishing only)
                         ↓
                 AgntID runtime checks
                         ↓
                 private callback → local tool / GitHub API
```

Each thread uses an isolated Git worktree pinned to a commit. Repository access
is limited by `GITHUB_REPOS`. Publishing uses a separate tool with approval
required by default. Slack requests are signature-verified on both callback routes.

The shell has a command allowlist and a scrubbed environment. File tools block
common secret paths. These controls are not a complete process sandbox; run
untrusted repository code in an appropriately isolated environment.

## Current demo status

The local Slack connection and deterministic AgntID replay have been exercised.
The replay demonstrated successful reads/edits, a denied search above the
configured result limit, and exclusion of the delete tool.

**Known limitation:** the current AgntID intent classifier can reject legitimate
read-only requests containing constraints such as “Do not edit files.” Updating
the workspace description did not resolve this false positive. Full protected
Slack execution and publishing are not yet verified end to end. Details are in
the [local Slack guide](docs/slack-local.md).

## Run locally

```bash
git clone https://github.com/pritipsingh/patchpilot.git
cd patchpilot
./scripts/venv_setup.sh
cp example.env .env
```

Fill in `.env` with your model key, Slack bot token and signing secret, and a
GitHub App or fine-grained token scoped to your chosen repositories. Keep actual
credentials out of Git. The example repository allowlist is
`pritipsingh/patchpilot`; change `GITHUB_REPOS` for your own demo repository.

GitHub access requires Contents and Pull requests read/write plus Metadata read
for publishing. A GitHub App must be installed on the selected repository; a
fine-grained token must explicitly include it. Leave `REQUIRE_CONFIRMATION=true`.

```bash
PORT=8010 RUNTIME_ENV=dev PREWARM_REPOS=false \
SLACK_STREAMING=false REQUIRE_CONFIRMATION=true \
AGNTID_ENABLED=false .venv/bin/python -m app.main
```

This starts the server on port 8010 for the initial Slack connectivity test.
Development mode serves without API authentication. Expose only the signed
Slack callbacks using the supplied ngrok policy:

```bash
ngrok http 8010 --traffic-policy-file docs/slack-ngrok-policy.yml
```

In your Slack app, disable Socket Mode and configure:

| Setting | URL |
| --- | --- |
| Event Subscriptions | `https://YOUR-NGROK-HOST/slack/events` |
| Interactivity & Shortcuts | `https://YOUR-NGROK-HOST/slack/interactions` |

Subscribe to `app_mention` and `message.im`, install the required scopes, and
invite the app to your test channel. Use the actual Slack app display name in
mentions; changing this repository does not rename an already-installed bot.
See the [full Slack setup](docs/slack-local.md) for scopes and troubleshooting.

## Enable AgntID

Install the optional integration:

```bash
.venv/bin/python -m pip install -e '.[agntid]'
```

If your virtual environment was created without pip, use
`uv pip install --python .venv/bin/python -e '.[agntid]'`.

Follow the [runtime setup and replay guide](docs/agntid-demo.md). The private
callback executor binds to port 8091 by default. Run one application worker and
keep this endpoint private. The integration retains the internal `pr-agent`
identity and `pr_agent` tool prefix for compatibility with the existing runtime
policies and audit history.

## Development

```bash
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy agents app db tools workspace
.venv/bin/python -m pytest tests -q
```

| Path | Purpose |
| --- | --- |
| `agents/pr_agent.py` | Agent instructions and tool wiring |
| `app/main.py` | HTTP server and Slack interface |
| `app/agntid_bridge.py` | Policy routing and single-use execution tickets |
| `app/agntid_executor.py` | Private callback listener |
| `workspace/manager.py` | Repository mirrors and session checkouts |
| `tools/` | Workspace, GitHub, publishing, and Slack tools |
| `scripts/agntid_demo.py` | Deterministic replay on disposable files |

Docker Compose provides a local database and persistent workspace volume.
Deployment requires your own infrastructure and secrets. The inherited Railway
workflow is restricted to the original repository and cannot deploy this copy.

## Framework and attribution

PatchPilot uses [Agno](https://docs.agno.com) 3.0.5 and its AgentOS Slack interface,
plus the optional AgntID SDK. Framework imports, environment variables, and base
images retain their upstream names.

This private standalone repository derives from
[agno-agi/agno-pr-agent](https://github.com/agno-agi/agno-pr-agent), using the local
`7f8cd88` snapshot with the AgntID integration. The original project attribution
is retained here; the runtime verification notes preserve their historical names.
