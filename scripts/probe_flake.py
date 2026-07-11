"""Diagnose the empty-content flake. Direct httpx calls, bypassing the harness."""
import httpx, json, yaml, sys
from pathlib import Path

from harness.tools import REGISTRY

CONFIG = yaml.safe_load(Path("config.yaml").read_text())
BASE = CONFIG["base_url"]
MODEL = CONFIG["model"]

# Exactly harness/loop.py's TOOL_SCHEMAS construction — each REGISTRY value's
# schema is already the full {"type": "function", "function": {...}} dict.
TOOLS = [schema for schema, _ in REGISTRY.values()]

# Reconstruct the EXACT system prompt the harness used, index included.
# Easiest reliable way: copy it out of one of tonight's flake .jsonl logs —
# the first log line should contain the messages sent. Paste it here:
SYSTEM_WITH_INDEX = Path("scripts/flake_system_prompt.txt").read_text()

USER = ("Write a Python file containing tricky quotes: nested double and "
        "single quotes, an apostrophe in prose, and a triple-quoted string.")

# Plain kukulkan prompt, no skills index — "no skills involved" for the clincher probe.
SYSTEM_PLAIN = Path("agents/kukulkan/prompt.md").read_text()

CLINCHER_USER = ('Call the tool named `frobnicate_file` with arguments {"path": "x.txt"}. '
                  'Do not use any other tool. Do not explain.')

# Narrower version: the collision name is a REAL skill name (present in the
# index, three lines above the tool list) rather than a wholly fictitious one.
SKILL_NAME_USER = ('Call the tool named `write-and-verify-code` with arguments {"path": "x.txt"}. '
                    'Do not use any other tool. Do not explain.')

def probe(label, system, think, tools=None, timeout=600, user=None):
    # Same payload construction as harness/ollama_client.py's chat(): "tools"
    # is only added to the payload when not None, same conditional.
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user if user is not None else USER}],
        "stream": False,
        "think": think,
        "options": {"temperature": CONFIG["temperature"],
            "num_ctx": CONFIG["num_ctx"]},
    }
    if tools is not None:
        payload["tools"] = tools

    r = httpx.post(f"{BASE}/api/chat", json=payload, timeout=timeout).json()
    msg = r.get("message", {})
    print(f"\n=== {label} ===")
    print(f"  done_reason:  {r.get('done_reason')}")
    print(f"  eval_count:   {r.get('eval_count')}")
    print(f"  content len:  {len(msg.get('content') or '')}")
    print(f"  thinking len: {len(msg.get('thinking') or '')}")
    print(f"  tool_calls:   {len(msg.get('tool_calls') or [])}")
    print(f"  content head: {repr((msg.get('content') or '')[:120])}")

if __name__ == "__main__":
    # A', rerun with the revised index wording (explicit "NOT tools" + a
    # worked load_skill(name=...) example) — flake_system_prompt.txt was
    # regenerated to match harness/loop.py's new index text.
    for i in range(5):
        probe(f"A'-{i+1} (revised index): index + tools + think=false", SYSTEM_WITH_INDEX,
              think=False, tools=TOOLS)