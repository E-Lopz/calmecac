"""Experiment runner: measure how often the model calls load_skill on a fixed task.

Run with: python -m scripts.trigger_test "task text" [--n 5]
"""

import argparse
import json
import shutil
import sys

import yaml

from harness.loop import LOG_DIR, run_task
from harness.run import CONFIG_PATH
from harness.tools import WORKSPACE


def _clear_workspace():
    for entry in WORKSPACE.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _analyze_log(log_path):
    """Read one run's .jsonl log and classify load_skill usage and outcome."""
    events = [json.loads(line) for line in log_path.read_text().splitlines()]

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    model_calls = [e for e in events if e["type"] == "model_call"]
    aborted = any(e["type"] == "abort" for e in events)

    skill_loaded = None
    load_skill_idx = write_file_idx = None
    for i, call in enumerate(tool_calls):
        if call["name"] == "load_skill" and load_skill_idx is None:
            load_skill_idx = i
            skill_loaded = call.get("skill_loaded")
        if call["name"] == "write_file" and write_file_idx is None:
            write_file_idx = i

    if load_skill_idx is None:
        order = "not-loaded"
    elif write_file_idx is None or load_skill_idx < write_file_idx:
        order = "before"
    else:
        order = "after"

    final_content = model_calls[-1]["response"].get("content", "") if model_calls else ""
    is_flake = not tool_calls and not aborted and final_content == ""
    outcome = "flake" if is_flake else ("aborted" if aborted else "completed")

    return skill_loaded, order, outcome


def main():
    parser = argparse.ArgumentParser(description="Measure load_skill trigger rate across repeated runs.")
    parser.add_argument("task", help="Fixed task string, run identically N times.")
    parser.add_argument("--n", type=int, default=5, help="Number of runs (default 5).")
    args = parser.parse_args()

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    WORKSPACE.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    rows = []
    for run_num in range(1, args.n + 1):
        _clear_workspace()
        before = set(LOG_DIR.glob("*.jsonl"))
        run_task(args.task, config)
        after = set(LOG_DIR.glob("*.jsonl"))
        new_logs = sorted(after - before)

        if not new_logs:
            print(f"run {run_num}: no new log file found (timestamp collision?), skipping", file=sys.stderr)
            continue

        skill_loaded, order, outcome = _analyze_log(new_logs[-1])
        rows.append((run_num, skill_loaded or "-", order, outcome))

    _clear_workspace()

    print(f"\n{'run':<4} {'skill_loaded':<24} {'order':<12} {'outcome':<10}")
    for run_num, skill_loaded, order, outcome in rows:
        print(f"{run_num:<4} {skill_loaded:<24} {order:<12} {outcome:<10}")

    flakes = sum(1 for *_, outcome in rows if outcome == "flake")
    non_flake = len(rows) - flakes
    triggered = sum(1 for _, skill_loaded, _, outcome in rows if outcome != "flake" and skill_loaded != "-")
    print(f"\nload_skill triggered {triggered}/{non_flake} runs ({flakes} flake(s) excluded)")


if __name__ == "__main__":
    main()
