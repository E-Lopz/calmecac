# Evals

Agentic eval runner for this repo's agent — harness + prompt + skills + config
as a unit, exercised through the real ReAct loop (`harness/loop.run_task`)
against a live Ollama instance. This is distinct from the separate `llm-eval`
project, which benchmarks *models* in isolation; this benchmarks *the agent*.

- `cases/<name>.yaml` — one file per pinned case: `name`, `task`, optional
  `max_steps` override, `checks` (typed assertions graded against the run's
  `.jsonl` log and post-run workspace state — see `checks.py` for the list),
  optional `expected_flaky: true` for cases that measure a known flake rate
  rather than gate the suite.
- `checks.py` — the check implementations, keyed by `type` in `CHECKS`.
- `run.py` — the runner: `python -m evals.run [--case NAME] [--n 5]`. Runs
  each case N times (default 3), wiping `workspace/` between runs, and
  reports pass rates, mean steps, mean token count, and swallow count to
  stdout plus a timestamped JSON file in `results/`.
- `results/` — committed (not gitignored): this is the project's measurement
  history, so results are tracked over time rather than discarded.
