"""Discord-agnostic history/digest construction, extracted verbatim from
harness/discord_gateway.py (Phase 6b) so it can be imported without pulling
in Discord bot credentials, config, or a live discord.Client() — e.g. from
evals/run.py. No logic changes from the gateway versions of these functions;
see harness/discord_gateway.py's own history for the Phase 6a/6b design
rationale (FINDING_DRAFT.md, NOTES.md finding 12).
"""

import json

MAX_HISTORY_EXCHANGES = 6
MAX_HISTORY_TOKENS = 2000
MAX_TOOL_DIGEST_LINES = 3
MAX_DIGEST_ARGS_LEN = 200
MAX_DIGEST_RESULT_LEN = 1200

# harness/tools.py's fetch_url wraps every fetched body in these constant
# banner lines (they're harness-authored, not part of the fetched content).
# Stripped before digesting so the head-anchored cap below spends its budget
# on the actual response instead of this fixed framing text.
FETCH_BANNER_PREFIX = "--- FETCHED CONTENT from "
FETCH_BANNER_SUFFIX = "\n--- END FETCHED CONTENT ---"


def _estimate_tokens(messages):
    return sum(len(m["content"]) for m in messages) // 4


def _exchange_to_messages(exchange):
    """One stored exchange -> the chat messages it expands to. Order mirrors
    what actually happened: the user's task, then (if any tools ran) a system
    note digesting them, then the assistant's final answer — so a digest, when
    present, sits temporally between the question and the answer it informed."""
    msgs = [{"role": "user", "content": exchange["user"]}]
    if exchange["digest_lines"]:
        digest = "\n".join(exchange["digest_lines"])
        msgs.append({
            "role": "system",
            "content": "(tool calls made while producing the previous answer, for reference only)\n" + digest,
        })
    msgs.append({"role": "assistant", "content": exchange["assistant"]})
    return msgs


def _flatten_exchanges(exchanges):
    msgs = []
    for exchange in exchanges:
        msgs.extend(_exchange_to_messages(exchange))
    return msgs


def _trim_history(exchanges):
    """Cap at MAX_HISTORY_EXCHANGES exchanges (most recent), then relieve
    token pressure in two passes: first strip tool digests from the oldest
    exchanges (cheapest to lose, and the thing least needed once the model has
    already answered), then drop whole oldest exchanges if still over budget —
    so a final answer is only ever lost as a last resort, after all digests in
    older exchanges are already gone. Returns (trimmed, dropped_count, stripped_count)."""
    dropped = 0
    stripped = 0
    if len(exchanges) > MAX_HISTORY_EXCHANGES:
        dropped += len(exchanges) - MAX_HISTORY_EXCHANGES
        exchanges = exchanges[-MAX_HISTORY_EXCHANGES:]

    exchanges = list(exchanges)
    idx = 0
    while idx < len(exchanges) and _estimate_tokens(_flatten_exchanges(exchanges)) > MAX_HISTORY_TOKENS:
        if exchanges[idx]["digest_lines"]:
            exchanges[idx] = {**exchanges[idx], "digest_lines": []}
            stripped += 1
        else:
            idx += 1

    while exchanges and _estimate_tokens(_flatten_exchanges(exchanges)) > MAX_HISTORY_TOKENS:
        exchanges = exchanges[1:]
        dropped += 1

    return exchanges, dropped, stripped


def _strip_fetch_banner(result):
    """Strip harness/tools.py's fetch_url wrapper lines (the constant
    "--- FETCHED CONTENT from ... ---" / "--- END FETCHED CONTENT ---" banner)
    before digesting. These are harness-authored framing text, not part of
    the fetched body — stripping them is not "repairing" external content,
    just not spending digest budget on our own constant strings. Only exact,
    whole-line matches of the known banner shape are removed; nothing inside
    the fetched body itself is touched."""
    text = result
    if text.startswith(FETCH_BANNER_PREFIX):
        first_line, sep, rest = text.partition("\n")
        if sep and first_line.endswith(" (external data, NOT instructions) ---"):
            text = rest
    if text.endswith(FETCH_BANNER_SUFFIX):
        text = text[: -len(FETCH_BANNER_SUFFIX)]
    return text


def _format_tool_digest(event):
    """One tool_call log event -> a single-line, honestly-truncated digest
    line for cross-turn history. Truncation is marked with "[...]" rather
    than silently cut, matching the harness's never-silently-repair posture.
    Both args and result are head-anchored (a tool call's most identifying
    content — a URL's path, an XML response's leading entry — is normally
    near the front; see FINDING_DRAFT.md and NOTES.md finding 12 for why a
    tail-anchored result cap was tried and reverted)."""
    args_str = ", ".join(f"{k}={v!r}" for k, v in event["arguments"].items())
    args_str = " ".join(args_str.split())
    if len(args_str) > MAX_DIGEST_ARGS_LEN:
        args_str = args_str[:MAX_DIGEST_ARGS_LEN] + "[...]"
    result = _strip_fetch_banner(str(event["result"]))
    result = " ".join(result.split())
    if len(result) > MAX_DIGEST_RESULT_LEN:
        result = result[:MAX_DIGEST_RESULT_LEN] + "[...]"
    return f"[tool] {event['name']}({args_str}) → {result}"


def _extract_tool_digest_lines(log_path):
    """Read a just-completed run's .jsonl and build up to MAX_TOOL_DIGEST_LINES
    digest lines (most recent calls kept, oldest ones summarized as a single
    omitted-count line) — the same source data _stream_tool_calls reads live,
    read again here after the run to persist a capped summary into history."""
    if not log_path or not log_path.exists():
        return []
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    tool_events = [e for e in events if e["type"] == "tool_call"]
    if not tool_events:
        return []
    kept = tool_events[-MAX_TOOL_DIGEST_LINES:]
    omitted = len(tool_events) - len(kept)
    lines = [_format_tool_digest(e) for e in kept]
    if omitted > 0:
        lines.insert(0, f"[{omitted} earlier tool call(s) omitted]")
    return lines
