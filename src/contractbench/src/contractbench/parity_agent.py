"""Harbor port of the original ContractBench agent loop, used for parity.

ContractBench's own harness (``agents/docker_runner.py`` + ``agents/base.py`` +
``agents/providers/openai_agent.py`` in SecurityLab-UCD/ContractBench) drives the
model with a native tool-calling loop rather than a Harbor CLI agent. This module
reproduces that loop inside Harbor so the parity experiment compares the same
agent on both sides, and only the harness differs.

Matched behaviour (original value in parentheses):

- system prompt and the single ``bash`` tool definition, copied verbatim
  (``agents/base.py: SYSTEM_PROMPT``, ``BASH_TOOL_DEFINITION``);
- conversation = system prompt, then the task instruction as the first user
  message;
- OpenAI chat completions with ``tool_choice="auto"``, ``temperature=0.0`` and
  ``max_tokens=16384`` (``LLMAgent`` defaults);
- at most 30 model calls (``MAX_TURNS = 30``);
- a soft deadline checked only before each model call (``run_agent_loop``):
  300 s by default (``AGENT_TIMEOUT = 300``), raised with
  ``run_task_docker.py --timeout``; the parity runs use 600 s on both sides
  (``agent_timeout_sec: 600`` in ``contractbench-parity.yaml``);
- the loop stops on the first assistant turn without tool calls;
- each tool call runs ``bash -c <command>`` in the task container with a 60 s
  limit; output is stdout, then stderr, truncated at 30,000 characters, with
  ``(no output)`` / ``(command timed out after 60s)`` placeholders
  (``_exec_in_container``).

Harbor's own agent timeout is set above the soft deadline in the parity config,
so a trial ends the way it does upstream: at the first turn boundary after the
deadline.

Harbor 0.25's Docker environment decodes command output with
``errors="replace"`` (as upstream does since its binary-output fix) and signals
a command timeout with ``RuntimeError("Command timed out after ...")``, which
``_exec`` maps to upstream's ``(command timed out after 60s)`` observation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

MAX_TURNS = 30
MAX_OUTPUT_CHARS = 30_000
AGENT_TIMEOUT = 300
COMMAND_TIMEOUT = 60

BASH_TOOL_DEFINITION = {
    "name": "bash",
    "description": (
        "Execute a bash command in the task environment. "
        "The environment has curl, python3, and standard Unix tools. "
        "A web server is running at http://localhost:8080. "
        "Use this tool to interact with the server and complete the task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            }
        },
        "required": ["command"],
    },
}

SYSTEM_PROMPT = """\
You are an autonomous agent completing a task inside a Docker container.
You have one tool: `bash` — execute any bash command and see stdout/stderr.

The container has a web server at http://localhost:8080 with the task API.
You have curl, python3, and standard Unix tools available.

Rules:
- Read the task instructions carefully before acting.
- Execute commands one at a time and observe the output.
- When you are finished, stop calling tools and say "DONE".
- Do NOT explain your reasoning at length — act efficiently.
- Preserve URLs, tokens, and other opaque strings EXACTLY as given.
  Do not decode, re-encode, truncate, or reformat them.
"""

# gpt-4o-2024-08-06 list price, USD per token (input, cached input, output).
_PRICES = {"gpt-4o-2024-08-06": (2.50e-6, 1.25e-6, 10.00e-6)}

# Harbor's docker exec merges stderr into stdout. Upstream returns stdout, then
# stderr, so the command is wrapped to capture stderr separately.
_STDERR_MARK = "\x00__stderr_boundary__\x00"


class ContractBenchParityAgent(BaseAgent):
    def __init__(
        self,
        *args,
        max_turns: int = MAX_TURNS,
        agent_timeout_sec: int = AGENT_TIMEOUT,
        temperature: float = 0.0,
        max_tokens: int = 16384,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._max_turns = max_turns
        self._agent_timeout_sec = agent_timeout_sec
        self._temperature = temperature
        self._max_tokens = max_tokens

    @staticmethod
    def name() -> str:
        return "contractbench-parity-agent"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        import openai

        if not self.model_name:
            raise RuntimeError("model_name is required for ContractBenchParityAgent")
        model = self.model_name.split("/", 1)[-1]

        client_kwargs: dict[str, Any] = {"api_key": os.environ["OPENAI_API_KEY"]}
        if os.environ.get("OPENAI_BASE_URL"):
            client_kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        client = openai.OpenAI(**client_kwargs)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction.strip()},
        ]
        tools = [{"type": "function", "function": BASH_TOOL_DEFINITION}]

        n_input = n_cached = n_output = 0
        turns = 0
        status = "max_turns"
        response_models: set[str] = set()
        deadline = time.time() + self._agent_timeout_sec

        try:
            for _ in range(self._max_turns):
                if time.time() > deadline:
                    status = "timeout"
                    break

                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                )
                turns += 1
                response_models.add(response.model)
                if response.usage:
                    n_input += response.usage.prompt_tokens
                    n_output += response.usage.completion_tokens
                    details = response.usage.prompt_tokens_details
                    n_cached += (details.cached_tokens or 0) if details else 0

                msg = response.choices[0].message
                tool_calls = []
                for tc in msg.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append((tc.id, args.get("command", "")))

                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": "bash",
                                        "arguments": json.dumps({"command": command}),
                                    },
                                }
                                for call_id, command in tool_calls
                            ],
                        }
                    )
                else:
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    status = "done"
                    break

                for call_id, command in tool_calls:
                    output = await self._exec(environment, command)
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": output}
                    )
        finally:
            prices = _PRICES.get(model)
            context.n_input_tokens = n_input
            context.n_cache_tokens = n_cached
            context.n_output_tokens = n_output
            if prices is not None:
                context.cost_usd = (
                    (n_input - n_cached) * prices[0]
                    + n_cached * prices[1]
                    + n_output * prices[2]
                )
            context.metadata = {
                "status": status,
                "turns": turns,
                "response_models": sorted(response_models),
            }
            (self.logs_dir / "agent_trace.json").write_text(
                json.dumps(
                    {
                        "model": model,
                        "response_models": sorted(response_models),
                        "status": status,
                        "turns": turns,
                        "total_input_tokens": n_input,
                        "total_output_tokens": n_output,
                        "messages": messages,
                    },
                    indent=2,
                )
            )

    async def _exec(self, environment: BaseEnvironment, command: str) -> str:
        wrapped = (
            '__cb_err=$(mktemp); bash -c {cmd} 2>"$__cb_err"; '
            "printf '{mark}'; cat \"$__cb_err\"; rm -f \"$__cb_err\""
        ).format(cmd=shlex.quote(command), mark=_STDERR_MARK.replace("\x00", "\\0"))
        try:
            result = await environment.exec(wrapped, timeout_sec=COMMAND_TIMEOUT)
        except RuntimeError as e:
            if "timed out" in str(e):
                return f"(command timed out after {COMMAND_TIMEOUT}s)"
            raise

        raw = result.stdout or ""
        stdout, _, stderr = raw.partition(_STDERR_MARK)
        output = ""
        if stdout:
            output += stdout
        if stderr:
            if output:
                output += "\n"
            output += stderr
        if len(output) > MAX_OUTPUT_CHARS:
            output = (
                output[:MAX_OUTPUT_CHARS]
                + f"\n... (truncated, {len(output)} chars total)"
            )
        return output or "(no output)"
