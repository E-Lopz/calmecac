"""Capture the raw, unparsed A' generation (NOTES.md finding 6, Task 2).

Renders the ChatML template by hand and hits /api/generate with raw:true,
bypassing Ollama's chat-template rendering and tool_call/content parsing —
so we can see the verbatim text the swallow has been eating. Only the
empty-think-seeded variant (forces the model past the <think> block directly
into generation) — the earlier "plain" variant just showed the model wants to
think at length by default and never got there within num_predict.

System prompt is built live from harness/loop.py's construction (not a cached
snapshot). Scratch/diagnostic only — no tracked files touched.
"""
import json, httpx

from harness.tools import REGISTRY, list_skills
from harness.loop import PROMPT_PATH

MODEL = "qwen3:14b"

TOOLS = [schema for schema, _ in REGISTRY.values()]


def build_system_prompt():
    """Exactly harness/loop.py's run_task() system-prompt construction, live."""
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
USER = ("Write a Python file containing tricky quotes: nested double and "
        "single quotes, an apostrophe in prose, and a triple-quoted string.")

TOOLS_BLOCK = "\n".join(json.dumps(t) for t in TOOLS)

TEMPLATE = """<|im_start|>system
{system}

# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call><|im_end|>
<|im_start|>user
{user}<|im_end|>
<|im_start|>assistant
"""

PROMPT = TEMPLATE.format(system=SYSTEM, tools=TOOLS_BLOCK, user=USER) + "<think>\n\n</think>\n\n"


def analyze(text):
    print("--- analysis ---")
    start = text.find("<tool_call>")
    if start == -1:
        print("no <tool_call> block found in raw output")
        return
    body_start = start + len("<tool_call>")
    end = text.find("</tool_call>", body_start)
    if end == -1:
        print("<tool_call> opened but no closing </tool_call> — output truncated before close")
        inner = text[body_start:]
    else:
        inner = text[body_start:end]
        print("closing </tool_call> present")
    print("tool_call body:", repr(inner))
    print("mentions write_file:", "write_file" in inner)

    try:
        json.loads(inner)
        print("json.loads: OK — arguments parse as valid JSON")
    except json.JSONDecodeError as e:
        char_at_error = inner[e.pos] if e.pos < len(inner) else "(end of string)"
        print(f"json.loads FAILED: {e.msg}")
        print(f"  position: char {e.pos}, line {e.lineno}, col {e.colno}")
        print(f"  character at error: {char_at_error!r}")
        lo = max(0, e.pos - 20)
        hi = min(len(inner), e.pos + 20)
        print(f"  context (~40 chars around error): {inner[lo:hi]!r}")


if __name__ == "__main__":
    for i in range(1, 4):
        r = httpx.post("http://localhost:11434/api/generate",
                       json={"model": MODEL, "prompt": PROMPT, "raw": True,
                             "stream": False,
                             "options": {"temperature": 0.1, "num_ctx": 16384,
                                         "num_predict": 512}},
                       timeout=600).json()
        print(f"\n=== run {i} ===")
        print("prompt_eval_count:", r.get("prompt_eval_count"))
        print("eval_count:", r.get("eval_count"))
        print("done_reason:", r.get("done_reason"))
        response_text = r.get("response") or ""
        print("RAW OUTPUT:")
        print(repr(response_text))
        analyze(response_text)
