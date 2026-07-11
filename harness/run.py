"""CLI entry point for the agent loop.

Run with: python -m harness.run "task here"
"""

import sys
from pathlib import Path

import yaml

from harness.loop import run_task

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def main():
    if len(sys.argv) != 2:
        print('usage: python -m harness.run "task here"')
        sys.exit(1)

    task = sys.argv[1]
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    answer = run_task(task, config)
    print(f"\nfinal answer: {answer}")


if __name__ == "__main__":
    main()
