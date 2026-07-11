"""Content-dependence ablation for the empty-response flake (NOTES.md finding 6).

Same request construction as scripts/probe_flake.py: live system prompt built
the same way harness/loop.py's run_task() builds it (not a stale snapshot),
the real REGISTRY-derived tools list, think=false, temperature 0.1, num_ctx 16384.
Direct httpx calls to /api/chat, bypassing the harness entirely. Scratch/diagnostic
only — no tracked files touched.
"""
import httpx, json, yaml
from pathlib import Path

from harness.tools import REGISTRY, list_skills
from harness.loop import PROMPT_PATH

CONFIG = yaml.safe_load(Path("config.yaml").read_text())
BASE = CONFIG["base_url"]
MODEL = CONFIG["model"]

# Exactly harness/loop.py's TOOL_SCHEMAS construction.
TOOLS = [schema for schema, _ in REGISTRY.values()]


def build_system_prompt():
    """Exactly harness/loop.py's run_task() system-prompt construction, built
    live against the current skills/ and prompt.md — not a cached copy."""
    system_prompt = PROMPT_PATH.read_text()
    skills = list_skills()
    if skills:
        skill_lines = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
        system_prompt += (
            "\n\nAvailable skills — these are NOT tools. To use one, call the load_skill "
            "tool with the skill's name as the \"name\" argument, e.g. "
            'load_skill(name="write-and-verify-code"):\n'
            f"{skill_lines}"
        )
    return system_prompt


SYSTEM = build_system_prompt()

TASKS = {
    "T1": "Write a file named hello.txt containing the single word hello.",
    "T2": 'Write a file named greeting.txt containing: She said "hi" to me.',
    "T3": ("Write a Python file containing tricky quotes: nested double and "
           "single quotes, an apostrophe in prose, and a triple-quoted string."),
}


def run_once(label, task, timeout=600):
    # Same payload shape as scripts/probe_flake.py's probe(): "tools" only
    # added when not None, mirroring harness/ollama_client.py's chat().
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": task}],
        "stream": False,
        "think": False,
        "options": {"temperature": CONFIG["temperature"], "num_ctx": CONFIG["num_ctx"]},
        "tools": TOOLS,
    }
    r = httpx.post(f"{BASE}/api/chat", json=payload, timeout=timeout).json()
    msg = r.get("message", {})
    tool_calls = msg.get("tool_calls") or []

    tool_name = None
    args_valid = None
    if tool_calls:
        call = tool_calls[0]["function"]
        tool_name = call.get("name")
        args = call.get("arguments")
        if isinstance(args, dict):
            args_valid = True
        elif isinstance(args, str):
            try:
                json.loads(args)
                args_valid = True
            except json.JSONDecodeError:
                args_valid = False
        else:
            args_valid = False

    return {
        "label": label,
        "done_reason": r.get("done_reason"),
        "eval_count": r.get("eval_count"),
        "content_len": len(msg.get("content") or ""),
        "tool_calls": len(tool_calls),
        "tool_name": tool_name,
        "args_valid": args_valid,
    }


if __name__ == "__main__":
    rows = []
    for task_label, task_text in TASKS.items():
        for i in range(1, 4):
            row = run_once(f"{task_label}-{i}", task_text)
            rows.append(row)
            print(row)

    print(f"\n{'run':<7} {'done_reason':<12} {'eval_count':<11} {'content_len':<12} "
          f"{'tool_calls':<11} {'tool_name':<16} {'args_valid':<10}")
    for row in rows:
        print(f"{row['label']:<7} {str(row['done_reason']):<12} {str(row['eval_count']):<11} "
              f"{str(row['content_len']):<12} {str(row['tool_calls']):<11} "
              f"{str(row['tool_name']):<16} {str(row['args_valid']):<10}")
