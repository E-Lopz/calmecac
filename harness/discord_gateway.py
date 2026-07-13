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

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

busy = False  # single-task-at-a-time gate; no queue in this phase

# Phase 6a: short-term, in-process conversation memory, keyed by channel id.
# Final exchanges only (user task text + assistant final/abort answer) — no
# tool-call steps. Dies with the gateway; restarting it is how you wipe memory.
HISTORY = {}
MAX_HISTORY_EXCHANGES = 6
MAX_HISTORY_TOKENS = 2000


def _truncate(text, log_name):
    if len(text) <= MAX_MESSAGE_LEN:
        return text
    suffix = f"\n...(truncated, full output in logs/{log_name})"
    return text[: MAX_MESSAGE_LEN - len(suffix)] + suffix


def _strip_mention(content):
    return re.sub(rf"<@!?{client.user.id}>", "", content).strip()


def _estimate_tokens(messages):
    return sum(len(m["content"]) for m in messages) // 4


def _trim_history(history):
    """Cap at MAX_HISTORY_EXCHANGES exchanges, then drop oldest exchanges until
    under MAX_HISTORY_TOKENS estimated tokens. Returns (trimmed, dropped_count)."""
    dropped = 0
    max_messages = MAX_HISTORY_EXCHANGES * 2
    if len(history) > max_messages:
        dropped += (len(history) - max_messages) // 2
        history = history[-max_messages:]
    while history and _estimate_tokens(history) > MAX_HISTORY_TOKENS:
        history = history[2:]
        dropped += 1
    return history, dropped


def _format_tool_call(event):
    args = ", ".join(f"{k}={v!r}" for k, v in event["arguments"].items())
    return f"step {event['step']}: {event['name']}({args}) -> {event['result_head']}"


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


async def _run_and_report(message):
    channel = message.channel
    task_text = _strip_mention(message.content)
    await message.add_reaction("⏳")  # hourglass

    prior_messages = list(HISTORY.get(channel.id, []))

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
    stream_task = asyncio.create_task(_stream_tool_calls(channel, log_path, stop_event)) if log_path else None

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

    history = HISTORY.get(channel.id, [])
    history.append({"role": "user", "content": task_text})
    history.append({"role": "assistant", "content": answer})
    history, dropped = _trim_history(history)
    HISTORY[channel.id] = history
    if dropped:
        print(f"[gateway] dropped {dropped} oldest exchange(s) from channel {channel.id} history (budget)")


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
