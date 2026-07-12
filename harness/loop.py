"""The bare ReAct loop: call the model, execute any tool calls, repeat."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from harness.ollama_client import chat
from harness.tools import REGISTRY, SKILL_NAMES

PROMPT_PATH = Path(__file__).resolve().parent.parent / "agents" / "kukulkan" / "prompt.md"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

TOOL_SCHEMAS = [schema for schema, _ in REGISTRY.values()]

# NOTES.md findings 9/11: a no-tool-call response ends the run, but the model
# sometimes fills that final response with a statement of intent ("I will
# proceed...") instead of the tool call itself. Both a system-prompt rule and
# a worked example labeling this exact pattern WRONG produced null results —
# prompting alone doesn't fix it. This is the harness-level fallback: catch
# the pattern and force one more turn instead of ending the run on it.
INTENT_PATTERNS = [
    re.compile(r"\bI will (now )?(proceed|start|begin|create|write)\b", re.IGNORECASE),
    re.compile(r"\blet'?s (start|begin|proceed)\b", re.IGNORECASE),
    re.compile(r"\bproceeding to\b", re.IGNORECASE),
]

NUDGE_MESSAGE = (
    "Your previous response ended the task without taking any action — a response "
    "with no tool calls is final. Either make the tool call(s) now, or state your "
    "final answer without announcing intent."
)


def _looks_like_intent(content):
    return any(p.search(content) for p in INTENT_PATTERNS)


def _log(log_path, event):
    with open(log_path, "a") as f:
        f.write(json.dumps(event) + "\n")


def _abort_stats(tool_records):
    """Classify an aborted run from its executed tool calls: distinct successful
    calls, repeated ones, and errors — "progressing" if it was clean linear
    progress, "possibly-stuck" otherwise."""
    successful = [
        (r["name"], json.dumps(r["arguments"], sort_keys=True))
        for r in tool_records if not r["error"]
    ]
    errors = sum(1 for r in tool_records if r["error"])
    distinct = len(set(successful))
    repeats = len(successful) - distinct
    verdict = "progressing" if repeats == 0 and errors == 0 else "possibly-stuck"
    return len(successful), distinct, repeats, errors, verdict


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


def run_task(task: str, config, source="cli") -> str:
    max_steps = config.get("max_steps", 10)
    system_prompt = PROMPT_PATH.read_text() + f"\n\nYou have a budget of {max_steps} steps for this task."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"run_{timestamp}.jsonl"

    _log(log_path, {
        "type": "run_start",
        "messages": messages,
        "tool_names": list(REGISTRY),
        "source": source,
    })

    tool_records = []
    nudge_used = False

    for step in range(1, max_steps + 1):
        prompt_snapshot = list(messages)
        response = chat(messages, tools=TOOL_SCHEMAS)
        message = response["message"]
        messages.append(message)

        eval_count = response.get("eval_count")
        prompt_eval_count = response.get("prompt_eval_count")
        done_reason = response.get("done_reason")
        swallowed = (
            not message.get("content")
            and not message.get("tool_calls")
            and eval_count is not None
            and eval_count > 20
        )

        tool_calls = message.get("tool_calls")
        content = message.get("content", "")
        is_intent_nudge = not tool_calls and not nudge_used and _looks_like_intent(content)

        model_call_entry = {
            "step": step,
            "type": "model_call",
            "prompt": prompt_snapshot,
            "response": message,
            "eval_count": eval_count,
            "prompt_eval_count": prompt_eval_count,
            "done_reason": done_reason,
        }
        if swallowed:
            model_call_entry["generation_swallowed"] = True
            print(f"WARNING: model generated {eval_count} tokens but nothing surfaced "
                  "(parser swallow or discarded thinking)")
        if is_intent_nudge:
            model_call_entry["intent_nudge"] = True
        _log(log_path, model_call_entry)

        if not tool_calls:
            if is_intent_nudge:
                nudge_used = True
                messages.append({"role": "system", "content": NUDGE_MESSAGE})
                print(f"[step {step}] intent nudge issued (declared intent instead of acting)")
                continue
            print(f"[step {step}] final answer: {content}")
            return content

        if content:
            print(f"[step {step}] assistant: {content}")

        call_keys = [
            (c["function"]["name"], json.dumps(c["function"]["arguments"], sort_keys=True))
            for c in tool_calls
        ]

        for call, key in zip(tool_calls, call_keys):
            name = call["function"]["name"]
            arguments = call["function"]["arguments"]
            is_duplicate = call_keys.count(key) > 1
            if is_duplicate:
                print(f"[step {step}] warning: duplicate tool call {name}({arguments})")
            print(f"[step {step}] tool_call: {name}({arguments})")

            result, error = _call_tool(name, arguments)
            tool_content = error if error is not None else result
            print(f"[step {step}] tool_result: {tool_content}")

            messages.append({"role": "tool", "name": name, "content": tool_content})
            tool_records.append({"name": name, "arguments": arguments, "error": error is not None})

            log_entry = {
                "step": step,
                "type": "tool_call",
                "name": name,
                "arguments": arguments,
                "result": tool_content,
                "result_head": tool_content[:300],
                "error": error is not None,
            }
            if is_duplicate:
                log_entry["duplicate_call"] = True
            if name in SKILL_NAMES:
                log_entry["skill_loaded"] = name
                if arguments:
                    log_entry["skill_args_discarded"] = arguments
            _log(log_path, log_entry)

    successful, distinct, repeats, errors, verdict = _abort_stats(tool_records)
    abort_message = (
        f"aborted: exceeded max_steps ({max_steps}) without a final answer — "
        f"{distinct} distinct successful calls, {repeats} repeats, {errors} errors — {verdict}"
    )
    print(f"[step {max_steps}] {abort_message}")
    _log(log_path, {
        "step": max_steps,
        "type": "abort",
        "message": abort_message,
        "successful_calls": successful,
        "distinct_calls": distinct,
        "repeats": repeats,
        "error_calls": errors,
        "verdict": verdict,
    })
    return abort_message
