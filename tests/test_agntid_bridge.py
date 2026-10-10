"""Policy denials must never execute locally, even on errors or resumed approvals."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agno.models.message import Message
from agno.run import RunContext
from agno.tools import Toolkit

from app.agntid_bridge import TICKET_FIELD, AgntidBridge, BridgeSettings, ExecutionBroker, capture_request


def context(session="thread-a"):
    return RunContext(
        run_id="run-1",
        session_id=session,
        user_id="user-1",
        metadata={"user_name": "requester"},
        messages=[Message(role="user", content="Read this workspace")],
    )


class LocalTools(Toolkit):
    def __init__(self):
        self.calls = []
        super().__init__(tools=[self.read, self.publish], requires_confirmation_tools=["publish"], cache_results=True)

    def read(self, run_context: RunContext, limit: int = 20) -> str:
        """Read records from this workspace with a bounded result count."""
        self.calls.append((run_context.session_id, limit))
        return json.dumps({"session": run_context.session_id, "limit": limit})

    def publish(self, title: str) -> str:
        """Publish an approved draft."""
        self.calls.append(title)
        return title


class FakeSDK:
    """Test double, not an AgntID enforcement implementation."""

    def __init__(self):
        self.mode = "allow"
        self.closed = []
        self.opened = []
        self.broker = None
        self.serial = 0

    @asynccontextmanager
    async def AgntidMCPClient(self, url, access_token=None):
        if self.mode == "offline":
            raise ConnectionError("Secret data should not appear in the response")
        yield self

    def create_task(self, agent_id, user_id, prompt):
        self.serial += 1
        return f"task-{self.serial}"

    @asynccontextmanager
    async def task_context(self, client, task_id):
        try:
            yield
        finally:
            self.closed.append(task_id)

    async def send_task_open_checked(self, client, task_id, agent_id, user_id, prompt):
        self.opened.append((agent_id, user_id, prompt))
        return self.mode != "reject_task", "decision"

    async def get_tools_list(self, client):
        return [] if self.mode == "hidden" else [SimpleNamespace(name=f"pr_agent_{n}") for n in ("read", "publish")]

    def wrap_client(self, client, task_id, *, task_argument):
        assert task_argument == "agntid_task_id"
        return self

    async def call_tool_async(self, name, arguments):
        if self.mode == "deny":
            return SimpleNamespace(denied=True)
        if self.mode == "timeout":
            await asyncio.sleep(10)
        if self.mode == "fake_success":
            return {"result": "success without execution"}
        if self.mode == "tamper":
            arguments["limit"] = 999
        ticket = arguments.pop(TICKET_FIELD)
        value = await self.broker.execute(name.removeprefix("pr_agent_"), arguments, ticket)
        return SimpleNamespace(value=value)


def setup_bridge(timeout=1):
    sdk = FakeSDK()
    bridge = AgntidBridge(BridgeSettings("http://localhost:8080/mcp", timeout=timeout), sdk=sdk)
    sdk.broker = bridge.broker
    local = LocalTools()
    protected = bridge.protect(local)
    return bridge, sdk, local, protected


@pytest.mark.asyncio
async def test_allowed_call_executes_once_in_trusted_session():
    bridge, sdk, local, protected = setup_bridge()
    result = await protected.async_functions["read"].entrypoint(run_context=context(), limit=10)
    assert json.loads(result) == {"session": "thread-a", "limit": 10}
    assert local.calls == [("thread-a", 10)]
    assert sdk.opened == [("pr-agent", "user-1", "Read this workspace")]
    assert sdk.closed == ["task-1"]
    assert not bridge.broker.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deny", "reject_task", "hidden", "offline", "timeout", "tamper", "fake_success"])
async def test_denial_or_failure_never_falls_back_to_local_execution(mode):
    bridge, sdk, local, protected = setup_bridge(timeout=0.05)
    sdk.mode = mode
    result = json.loads(await protected.async_functions["read"].entrypoint(run_context=context()))
    assert result["status"] in {"denied", "error"}
    assert not local.calls
    assert not bridge.broker.pending
    assert "Secret data" not in str(result)
    if mode != "offline":
        assert sdk.closed == ["task-1"]


@pytest.mark.asyncio
async def test_missing_request_fails_closed():
    _, sdk, local, protected = setup_bridge()
    ctx = context()
    ctx.messages = []
    result = json.loads(await protected.async_functions["read"].entrypoint(run_context=ctx))
    assert result["status"] == "error"
    assert not sdk.opened and not local.calls


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_share_context():
    _, _, local, protected = setup_bridge()
    await asyncio.gather(
        *[
            protected.async_functions["read"].entrypoint(run_context=context(session), limit=index)
            for index, session in enumerate(["thread-a", "thread-b"])
        ]
    )
    assert set(local.calls) == {("thread-a", 0), ("thread-b", 1)}


def test_model_schema_hides_context_and_tickets_and_preserves_approval():
    _, _, _, protected = setup_bridge()
    for function in protected.get_async_functions().values():
        function.process_entrypoint()
        assert "run_context" not in function.parameters["properties"]
        assert TICKET_FIELD not in function.parameters["properties"]
        assert not function.cache_results
    assert protected.async_functions["publish"].requires_confirmation
    assert protected.functions["publish"].requires_confirmation
    assert set(protected.async_functions["publish"].parameters["properties"]) == {"title"}
    with pytest.raises(RuntimeError, match="arun"):
        protected.functions["read"].entrypoint(run_context=context())


@pytest.mark.asyncio
async def test_execution_tickets_reject_replay_expiration_and_modified_calls():
    broker = ExecutionBroker()
    local = LocalTools()
    broker.methods["read"] = local.read
    arguments = {"limit": 20}
    ticket = broker.issue("read", arguments, context(), ttl=1)
    await broker.execute("read", arguments, ticket)
    with pytest.raises(PermissionError):
        await broker.execute("read", arguments, ticket)
    expired = broker.issue("read", arguments, context(), ttl=-1)
    with pytest.raises(PermissionError):
        await broker.execute("read", arguments, expired)
    wrong = broker.issue("read", arguments, context(), ttl=1)
    with pytest.raises(PermissionError):
        await broker.execute("publish", {"title": "bypass"}, wrong)
    with pytest.raises(PermissionError):
        await broker.execute("read", arguments, "invented")
    assert local.calls == [("thread-a", 20)]


@pytest.mark.parametrize("url", ["", "file:///tmp/a", "http://user:secret@host/mcp", "https://host/mcp?token=secret"])
def test_invalid_runtime_configuration_fails_at_startup(url):
    with pytest.raises(ValueError):
        BridgeSettings(url)


@pytest.mark.asyncio
async def test_real_mcp_executor_transport_and_tool_schema(unused_tcp_port):
    pytest.importorskip("agntid")
    from fastmcp import Client

    from app.agntid_executor import serve_executor

    sdk = FakeSDK()
    bridge = AgntidBridge(BridgeSettings("http://localhost:8080/mcp", executor_port=unused_tcp_port), sdk=sdk)
    local = LocalTools()
    bridge.protect(local)
    async with serve_executor(bridge):
        async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp") as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert set(tools["read"].inputSchema["properties"]) == {"limit", TICKET_FIELD}
            with pytest.raises(Exception, match="execution ticket"):
                await client.call_tool("read", {"limit": 20, TICKET_FIELD: "forged"})
            ticket = bridge.broker.issue("read", {"limit": 20}, context(), ttl=10)
            import agntid

            result = await agntid.wrap_client(client).call_tool_async("read", {"limit": 20, TICKET_FIELD: ticket})
            assert "thread-a" in str(result)
            assert local.calls == [("thread-a", 20)]
            with pytest.raises(Exception, match="execution ticket"):
                await client.call_tool("read", {"limit": 20, TICKET_FIELD: ticket})


@pytest.mark.asyncio
async def test_nullable_arguments_survive_runtime_schema_import(unused_tcp_port):
    pytest.importorskip("agntid")
    from fastmcp import Client

    from app.agntid_executor import serve_executor

    class OptionalTools(Toolkit):
        def __init__(self):
            super().__init__(tools=[self.read])

        def read(self, start: int | None = None) -> str:
            return json.dumps({"start": start})

    class RuntimeSDK(FakeSDK):
        async def call_tool_async(self, name, arguments):
            # Live runtime requires integer schema, rejects explicit null, and
            # forwards omitted defaults to the real callback transport.
            assert arguments.get("start", 0) is not None
            arguments.setdefault("start", None)
            import agntid

            return await agntid.wrap_client(self.client).call_tool_async(name.removeprefix("pr_agent_"), arguments)

    sdk = RuntimeSDK()
    bridge = AgntidBridge(BridgeSettings("http://localhost/mcp", executor_port=unused_tcp_port), sdk=sdk)
    protected = bridge.protect(OptionalTools())
    async with serve_executor(bridge):
        async with Client(f"http://127.0.0.1:{unused_tcp_port}/mcp") as client:
            sdk.client = client
            schema = (await client.list_tools())[0].inputSchema
            assert schema["properties"]["start"]["type"] == "integer"
            assert "start" not in schema["required"]
            for start in (None, 3):
                result = await protected.async_functions["read"].entrypoint(run_context=context(), start=start)
                assert json.loads(result) == {"start": start}
                assert not bridge.broker.pending


@pytest.mark.asyncio
async def test_agno_run_injects_context_and_pauses_before_issuing_publish_ticket():
    from agno.agent import Agent
    from agno.db.in_memory import InMemoryDb
    from agno.models.base import Model
    from agno.models.response import ModelResponse

    class ToolModel(Model):
        def __init__(self, tool, arguments):
            super().__init__(id="offline-tool-model", name="offline-tool-model", provider="test")
            self.tool = tool
            self.arguments = arguments
            self.sent = False

        def invoke(self, *args, **kwargs):
            if self.sent:
                return ModelResponse(role="assistant", content="Done")
            self.sent = True
            return ModelResponse(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": self.tool, "arguments": json.dumps(self.arguments)},
                    }
                ],
            )

        async def ainvoke(self, *args, **kwargs):
            return self.invoke(*args, **kwargs)

        def invoke_stream(self, *args, **kwargs):
            yield self.invoke(*args, **kwargs)

        async def ainvoke_stream(self, *args, **kwargs):
            yield self.invoke(*args, **kwargs)

        def _parse_provider_response(self, response, **kwargs):
            return response

        def _parse_provider_response_delta(self, response):
            return response

    bridge, sdk, local, toolkit = setup_bridge()
    reader = Agent(model=ToolModel("read", {"limit": 7}), tools=[toolkit], telemetry=False)
    await reader.arun("Read this workspace", session_id="real-thread", user_id="real-user")
    assert local.calls == [("real-thread", 7)]
    assert sdk.opened[0][1:] == ("real-user", "Read this workspace")

    _, publish_sdk, publisher, publish_tools = setup_bridge()
    writer = Agent(
        model=ToolModel("publish", {"title": "Fix the bug"}),
        tools=[publish_tools],
        db=InMemoryDb(),
        telemetry=False,
        pre_hooks=[capture_request],
    )
    paused = await writer.arun("Publish the fix", session_id="publish-thread", user_id="real-user")
    assert paused.is_paused
    assert not publisher.calls and not publish_sdk.opened
    paused.requirements[0].confirm()
    resumed = await writer.acontinue_run(
        run_id=paused.run_id, requirements=paused.requirements, session_id="publish-thread"
    )
    assert publisher.calls == ["Fix the bug"], [t.result for t in resumed.tools]
    assert publish_sdk.opened[0][1] == "real-user"
    assert not bridge.broker.pending


@pytest.mark.asyncio
async def test_all_existing_repository_tools_have_protected_mcp_schemas(tmp_path):
    pytest.importorskip("agntid")
    from app.credentials import GitHubCredentials
    from tools.github_repo import GitHubRepoTools
    from tools.publish import PublishTools
    from tools.workspace_tools import RepoWorkspaceTools
    from workspace.manager import WorkspaceManager

    manager = WorkspaceManager(tmp_path, token_provider=lambda _repo: "")
    github = GitHubRepoTools(credentials=GitHubCredentials(access_token="test-only-token"), repos=["demo/repo"])
    bridge = AgntidBridge(BridgeSettings("http://localhost:8080/mcp"), sdk=FakeSDK())
    originals = [RepoWorkspaceTools(manager, ["demo/repo"]), github, PublishTools(manager, github)]
    for original in originals:
        protected = bridge.protect(original)
        for function in protected.get_async_functions().values():
            function.process_entrypoint()
            assert "run_context" not in function.parameters["properties"]
            assert TICKET_FIELD not in function.parameters["properties"]
    tools = await bridge.executor().list_tools()
    assert len(tools) == sum(len(t.functions) for t in originals)
    for tool in tools:
        assert TICKET_FIELD in tool.inputSchema["required"]
        assert "run_context" not in tool.inputSchema["properties"]
