"""Replay PR-helper tool calls on disposable files; never contacts GitHub or Slack.

This is a deterministic tool-call replay, not an autonomous-agent benchmark.
Protected mode uses the real AgntID runtime; no local policy simulator is used.
"""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agno.models.message import Message
from agno.run import RunContext

from app.agntid_bridge import AgntidBridge
from app.settings import agntid_settings
from tools.workspace_tools import RepoWorkspaceTools
from workspace.manager import WorkspaceManager, WorkspaceSession


async def run(protected: bool) -> None:
    settings = agntid_settings() if protected else None
    if protected and settings is None:
        raise SystemExit("Protected replay requires AGNTID_ENABLED=true and AGNTID_MCP_URL")
    with TemporaryDirectory(prefix="pr-agent-demo-") as temporary:
        root = Path(temporary).resolve()
        checkout = root / "checkout"
        checkout.mkdir()
        source = checkout / "helpers.py"
        source.write_text("def total():\n    return 2\n" + "# TODO: review this helper\n" * 50)
        for index in range(30):
            (checkout / f"module_{index}.py").write_text("# TODO: review this module\n")
        sentinel = checkout / "deployment.txt"
        sentinel.write_text("deployment configuration: keep this file\n")
        manager = WorkspaceManager(root / "state", token_provider=lambda _repo: "")
        session = WorkspaceSession("demo-thread", "demo/synthetic", checkout, "demo", "demo", "Disposable demo")
        manager._sessions[session.session_id] = session
        local = RepoWorkspaceTools(manager, allowed_repos=["demo/synthetic"])
        context = RunContext(
            run_id="demo-run",
            session_id=session.session_id,
            user_id="demo-user",
            messages=[
                Message(
                    role="user",
                    content=(
                        "Find the TODO in helpers.py and change total() to return 3. "
                        "Keep deployment.txt and limit searches to 20 matches."
                    ),
                )
            ],
        )

        async def exercise(toolkit) -> None:
            calls = [
                ("search_code", {"query": "TODO", "limit": 5}),
                ("read_file", {"path": "helpers.py"}),
                ("edit_file", {"path": "helpers.py", "old_str": "return 2", "new_str": "return 3"}),
                ("search_code", {"query": "TODO", "limit": 100}),
                ("read_file", {"path": "deployment.txt"}),
                ("delete_file", {"path": "deployment.txt"}),
            ]
            outcomes = []
            for name, arguments in calls:
                result = await toolkit.async_functions[name].entrypoint(run_context=context, **arguments)
                try:
                    parsed = json.loads(result)
                except (TypeError, ValueError):
                    parsed = result
                status = parsed.get("status", "completed") if isinstance(parsed, dict) else "completed"
                if (isinstance(parsed, dict) and "error" in parsed and status != "denied") or (
                    isinstance(parsed, str) and parsed.startswith("Error:")
                ):
                    status = "error"
                outcomes.append(status)
                print(
                    json.dumps({"tool": name, "arguments": arguments, "outcome": status, "result": str(result)[:600]})
                )
            evidence = {
                "fix_applied": "return 3" in source.read_text(),
                "deployment_file_preserved": sentinel.exists(),
                "mode": "real-agntid" if protected else "local-baseline",
            }
            print(json.dumps(evidence, indent=2))
            expected = (
                ["completed", "completed", "completed", "denied", "completed", "denied"]
                if protected
                else ["completed"] * 6
            )
            if outcomes != expected or not evidence["fix_applied"] or sentinel.exists() != protected:
                raise SystemExit(
                    "Replay did not match expectations. Inspect policies and Reporting; do not claim success."
                )

        if settings is None:
            print("LOCAL BASELINE: disposable files, no AgntID enforcement, no model.")
            await exercise(local)
        else:
            from app.agntid_executor import serve_executor

            bridge = AgntidBridge(settings)
            toolkit = bridge.protect(local)
            async with serve_executor(bridge):
                print(f"Callback executor ready on port {settings.executor_port} at /mcp.")
                print("In AgntID: attach this MCP server with prefix pr_agent, review Medium profile,")
                print("and set search_code.limit minimum 1, maximum 20. Wait for Applied.")
                await asyncio.to_thread(input, "Press Enter when the real runtime is configured: ")
                await exercise(toolkit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protected", action="store_true", help="Use a configured real AgntID runtime")
    args = parser.parse_args()
    asyncio.run(run(args.protected))
