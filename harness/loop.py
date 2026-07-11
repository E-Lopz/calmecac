"""The bare ReAct loop: call the model, execute any tool calls, repeat."""

import json
from datetime import datetime, timezone
from pathlib import Path

from harness.ollama_client import chat
from harness.tools import REGISTRY

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agents" / "kukulkan" / "prompt.md"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

TOOL_SCHEMAS = [schema for schema, _ in REGISTRY.values()]


def _log(log_path, event):
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")


def _call_tool(name, arguments):
    """Run a tool call. Returns (result, error) — exactly one is None."""
    if name not in REGISTRY:
        return None, f"unknown tool '{name}'. Available tools: {', '.join(REGISTRY)}"
    if not isinstance(arguments, dict):
        return None, f"malformed arguments for '{name}': expected a JSON object, got {arguments!r}"

    _, func = REGISTRY[name]
    try:
        result = func(**arguments)
    except TypeError as e:
        return None, f"bad arguments for '{name}': {e}"
    except Exception as e:
        return None, f"'{name}' failed: {e}"
    return str(result), None


def run_task(task: str, config) -> str:
    system_prompt = PROMPT_PATH.read_text()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"run_{timestamp}.jsonl"

    max_steps = config.get("max_steps", 10)

    for step in range(1, max_steps + 1):
        prompt_snapshot = list(messages)
        response = chat(messages, tools=TOOL_SCHEMAS)
        message = response["message"]
        messages.append(message)

        _log(log_path, {
            "step": step,
            "type": "model_call",
            "prompt": prompt_snapshot,
            "response": message,
        })

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            content = message.get("content", "")
            print(f"[step {step}] final answer: {content}")
            return content

        content = message.get("content")
        if content:
            print(f"[step {step}] assistant: {content}")

        for call in tool_calls:
            name = call["function"]["name"]
            arguments = call["function"]["arguments"]
            print(f"[step {step}] tool_call: {name}({arguments})")

            result, error = _call_tool(name, arguments)
            tool_content = error if error is not None else result
            print(f"[step {step}] tool_result: {tool_content}")

            messages.append({"role": "tool", "name": name, "content": tool_content})
            _log(log_path, {
                "step": step,
                "type": "tool_call",
                "name": name,
                "arguments": arguments,
                "result": tool_content,
                "error": error is not None,
            })

    abort_message = f"aborted: exceeded max_steps ({max_steps}) without a final answer"
    print(f"[step {max_steps}] {abort_message}")
    _log(log_path, {"step": max_steps, "type": "abort", "message": abort_message})
    return abort_message
