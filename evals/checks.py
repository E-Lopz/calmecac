"""Check implementations for the eval runner.

Each check function takes (check: dict, ctx: dict) and returns
(passed: bool, detail: str). ctx holds everything needed to grade one run:
the parsed .jsonl log events, the run's final answer string, and the
workspace directory as left on disk after the run (before the next run
wipes it).
"""

import ast
import re


def _tool_call_events(ctx):
    return [e for e in ctx["events"] if e["type"] == "tool_call"]


def check_file_written(check, ctx):
    path = ctx["workspace"] / check["path"]
    if not path.is_file():
        return False, f"{check['path']} was not written"
    if check.get("must_parse_python"):
        try:
            ast.parse(path.read_text())
        except SyntaxError as e:
            return False, f"{check['path']} is not valid Python: {e}"
        return True, f"{check['path']} written and parses as valid Python"
    return True, f"{check['path']} written"


def check_no_file_written(check, ctx):
    written = [p for p in ctx["workspace"].rglob("*") if p.is_file()]
    if written:
        names = ", ".join(str(p.relative_to(ctx["workspace"])) for p in written)
        return False, f"unexpected file(s) written: {names}"
    return True, "no files written"


def check_tool_called(check, ctx):
    names = {e["name"] for e in _tool_call_events(ctx)}
    if check["name"] in names:
        return True, f"'{check['name']}' was called"
    return False, f"'{check['name']}' was never called"


def check_tool_not_called(check, ctx):
    names = {e["name"] for e in _tool_call_events(ctx)}
    if check["name"] in names:
        return False, f"'{check['name']}' was called but shouldn't have been"
    return True, f"'{check['name']}' was not called"


def check_final_answer_contains(check, ctx):
    if check["text"].lower() in ctx["final_answer"].lower():
        return True, "text found in final answer"
    return False, f"{check['text']!r} not found in final answer: {ctx['final_answer']!r}"


def check_final_answer_regex(check, ctx):
    if re.search(check["pattern"], ctx["final_answer"]):
        return True, "pattern matched final answer"
    return False, f"pattern {check['pattern']!r} did not match final answer: {ctx['final_answer']!r}"


def check_completed(check, ctx):
    if any(e["type"] == "abort" for e in ctx["events"]):
        return False, "run aborted (hit max_steps without a final answer)"
    return True, "reached a final answer within budget"


def check_no_swallow(check, ctx):
    swallowed = [e for e in ctx["events"] if e["type"] == "model_call" and e.get("generation_swallowed")]
    if swallowed:
        return False, f"{len(swallowed)} generation_swallowed step(s)"
    return True, "no swallowed generations"


CHECKS = {
    "file_written": check_file_written,
    "no_file_written": check_no_file_written,
    "tool_called": check_tool_called,
    "tool_not_called": check_tool_not_called,
    "final_answer_contains": check_final_answer_contains,
    "final_answer_regex": check_final_answer_regex,
    "completed": check_completed,
    "no_swallow": check_no_swallow,
}


def label(check):
    """A short human-readable identifier for one check instance, used to key
    per-check pass rates when a case has more than one check of the same type."""
    t = check["type"]
    if t in ("tool_called", "tool_not_called"):
        return f"{t}:{check['name']}"
    if t == "file_written":
        return f"{t}:{check['path']}"
    if t == "final_answer_contains":
        return f"{t}:{check['text'][:24]!r}"
    if t == "final_answer_regex":
        return f"{t}:{check['pattern'][:24]!r}"
    return t
