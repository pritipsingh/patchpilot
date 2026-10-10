# PR Agent with AgntID

The project remains a Slack-native PR helper. In AgntID mode its repository,
workspace and publish tools execute through the runtime before reaching their
existing implementations. Slack thread reading remains local. Existing repository,
branch, path and command restrictions remain in force, as does human approval
before publishing or posting a GitHub comment.

## Architecture

```text
Slack / AgentOS → Agno tool call → human approval (publish/comment only)
                               → AgntID task open + tool call
                               → runtime policy
                               → private MCP callback → existing tool
                               → result + AgntID Reporting
```

Each tool invocation opens and closes an AgntID task with the current user request.
This deliberately uses a short task per tool call, including calls resumed after
approval; it does not keep one long-lived AgntID task for the whole conversation.
User and session identity come from Agno's run context, not model arguments.
Multi-step intent snapshots, delegation and prompt-aware tool shortlisting are not
implemented. Runtime tool visibility and per-call policy remain authoritative.

The callback requires a random, single-use execution ticket bound to the tool,
normalized arguments and trusted session. Tickets never enter the model's schema
or messages. The callback consumes the ticket before running the tool. It cannot
be used to execute arbitrary standalone calls from Simulator or Inspector; run
calls through this helper or the replay below instead.

There is no direct-execution fallback, policy-result cache or automatic retry.
A timeout can occur after a side effect has started: inspect Reporting and actual
state before retrying. Restarting the app invalidates pending tickets. Run one
AgentOS worker: tickets and workspace managers are process-local.

## Setup

1. Install the optional SDK from the pinned official source commit:

   ```sh
   uv pip install -e '.[agntid,dev]'
   ```

2. Deploy and connect the runtime using the
   [AgntID quick-start](https://docs.agntid.ai/quick-start/).
   This repository does not create portal credentials or register runtimes.

3. Configure the helper in `.env`:

   ```dotenv
   AGNTID_ENABLED=true
   AGNTID_MCP_URL=http://localhost:8082/mcp
   AGNTID_TOOL_PREFIX=pr_agent_
   AGNTID_EXECUTOR_HOST=127.0.0.1
   AGNTID_EXECUTOR_PORT=8091
   REQUIRE_CONFIRMATION=true
   PORT=8010
   ```

   Use the actual MCP endpoint from your runtime; the URL above is an example.
   Set `AGNTID_ACCESS_TOKEN` separately when runtime OAuth requires it. Never put
   tokens in the URL. Port 8010 avoids the existing local AgentOS on port 8000.

4. Start the helper: `python -m app.main`. It starts its callback MCP listener at
   `http://127.0.0.1:8091/mcp`. The callback listener only exists in AgntID mode.

5. In the portal, add a Remote HTTP MCP server with ID/prefix `pr_agent`, pointing
   at the callback endpoint reachable **from the runtime**. With Docker Desktop
   on macOS this is commonly `http://host.docker.internal:8091/mcp`. If loopback
   is not reachable, bind `AGNTID_EXECUTOR_HOST` to a private reachable interface.
   Keep this endpoint private. It is not a public MCP service for other clients.

6. Test connection, save, review tool classifications, and wait for `Applied`.
   Set `AGNTID_TOOL_PREFIX` to the exact public tool prefix from the runtime's
   tools list (the portal may display dotted names while MCP uses underscores).
   Use the [server setup guide](https://docs.agntid.ai/mcp/attach-server/).

7. Choose a profile and policies. Medium permits reviewed normal reads/writes but
   excludes execute/delete operations. Consequently, `run_command` may be hidden
   and the helper must report tests as not run. For a complete coding demo that
   runs tests, explicitly review the execution capability and configure a profile
   and policies allowing your intended commands. Do not misclassify execution as
   a read to make it visible. Validate every intended allowed and denied action.

`AGNTID_ENABLED=false` preserves the original local behavior. It is a startup
setting controlled by the operator, never an agent-accessible toggle. The SDK is
optional and absent from the default container; use the installation above for
this local demo rather than deploying the default image with enforcement enabled.

## Repeatable before/after replay

The replay uses the actual workspace tools and temporary synthetic source files.
It has no LLM, GitHub or Slack calls. Label it **deterministic tool-call replay**
in the presentation; show the live PR helper separately as the useful agent.
Do not run the helper and replay simultaneously on the same callback port.

Baseline:

```sh
python -m scripts.agntid_demo
```

It searches code, fixes a function, permits an oversized search, and deletes a
disposable deployment file. No real checkout or deployment file is touched.

Protected:

```sh
AGNTID_ENABLED=true python -m scripts.agntid_demo --protected
```

The replay starts the callback server and pauses so you can register it and apply
policies. Use Medium, approve the read/edit classifications, and set the baseline
allow policy for `pr_agent.search_code` to `limit` minimum 1, maximum 20. The same
numeric min/max controls are demonstrated in AgntID's quick-start. Do not add a
policy constraint to `execution_ticket`; it is a transport capability.

After synchronization, press Enter. Expected evidence:

| Call | Expected result | Evidence |
| --- | --- | --- |
| Search with limit 5 | Allowed | Actual matching source lines |
| Edit `return 2` to `return 3` | Allowed | Temporary file contains the fix |
| Search with limit 100 | Denied by input policy | Runtime Reporting denial; no callback |
| Delete deployment file | Excluded by Medium | Helper reports unavailable tool; file still exists |

The hidden delete tool is rejected before submission, so do not describe it as a
runtime Tool Execution denial. The oversized search is the actual runtime-denied
call. The script exits unsuccessfully if the observed outcomes do not match.
Tool task IDs are included in denial responses and application logs; Reporting
is the authoritative runtime evidence. No ticket values are printed by the replay.

## Live pitch

“Our PR helper already fixes code and asks before publishing. Human approval alone
doesn't constrain every intermediate tool call. We added AgntID so tool visibility
and argument policy are checked during execution, while retaining our original
approval and repository protections.”

1. Give the helper a small bug in a disposable repository; show its diagnosis and edit.
2. Show the bounded-search replay before and after enforcement.
3. Show the real policy denial in Reporting and the preserved deployment file.
4. Show the PR approval pause. Publish only to an explicitly chosen demo repository
   after approving the actual changes.

Do not claim this is a new process sandbox, that all arbitrary shell behavior is
covered, or that it guarantees protection from every prompt injection. AgntID
governs the tool calls routed through it. Existing shell/process limits are still
those described in the main README.

## Verification

```sh
python -m ruff check .
python -m mypy agents app db tools workspace
python -m pytest tests -q
```

Tests exercise fail-closed behavior, task closure, preserved approval flags,
session isolation, ticket tampering/replay, and an actual local HTTP MCP callback.
Test-double decisions are not proof of real AgntID policy enforcement. Complete
the protected replay against your registered runtime before recording a demo.

### Live verification: 10 October 2026

Verified at 14:10 IST against **PR Agent Local Demo** in Priti Singh's Org,
runtime ID `c2b0e45c-9d7a-4dc6-afc6-be29ac43537c`, using the Medium profile.
The registered runtime and intent parser run in the local Docker Compose project
`pr-agent-agntid`. The UI is `http://localhost:18080/`; MCP is
`http://127.0.0.1:18082/mcp`. The portal connection uses server/prefix `pr_agent`
and `http://host.docker.internal:8091/mcp` without the catalog's placeholder
`MCP_AUTH_TOKEN` reference. The callback enforces its single-use execution tickets.

The protected replay exited successfully with these results:

| Operation | Observed result |
| --- | --- |
| Search, limit 5 | Completed, returned 5 matches |
| Read helpers.py | Completed |
| Edit return 2 to return 3 | Completed; resulting file verified |
| Search, limit 100 | Runtime denied; range policy requires 1–20 |
| Read deployment.txt | Completed |
| Delete deployment.txt | Unavailable under Medium; file preserved |

[Reporting](https://app.agntid.ai/reporting) shows `pr-agent` as the acting agent.
Filter by `pr_agent`, then expand the events around **14:10:05 IST**:
`pr_agent.edit_file` has successful Tool Execution entries, while
`pr_agent.search_code` has a Tool Execution denial stating
`limit must satisfy {'min': 1, 'max': 20}`. The denial policy is
`pr_agent-pr_agent.search_code-1anux1vv`.

This is verified input-policy enforcement and tool exclusion on synthetic files.
It is not an end-to-end Slack/LLM/GitHub publishing test. Runtime intent telemetry
also recorded uncertain-alignment review notices for reads/searches while allowing
them; do not present those notices as proof of an enforced human-approval gate.
The existing Agno publishing approval remains separate.

Live testing found and fixed two interoperability issues: the runtime uses
`agntid_task_id` instead of the SDK wrapper's legacy `_task_id`, and its schema
importer treats nullable `anyOf` inputs as strings. The bridge now sends the
correct context field, exposes concrete optional input schemas, omits null defaults
on outbound calls, and accepts defaults restored by the runtime at the callback.
All 55 tests, Ruff and mypy passed after these fixes.

To repeat with this registered runtime, run from the repository:

```sh
AGNTID_ENABLED=true AGNTID_MCP_URL=http://127.0.0.1:18082/mcp \
  AGNTID_ACCESS_TOKEN= REQUIRE_CONFIRMATION=true \
  .venv/bin/python -m scripts.agntid_demo --protected
```

Press Enter once the callback starts; the policies are already saved. The callback
exists only while this process runs. The replay removes its temporary files and
stops the callback when finished; Reporting retains the evidence. The Docker
runtime stays running. Its local compose file and logs are gitignored under
`.agntid-runtime/`; no bootstrap credential was added to repository configuration.
After changing callback schemas, restart only this demo runtime so it rediscovers
them (Reload MCP Config alone did not replace cached schemas):

```sh
docker compose -p pr-agent-agntid -f .agntid-runtime/compose.yaml restart achr-runtime
```
