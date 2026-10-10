# Trying PR Agent in your Slack workspace

The app runs on your Mac. Slack delivers signed HTTP callbacks through ngrok.
Use a development Slack app and a demo repository for the first run. These steps
have been checked against the installed Agno 3.0.5 routes. The Slack exchange
itself has not yet been exercised in this session.

## 1. Configure the existing .env

Work in `/Users/priti/Desktop/lab/agno-pr-agent-personal` on `main`.
Edit the existing `.env`; do not overwrite it with `example.env`. Keep actual
tokens in that file, not in Slack messages or the presentation.

Required values:

```dotenv
OPENAI_API_KEY=<your existing model key>
SLACK_BOT_TOKEN=<your development app bot token>
SLACK_SIGNING_SECRET=<that same app's signing secret>
GITHUB_REPOS=<owner/demo-repository>
SLACK_STREAMING=false
REQUIRE_CONFIRMATION=true
PREWARM_REPOS=false
```

Keep your existing GitHub App credentials or a fine-grained GitHub token scoped to
the demo repository. Publishing needs Contents and Pull requests read/write plus
Metadata read. `MODEL_ID` must name a model your API account can use.

## 2. Start the app

For a first Slack connectivity test, explicitly run the original local-tool mode:

```sh
cd /Users/priti/Desktop/lab/agno-pr-agent-personal
PORT=8010 RUNTIME_ENV=dev PREWARM_REPOS=false SLACK_STREAMING=false \
  REQUIRE_CONFIRMATION=true AGNTID_ENABLED=false \
  .venv/bin/python -m app.main
```

Wait for `Slack interface enabled at /slack/events` and application startup.
This first mode does not apply AgntID policies. Existing repo restrictions and
publishing approval still apply. Keep the process running.

## 3. Start the Slack-only tunnel

Install and authenticate ngrok using its official setup if it is not already
configured. In a second terminal, run:

```sh
cd /Users/priti/Desktop/lab/agno-pr-agent-personal
ngrok http 8010 --traffic-policy-file docs/slack-ngrok-policy.yml
```

Use the HTTPS forwarding address ngrok prints. The included policy rejects
requests outside POST `/slack/events` and POST `/slack/interactions`, so the
unauthenticated development AgentOS API does not become a public endpoint.
Agno checks Slack signatures on both accepted routes. Do not tunnel the AgntID
UI or callback ports. If ngrok rejects the policy, resolve that error before
using the tunnel.

## 4. Configure the development Slack app

At https://api.slack.com/apps choose your development app, or create one.

1. OAuth & Permissions: add the needed bot scopes. For a text-only channel/DM
   test use `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`
   (private channels), `im:history`, `im:read`, `im:write`, and `users:read`.
   Add `files:read` only for attachments. Leave streaming off for this first test.
2. Install/reinstall it in your workspace after scope changes. Put its bot token
   and signing secret in `.env`, then restart the app if they changed.
3. Event Subscriptions: enable and set
   `https://YOUR-NGROK-HOST/slack/events`. Wait for **Verified**. Subscribe to
   `app_mention` and `message.im`, then save.
4. Interactivity & Shortcuts: enable and set
   `https://YOUR-NGROK-HOST/slack/interactions`, then save. This URL is different
   from Event Subscriptions in this Agno version.
5. Invite the app to a test channel with `/invite @PR Agent` (use its actual name).

If your tunnel hostname changes, update both Slack URLs. Keep both terminals open.

## 5. Send a first message yourself

In the test channel:

> @PR Agent List the repositories you are allowed to use. Do not change any files.

Then start a thread with:

> @PR Agent In owner/demo-repository, read README.md and summarize it. Do not edit or publish anything.

Replace the repository with the exact allowlisted name. Expect a reply in Slack
and the run in your server logs. For a later publishing test, use a small change
in the demo repository and verify that Slack shows an approval card. Clicking
Approve authorizes the real GitHub action shown on that card.

## 6. Enable AgntID for the full agent

Stop the application with Ctrl-C. Ensure the deterministic replay is not running
because it uses the same callback port, 8091. Start the registered runtime:

```sh
docker compose -p pr-agent-agntid -f .agntid-runtime/compose.yaml up -d
PORT=8010 RUNTIME_ENV=dev PREWARM_REPOS=false SLACK_STREAMING=false \
  REQUIRE_CONFIRMATION=true AGNTID_ENABLED=true \
  AGNTID_MCP_URL=http://127.0.0.1:18082/mcp AGNTID_TOOL_PREFIX=pr_agent_ \
  AGNTID_ACCESS_TOKEN= .venv/bin/python -m app.main
```

The full app exposes more tools than the verified 12-tool workspace replay.
With the app running, use another terminal to restart the demo runtime so it
rediscovers the full callback schema:

```sh
docker compose -p pr-agent-agntid -f .agntid-runtime/compose.yaml restart achr-runtime
```

In AgntID, open **Tools → PR_AGENT Service**, inspect new classifications and wait
for settings to apply. Review each tool according to its real operation. Medium
excludes execution/deletion and some other actions may need review. Keep those
restrictions for the initial read/edit test. Publishing and test-running need
their own deliberate policy configuration and have not been live-verified here.

Repeat the read-only Slack prompt. In Reporting, filter by `pr_agent` and check
new Tool Execution events. For a reliable denial demonstration use the
deterministic replay in `agntid-demo.md`; a model may choose a smaller search
instead of making a prohibited call.

## Troubleshooting

### Live intent-classifier limitation (2026-10-10)

The full Slack bridge is connected, but the current runtime can reject legitimate
read-only requests. `open_workspace` now describes local checkout/cache writes,
checkout reuse/replacement, and expired-session cleanup separately from source
editing, commits, pushes, or PR publication. Runtime discovery was restarted and
the updated description was verified through MCP `list_tools`.

Real-runtime checks after this change:

| Request | Observed result |
| --- | --- |
| Open workspace, search with limit 5, and do not edit/publish | `open_workspace` access policy allowed; semantic classifier denied with `CONTRADICTION_DETECTED` |
| Open a workspace as a standalone request | Callback completed and created a local checkout; runtime also logged an unresolved-resource HITL verdict, so this is not proof of enforced approval |
| Search the open workspace with limit 5, and do not edit/publish | Access policy allowed; semantic classifier denied with `CONTRADICTION_DETECTED` |
| Search with limit 100 | Access policy denied; semantic classifier also denied |

These were direct bridge replays against the real runtime, not new Slack messages.
The exact combined prompt was also submitted directly to the local classifier:
it denied the combined request, allowed the workspace-only request, and denied an
explicit prohibition on opening a workspace. The description change alone does
not resolve the false positive. Do not remove user constraints from task context
or disable intent checks to make the demo pass. A classifier fix and a successful
allowed Slack search remain outstanding.

| Symptom | Check |
| --- | --- |
| Slack URL cannot verify | App is running on 8010, ngrok is up, URL ends in `/slack/events`, and signing secret belongs to this app |
| No reply | Bot invited, `app_mention` subscribed, scopes installed, model credentials valid, `SLACK_STREAMING=false` |
| Approval buttons fail | Interactivity points to `/slack/interactions` |
| Callback port occupied | Stop the replay or other app using port 8091 |
| Tool unavailable under policy | Runtime discovery and classification review, then profile/policy eligibility |
| No new Reporting events | Confirm the application started with `AGNTID_ENABLED=true` and runtime is operational |

Sources: installed `agno/os/interfaces/slack/router.py` and `app/main.py`;
[Slack local development](https://docs.slack.dev/tools/node-slack-sdk/tutorials/local-development/),
[Slack signature verification](https://docs.slack.dev/authentication/verifying-requests-from-slack),
[ngrok deny action](https://ngrok.com/docs/gateway/traffic-policy/actions/deny),
[ngrok request variables](https://ngrok.com/docs/gateway/traffic-policy/variables/req).
