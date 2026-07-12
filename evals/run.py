"""Agentic eval runner: replays pinned cases (evals/cases/*.yaml) against the
live harness N times each and grades the resulting log/workspace against each
case's typed checks. This evaluates the agent as a whole — harness + prompt +
skills + config — not the underlying model in isolation.

Run with: python -m evals.run [--case NAME] [--n 5]
"""

import argparse
import json
import shutil
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from evals.checks import CHECKS, label
from harness.loop import LOG_DIR, run_task
from harness.run import CONFIG_PATH
from harness.tools import WORKSPACE

CASES_DIR = Path(__file__).resolve().parent / "cases"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _clear_workspace():
    for entry in WORKSPACE.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _load_cases(case_name=None):
    paths = sorted(CASES_DIR.glob("*.yaml"))
    if case_name:
        paths = [p for p in paths if p.stem == case_name]
        if not paths:
            sys.exit(f"no case named '{case_name}' in {CASES_DIR}")
    cases = []
    for p in paths:
        with open(p) as f:
            cases.append(yaml.safe_load(f))
    return cases


def _run_once(case, config):
    if case.get("max_steps") is not None:
        config = dict(config, max_steps=case["max_steps"])

    _clear_workspace()
    before = set(LOG_DIR.glob("*.jsonl"))
    final_answer = run_task(case["task"], config)
    after = set(LOG_DIR.glob("*.jsonl"))
    new_logs = sorted(after - before)
    if not new_logs:
        sys.exit(f"case '{case['name']}': no new log file found (timestamp collision?)")
    log_path = new_logs[-1]
    events = [json.loads(line) for line in log_path.read_text().splitlines()]

    ctx = {"events": events, "final_answer": final_answer, "workspace": WORKSPACE}
    check_results = []
    for check in case["checks"]:
        passed, detail = CHECKS[check["type"]](check, ctx)
        check_results.append({
            "label": label(check),
            "type": check["type"],
            "passed": passed,
            "detail": detail,
        })

    model_calls = [e for e in events if e["type"] == "model_call"]
    return {
        "log_file": log_path.name,
        "steps": len(model_calls),
        "eval_count_total": sum(e.get("eval_count") or 0 for e in model_calls),
        "swallowed": sum(1 for e in model_calls if e.get("generation_swallowed")),
        "final_answer": final_answer,
        "checks": check_results,
        "all_passed": all(c["passed"] for c in check_results),
    }


def _run_case(case, config, n):
    runs = [_run_once(case, config) for _ in range(n)]
    _clear_workspace()

    labels = [c["label"] for c in runs[0]["checks"]] if runs else []
    check_pass_rates = {
        lbl: sum(1 for r in runs for c in r["checks"] if c["label"] == lbl and c["passed"]) / n
        for lbl in labels
    }

    return {
        "name": case["name"],
        "expected_flaky": case.get("expected_flaky", False),
        "n": n,
        "runs": runs,
        "check_pass_rates": check_pass_rates,
        "overall_pass_rate": sum(r["all_passed"] for r in runs) / n,
        "mean_steps": statistics.mean(r["steps"] for r in runs),
        "mean_eval_count_total": statistics.mean(r["eval_count_total"] for r in runs),
        "swallow_count": sum(r["swallowed"] for r in runs),
    }


def _print_report(results):
    print(f"\n{'case':<20} {'n':<4} {'pass':<7} {'steps':<7} {'eval_cnt':<10} {'swallow':<8} {'flaky':<6}")
    for c in results:
        print(
            f"{c['name']:<20} {c['n']:<4} {c['overall_pass_rate'] * 100:>5.0f}%  "
            f"{c['mean_steps']:<7.1f} {c['mean_eval_count_total']:<10.1f} "
            f"{c['swallow_count']:<8} {'yes' if c['expected_flaky'] else 'no':<6}"
        )

    print("\nper-check pass rates:")
    for c in results:
        flaky_note = " (expected flaky — measures flake rate, not a suite failure)" if c["expected_flaky"] else ""
        print(f"  {c['name']}{flaky_note}:")
        for lbl, rate in c["check_pass_rates"].items():
            print(f"    {lbl:<50} {rate * 100:>5.0f}%")


def main():
    parser = argparse.ArgumentParser(description="Run pinned eval cases against the live harness.")
    parser.add_argument("--case", help="Run only this case (by file stem).")
    parser.add_argument("--n", type=int, default=3, help="Runs per case (default 3).")
    args = parser.parse_args()

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    WORKSPACE.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    cases = _load_cases(args.case)
    results = [_run_case(case, config, args.n) for case in cases]

    _print_report(results)

    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"results_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump({"timestamp": timestamp, "n": args.n, "cases": results}, f, indent=2)
    print(f"\nresults written to {out_path}")


if __name__ == "__main__":
    main()
