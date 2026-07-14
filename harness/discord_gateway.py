"""Discord gateway: a thin caller of harness.loop.run_task, exactly like
harness/run.py, but driven by Discord messages instead of argv.

Run with: python -m harness.discord_gateway
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import discord
import yaml
from dotenv import load_dotenv

from harness.loop import LOG_DIR, run_task

load_dotenv()

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
MAX_MESSAGE_LEN = 2000

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("DISCORD_BOT_TOKEN is not set (check your .env file). Aborting startup.")

ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USER_ID")
if not ALLOWED_USER_ID:
    sys.exit("DISCORD_ALLOWED_USER_ID is not set (check your .env file). Aborting startup.")
try:
    ALLOWED_USER_ID = int(ALLOWED_USER_ID)
except ValueError:
    sys.exit(
        f"DISCORD_ALLOWED_USER_ID must be a numeric Discord user id, got {ALLOWED_USER_ID!r}. "
        "Enable Developer Mode in Discord (Settings > Advanced), then right-click your profile "
        "and 'Copy User ID'."
    )

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

_discord_config = CONFIG.get("discord", {})
if not _discord_config.get("enabled", False):
    sys.exit("config.yaml: discord.enabled is false. Set it to true to run the gateway.")
CHANNEL_ID = _discord_config.get("channel_id")
if not CHANNEL_ID:
    sys.exit("config.yaml: discord.channel_id is not set.")

DEFAULT_VERBOSITY = _discord_config.get("verbosity", "quiet")
if DEFAULT_VERBOSITY not in ("quiet", "steps"):
    sys.exit(f"config.yaml: discord.verbosity must be 'quiet' or 'steps', got {DEFAULT_VERBOSITY!r}.")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

busy = False  # single-task-at-a-time gate; no queue in this phase

# Phase 6a: short-term, in-process conversation memory, keyed by channel id.
# Phase 6b adds a capped tool-call digest per exchange (see FINDING_DRAFT.md:
# without it, a follow-up like "get me the link" has no record of what the
# previous turn actually fetched, and the model re-derives — sometimes
# incorrectly — a fresh query). Each exchange is a dict:
#   {"user": task_text, "digest_lines": [...], "assistant": answer}
# digest_lines is a list of pre-formatted "[tool] name(args) -> result" strings,
# already capped to MAX_TOOL_DIGEST_LINES with an omitted-count marker — never
# raw tool-call steps beyond that cap. Dies with the gateway; restarting it is
# how you wipe memory.
HISTORY = {}
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

# Per-channel runtime verbosity override (!verbose). Not persisted — falls
# back to DEFAULT_VERBOSITY (config.yaml) on gateway restart.
CHANNEL_VERBOSITY = {}


def _verbosity_for(channel_id):
    return CHANNEL_VERBOSITY.get(channel_id, DEFAULT_VERBOSITY)


def _truncate(text, log_name):
    if len(text) <= MAX_MESSAGE_LEN:
        return text
    suffix = f"\n...(truncated, full output in logs/{log_name})"
    return text[: MAX_MESSAGE_LEN - len(suffix)] + suffix


def _strip_mention(content):
    return re.sub(rf"<@!?{client.user.id}>", "", content).strip()


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


def _format_tool_call(event):
    args = ", ".join(f"{k}={v!r}" for k, v in event["arguments"].items())
    return f"step {event['step']}: {event['name']}({args}) -> {event['result_head']}"


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


async def _stream_tool_calls(channel, log_path, stop_event):
    """Poll log_path for new tool_call lines and post each as a short message,
    until stop_event is set (the run finished). Nothing in loop.py changes for
    this — it's a plain reader of the same .jsonl every CLI run already writes."""
    pos = 0
    while True:
        if log_path.exists():
            with open(log_path) as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    pos = f.tell()
                    event = json.loads(line)
                    if event["type"] == "tool_call":
                        await channel.send(_truncate(_format_tool_call(event), log_path.name))
        if stop_event.is_set():
            break
        await asyncio.sleep(1.0)


async def _stream_progress_quiet(channel, log_path, stop_event):
    """Quiet-mode counterpart to _stream_tool_calls: posts nothing per step.
    Only once a run passes 4 steps does it post a single status message, then
    edit that same message every ~5 steps with a one-line progress note —
    Discord bots can't edit another user's message, so this status message is
    a separate, bot-owned post rather than the hourglass reaction itself."""
    pos = 0
    step = 0
    tool_call_count = 0
    status_message = None
    last_noted_step = 0
    while True:
        if log_path.exists():
            with open(log_path) as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    pos = f.tell()
                    event = json.loads(line)
                    step = event.get("step", step)
                    if event["type"] == "tool_call":
                        tool_call_count += 1
            if step > 4 and step - last_noted_step >= 5:
                note = f"working — step {step}, {tool_call_count} tool call(s) made"
                if status_message is None:
                    status_message = await channel.send(note)
                else:
                    await status_message.edit(content=note)
                last_noted_step = step
        if stop_event.is_set():
            break
        await asyncio.sleep(1.0)


async def _run_and_report(message):
    channel = message.channel
    task_text = _strip_mention(message.content)
    verbosity = _verbosity_for(channel.id)
    await message.add_reaction("⏳")  # hourglass

    exchanges = HISTORY.get(channel.id, [])
    prior_messages = _flatten_exchanges(exchanges)
    digest_len = sum(
        len(m["content"]) for m in prior_messages if m["role"] == "system"
    )
    print(f"[gateway] channel {channel.id}: history_len={len(prior_messages)} digest_len={digest_len}")

    existing_logs = set(LOG_DIR.glob("*.jsonl"))
    loop = asyncio.get_running_loop()
    run_future = loop.run_in_executor(None, run_task, task_text, CONFIG, "discord", prior_messages)

    log_path = None
    for _ in range(40):  # up to ~10s for the log file to appear
        new_logs = set(LOG_DIR.glob("*.jsonl")) - existing_logs
        if new_logs:
            log_path = sorted(new_logs)[0]
            break
        await asyncio.sleep(0.25)

    stop_event = asyncio.Event()
    stream_task = None
    if log_path:
        streamer = _stream_tool_calls if verbosity == "steps" else _stream_progress_quiet
        stream_task = asyncio.create_task(streamer(channel, log_path, stop_event))

    try:
        answer = await run_future
    finally:
        stop_event.set()
        if stream_task:
            await stream_task

    log_name = log_path.name if log_path else "(unknown)"
    await channel.send(_truncate(answer, log_name))
    await message.remove_reaction("⏳", client.user)
    await message.add_reaction("❌" if answer.startswith("aborted:") else "✅")

    digest_lines = _extract_tool_digest_lines(log_path)
    exchanges = HISTORY.get(channel.id, [])
    exchanges.append({"user": task_text, "digest_lines": digest_lines, "assistant": answer})
    exchanges, dropped, stripped = _trim_history(exchanges)
    HISTORY[channel.id] = exchanges
    if dropped:
        print(f"[gateway] dropped {dropped} oldest exchange(s) from channel {channel.id} history (budget)")
    if stripped:
        print(f"[gateway] stripped tool digest(s) from {stripped} exchange(s) in channel {channel.id} history (budget)")


@client.event
async def on_ready():
    print(f"[gateway] logged in as {client.user}, channel={CHANNEL_ID}, allowed_user={ALLOWED_USER_ID}")


@client.event
async def on_message(message):
    global busy

    if message.author.id == client.user.id:
        return

    if message.channel.id != CHANNEL_ID or message.author.id != ALLOWED_USER_ID:
        print(f"[gateway debug] ignored message author={message.author.id} "
              f"channel={message.channel.id} content={message.content[:50]!r}")
        return

    if message.content.strip() == "!reset":
        HISTORY.pop(message.channel.id, None)
        await message.reply("context cleared.")
        return

    if message.content.strip() == "!verbose":
        new = "steps" if _verbosity_for(message.channel.id) == "quiet" else "quiet"
        CHANNEL_VERBOSITY[message.channel.id] = new
        await message.reply(f"verbosity: {new}")
        return

    if not client.user.mentioned_in(message):
        print(f"[gateway debug] ignored message (no mention) author={message.author.id} "
              f"channel={message.channel.id} content={message.content[:50]!r}")
        return

    if busy:
        await message.reply("busy with the current task — send again when I finish")
        return

    busy = True
    try:
        await _run_and_report(message)
    finally:
        busy = False


def main():
    client.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
