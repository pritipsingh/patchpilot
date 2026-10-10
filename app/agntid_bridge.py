"""Route repository tools through AgntID without exposing local execution to the model.

The runtime calls back into a private MCP executor. A single-use ticket binds that
callback to an exact tool, arguments and framework-owned session. The ticket is
issued only when Agno actually invokes the tool (after any approval pause).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, get_args
from urllib.parse import urlparse

from agno.run import RunContext
from agno.run.agent import RunInput
from agno.tools import Toolkit
from pydantic import TypeAdapter, WithJsonSchema

logger = logging.getLogger(__name__)
TICKET_FIELD = "execution_ticket"
REQUEST_STATE = "agntid_request_prompts"


def capture_request(run_context: RunContext, run_input: RunInput) -> None:
    """Persist the original brief across an approval pause, keyed by framework run ID."""
    prompt = run_input.input_content_string()
    if not prompt.strip():
        raise ValueError("AgntID requires a non-empty user request")
    if run_context.session_state is None:
        run_context.session_state = {}
    requests = run_context.session_state.setdefault(REQUEST_STATE, {})
    requests[run_context.run_id] = prompt
    # Bound history; an exceptionally old paused run fails closed after eviction.
    while len(requests) > 32:
        del requests[next(iter(requests))]


@dataclass(frozen=True)
class BridgeSettings:
    url: str
    tool_prefix: str = "pr_agent_"
    access_token: str | None = None
    timeout: float = 300
    executor_host: str = "127.0.0.1"
    executor_port: int = 8091

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("AGNTID_MCP_URL must be an HTTP(S) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Use AGNTID_ACCESS_TOKEN instead of credentials in the runtime URL")
        if not self.tool_prefix or self.timeout <= 0 or not 1 <= self.executor_port <= 65535:
            raise ValueError("Invalid AgntID tool prefix, timeout, or executor port")


def canonical(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass
class PendingCall:
    tool: str
    arguments: str
    context: RunContext
    expires: float
    completed: bool = False


class ExecutionBroker:
    """Process-local, one-shot capabilities; never accepts model-provided identity."""

    def __init__(self) -> None:
        self.methods: dict[str, Callable[..., Any]] = {}
        self.pending: dict[str, PendingCall] = {}

    def issue(self, tool: str, arguments: dict[str, Any], context: RunContext, ttl: float) -> str:
        if not context.session_id or not context.run_id:
            raise ValueError("Protected tools require a framework session and run")
        now = time.monotonic()
        for ticket in list(self.pending):
            if self.pending[ticket].expires <= now:
                del self.pending[ticket]
        # Keep only the trusted fields the existing tools use, not messages or secrets.
        snapshot = RunContext(
            run_id=context.run_id,
            session_id=context.session_id,
            user_id=context.user_id,
            metadata={"user_name": (context.metadata or {}).get("user_name")},
        )
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = PendingCall(tool, canonical(arguments), snapshot, now + ttl)
        return ticket

    def discard(self, ticket: str) -> None:
        self.pending.pop(ticket, None)

    async def execute(self, tool: str, arguments: dict[str, Any], ticket: str) -> Any:
        # No await before consumption: duplicate callbacks cannot execute twice.
        call = self.pending.pop(ticket, None)
        if call is None or call.expires <= time.monotonic():
            raise PermissionError("Missing, expired, or already consumed execution ticket")
        if call.tool != tool or call.arguments != canonical(arguments):
            raise PermissionError("Execution ticket does not match this tool call")
        method = self.methods[tool]
        kwargs = json.loads(call.arguments)
        if "run_context" in inspect.signature(method).parameters:
            kwargs["run_context"] = call.context
        result = await asyncio.to_thread(method, **kwargs)
        call.completed = True
        return result


def request_prompt(context: RunContext) -> str:
    """Use framework messages, never a task/identity argument exposed to the model."""
    captured = (context.session_state or {}).get(REQUEST_STATE, {}).get(context.run_id)
    if isinstance(captured, str) and captured.strip():
        return captured
    for message in reversed(context.messages or []):
        if message.role == "user" and isinstance(message.content, str) and message.content.strip():
            return message.content
    raise ValueError("No user request is available in the run context; refusing protected execution")


class AgntidBridge:
    def __init__(self, settings: BridgeSettings, *, sdk: Any = None) -> None:
        if sdk is None:
            try:
                import agntid as sdk
            except ImportError as exc:
                raise RuntimeError("Install the agntid extra before enabling AGNTID_ENABLED") from exc
        self.sdk = sdk
        self.settings = settings
        self.broker = ExecutionBroker()

    async def call(self, name: str, arguments: dict[str, Any], context: RunContext) -> str:
        """No fallback, retry, or direct tool invocation after a runtime failure."""
        task_id: str | None = None
        ticket: str | None = None
        try:
            prompt = request_prompt(context)
            sdk = self.sdk
            async with asyncio.timeout(self.settings.timeout):
                async with sdk.AgntidMCPClient(self.settings.url, access_token=self.settings.access_token) as client:
                    task_id = sdk.create_task("pr-agent", context.user_id or "anonymous-user", prompt)
                    async with sdk.task_context(client, task_id):
                        ok, _reason = await sdk.send_task_open_checked(
                            client, task_id, "pr-agent", context.user_id or "anonymous-user", prompt
                        )
                        if not ok:
                            return self._outcome(name, task_id, "denied", "AgntID refused to open this task")
                        remote_name = self.settings.tool_prefix + name
                        tools = await sdk.get_tools_list(client)
                        if remote_name not in {tool["name"] if isinstance(tool, dict) else tool.name for tool in tools}:
                            return self._outcome(name, task_id, "denied", "Tool is not available under runtime policy")
                        ticket = self.broker.issue(name, arguments, context, self.settings.timeout)
                        pending_call = self.broker.pending[ticket]
                        # The live runtime advertises agntid_task_id; the SDK's
                        # legacy default _task_id is silently ignored there.
                        wire_arguments = {
                            key: value
                            for key, value in arguments.items()
                            if value is not None
                            or inspect.signature(self.broker.methods[name]).parameters[key].default is not None
                        }
                        result = await sdk.wrap_client(client, task_id, task_argument="agntid_task_id").call_tool_async(
                            remote_name, {**wire_arguments, TICKET_FIELD: ticket}
                        )
                        if getattr(result, "denied", False):
                            return self._outcome(name, task_id, "denied", "AgntID denied this tool call; see Reporting")
                        # A successful MCP reply without a callback is not execution evidence.
                        if not pending_call.completed:
                            return self._outcome(name, task_id, "error", "Runtime did not execute the protected tool")
                        value = getattr(result, "value", result)
                        if isinstance(value, dict) and "pr_agent_output" in value:
                            value = value["pr_agent_output"]
                        logger.info("agntid tool=%s task=%s outcome=completed", name, task_id)
                        return value if isinstance(value, str) else json.dumps(value)
        except Exception as exc:
            # Exception messages may include HTTP headers, signed tickets, or tool data.
            logger.warning("agntid tool=%s task=%s failure=%s", name, task_id, type(exc).__name__)
            return self._outcome(
                name,
                task_id,
                "error",
                "Protected call failed. Execution may have started; inspect Reporting before retrying.",
            )
        finally:
            if ticket is not None:
                self.broker.discard(ticket)

    @staticmethod
    def _outcome(name: str, task_id: str | None, status: str, message: str) -> str:
        logger.info("agntid tool=%s task=%s outcome=%s", name, task_id, status)
        return json.dumps({"status": status, "tool": name, "agntid_task_id": task_id, "error": message})

    def protect(self, toolkit: Toolkit) -> Toolkit:
        sync_tools = []
        async_tools = []
        for name, function in toolkit.functions.items():
            method = function.entrypoint
            if method is None or name in self.broker.methods:
                raise ValueError(f"Missing or duplicate protected tool: {name}")
            self.broker.methods[name] = method
            sync, asynchronous = self._proxies(name, method)
            sync_tools.append(sync)
            async_tools.append((asynchronous, name))
        return Toolkit(
            name=toolkit.name,
            tools=sync_tools,
            async_tools=async_tools,
            requires_confirmation_tools=toolkit.requires_confirmation_tools,
            # Every call must reach current policy, even if the underlying read was cached.
            cache_results=False,
        )

    def _proxies(self, name: str, method: Callable[..., Any]) -> tuple[Callable[..., Any], Callable[..., Any]]:
        signature = inspect.signature(method, eval_str=True)
        parameters = list(signature.parameters.values())
        if "run_context" not in signature.parameters:
            parameters.insert(
                0, inspect.Parameter("run_context", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=RunContext)
            )
        signature = signature.replace(parameters=parameters)

        def sync(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("AgntID protected tools require Agent.arun(); synchronous execution is disabled")

        async def asynchronous(*args: Any, **kwargs: Any) -> str:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = dict(bound.arguments)
            context = arguments.pop("run_context")
            return await self.call(name, arguments, context)

        proxies: list[Any] = [sync, asynchronous]
        for proxy in proxies:
            proxy.__name__ = name
            proxy.__doc__ = method.__doc__
            proxy.__signature__ = signature
            proxy.__annotations__ = {p.name: p.annotation for p in parameters}
            proxy.__annotations__["return"] = str
        return sync, asynchronous

    def executor(self) -> Any:
        """Private callback server: individual tool schemas remain visible to AgntID."""
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings

        # Docker's host alias is an expected callback host; keep rebinding checks.
        server = FastMCP(
            "PatchPilot executor",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[
                    "127.0.0.1:*",
                    "localhost:*",
                    "host.docker.internal:*",
                    f"{self.settings.executor_host}:*",
                ],
                allowed_origins=[],
            ),
        )
        for name, method in self.broker.methods.items():
            signature = inspect.signature(method, eval_str=True)
            parameters = [p for p in signature.parameters.values() if p.name != "run_context"]
            # Runtime 2.0 imports nullable anyOf schemas as strings. Represent
            # optional scalars as their concrete type with an omitted default;
            # the callback restores None and the ticket still binds full args.
            parameters = [
                p.replace(
                    annotation=Annotated[
                        p.annotation,
                        WithJsonSchema(
                            TypeAdapter(next(t for t in get_args(p.annotation) if t is not type(None))).json_schema()
                        ),
                    ]
                )
                if p.default is None and len(get_args(p.annotation)) == 2 and type(None) in get_args(p.annotation)
                else p
                for p in parameters
            ]
            parameters.append(inspect.Parameter(TICKET_FIELD, inspect.Parameter.KEYWORD_ONLY, annotation=str))

            def make_handler(tool_name: str) -> Callable[..., Any]:
                async def handler(**kwargs: Any) -> dict[str, str]:
                    ticket = kwargs.pop(TICKET_FIELD)
                    output = await self.broker.execute(tool_name, kwargs, ticket)
                    return {"pr_agent_output": output}

                return handler

            handler = make_handler(name)
            handler.__name__ = name
            handler.__doc__ = method.__doc__
            handler.__signature__ = signature.replace(  # type: ignore[attr-defined]
                parameters=parameters, return_annotation=dict[str, str]
            )
            handler.__annotations__ = {p.name: p.annotation for p in parameters}
            handler.__annotations__["return"] = dict[str, str]
            server.add_tool(handler, name=name)
        return server
