# Multi-turn eval runner: blocking finding + proposed syntax

Status: **stopped before implementation**, per this session's own instruction
("if too entangled to import cleanly, STOP and report — do not copy-paste").
No changes made to `evals/run.py`, `evals/checks.py`, `harness/discord_gateway.py`,
`loop.py`, or `tools.py`. This is a report for review, not a diff.

## 1. The blocking finding: `harness/discord_gateway.py` cannot be imported cleanly

Requirement 2 of this task was to reuse the gateway's history functions
(`_exchange_to_messages`, `_flatten_exchanges`, `_trim_history`,
`_extract_tool_digest_lines`, `_format_tool_digest`, `_strip_fetch_banner`,
and the `MAX_*`/`FETCH_BANNER_*` constants) via import, not copy-paste.

That import is not possible today. `harness/discord_gateway.py` runs
unconditional validation **at module level**, before any function is
reachable:

```python
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not BOT_TOKEN:
    sys.exit("DISCORD_BOT_TOKEN is not set (check your .env file). Aborting startup.")
...
ALLOWED_USER_ID = os.environ.get("DISCORD_ALLOWED_USER_ID")
if not ALLOWED_USER_ID:
    sys.exit(...)
...
_discord_config = CONFIG.get("discord", {})
if not _discord_config.get("enabled", False):
    sys.exit("config.yaml: discord.enabled is false. Set it to true to run the gateway.")
CHANNEL_ID = _discord_config.get("channel_id")
if not CHANNEL_ID:
    sys.exit(...)
...
client = discord.Client(intents=intents)
```

Verified directly: from a working directory where `load_dotenv()` can't find
this project's `.env` (any CI box, any dev machine that hasn't set up Discord,
or simply `discord.enabled: false` in `config.yaml`), `import
harness.discord_gateway` hard-crashes:

```
$ cd /tmp && python3 -c "import sys; sys.path.insert(0, '<repo>'); import harness.discord_gateway"
DISCORD_BOT_TOKEN is not set (check your .env file). Aborting startup.
exit code: 1
```

Even in the one case where it *doesn't* crash (running from this repo's root,
with the real `.env` present and `discord.enabled: true`), the import still
instantiates a live `discord.Client()` and requires a real `channel_id` — the
eval runner would be silently coupled to whether someone's Discord bot happens
to be configured on the machine running `python -m evals.run`, for a feature
(history/digest construction) that has nothing to do with Discord at all.

This is exactly the entanglement this task's own escape hatch was written
for. I did not copy-paste the six functions into `evals/run.py` — a divergent
copy would silently test a different mechanism than what production Discord
runs actually execute, defeating the point of "same code path" testing.

### Proposed refactor (not performed — needs your sign-off, since it touches `discord_gateway.py`)

Extract the seven Discord-agnostic pieces — they're already cleanly
segregated in the file (all defined before any `discord`-typed object is
touched) — into a new module, e.g. `harness/history.py`:

- Constants: `MAX_HISTORY_EXCHANGES`, `MAX_HISTORY_TOKENS`,
  `MAX_TOOL_DIGEST_LINES`, `MAX_DIGEST_ARGS_LEN`, `MAX_DIGEST_RESULT_LEN`,
  `FETCH_BANNER_PREFIX`, `FETCH_BANNER_SUFFIX`
- Functions: `_estimate_tokens`, `_exchange_to_messages`,
  `_flatten_exchanges`, `_trim_history`, `_strip_fetch_banner`,
  `_format_tool_digest`, `_extract_tool_digest_lines`

None of these touch `discord`, `client`, `channel`, or any async code — they
operate only on plain dicts, strings, and `Path`s. `harness/discord_gateway.py`
would then `from harness.history import (...)` instead of defining them
locally — same logic, same behavior, zero functional change, verifiable with
a diff. `evals/run.py` would import from `harness/history.py` too, so both
callers run the literal same code, which is the actual goal of requirement 2.

This is a one-file, mechanical extraction — low risk — but it does modify
`harness/discord_gateway.py`, which was named as off-limits for this session,
so I'm reporting rather than doing it. Say the word and I'll make the cut in
a follow-up turn.

## 2. Proposed `turns:` schema (evals/cases/*.yaml)

Single-turn cases are unaffected — `turns:` absent means the existing
`task:`/`checks:`/`max_steps:` top-level fields, byte-identical code path.

```yaml
name: arxiv-link-followup-title-url-match
max_steps: 10              # still case-level, applies to every turn (no per-turn override — not needed by either proposed case)
turns:
  - task: "can you extract the first paper title in arxiv of attention mechanisms"
    # checks: optional per turn; omitted here since nothing needs pinning on turn 1 itself
  - task: "can you get me the link"
    checks:
      - type: completed
      - type: final_answer_contains_prior_capture   # see §3
        from_turn: 1
        pattern: "<title>([^<]+)</title>"
      - type: final_answer_contains_prior_capture
        from_turn: 1
        pattern: "arxiv\\.org/abs/([\\w.]+)"
```

`_load_cases` needs no change beyond passing the raw dict through — the
branch on `"turns" in case` happens in `_run_case`/`_run_once`. Sketch:

```python
def _run_case(case, config, n):
    if "turns" in case:
        runs = [_run_multi_turn_once(case, config) for _ in range(n)]
    else:
        runs = [_run_once(case, config) for _ in range(n)]
    ...
```

`_run_multi_turn_once` drives each turn through `run_task(..., prior_messages=...)`
exactly like `discord_gateway._run_and_report` does per Discord message: build
`prior_messages` from the accumulated exchanges via `_flatten_exchanges`, run,
extract digest lines from that turn's own new log via
`_extract_tool_digest_lines`, append the exchange, `_trim_history`. Per-turn
`ctx` for checks gains a `"turns"` key (list of every turn's `{events,
final_answer}` so far, 0-indexed) so a check can reach back to an earlier
turn — see §3.

## 3. Proposed dynamic-reference check type

```yaml
- type: final_answer_contains_prior_capture
  from_turn: 1                          # 1-indexed turn to read from
  from: tool_result                     # "tool_result" (default) or "final_answer"
  tool_name: fetch_url                  # optional: restrict to one tool's result(s) in that turn
  pattern: "<title>([^<]+)</title>"     # regex, exactly one capture group
```

Semantics: search `pattern` against `from_turn`'s tool-call results (or its
final answer, if `from: final_answer`), take capture group 1, assert it's a
substring of *this check's own turn's* final answer. Implementation
(`evals/checks.py`):

```python
def check_final_answer_contains_prior_capture(check, ctx):
    turn_ctx = ctx["turns"][check["from_turn"] - 1]
    source = check.get("from", "tool_result")
    if source == "final_answer":
        haystack = turn_ctx["final_answer"]
    else:
        events = [e for e in turn_ctx["events"] if e["type"] == "tool_call"]
        if check.get("tool_name"):
            events = [e for e in events if e["name"] == check["tool_name"]]
        haystack = "\n".join(e["result"] for e in events)
    m = re.search(check["pattern"], haystack)
    if not m:
        return False, f"pattern {check['pattern']!r} matched nothing in turn {check['from_turn']}'s {source}"
    captured = m.group(1)
    if captured in ctx["final_answer"]:
        return True, f"{captured!r} (from turn {check['from_turn']}) found in this turn's final answer"
    return False, f"{captured!r} (from turn {check['from_turn']}) NOT in this turn's final answer: {ctx['final_answer']!r}"
```

Two instances of this one check type (title pattern, id/url pattern) cover
"title AND url match" without a combined/compound check type — kept to the
one new type, as instructed. `label()` in `checks.py` needs one new branch
(`f"{t}:{check['pattern'][:24]!r}"`, matching the existing
`final_answer_regex` convention) — no other existing check needed changes.

Only turn 1's *tool results* are ever the source in both proposed cases —
`from: final_answer` is included above because it's nearly free to support
generically and closes an obvious next question, but nothing here currently
exercises it. Flagging in case you'd rather I drop it to keep the type
strictly to what's proven needed.

## 4. `informational: true` (case 2, negative control)

```yaml
name: arxiv-link-followup-no-digest
informational: true
turns:
  - task: "..."
  - task: "..."
    checks: [...]   # same two checks as case 1
```

`_run_case` records `informational` on the result dict (alongside the
existing `expected_flaky`); `main()`'s exit-code logic (currently implicit —
today's `main()` doesn't actually set a nonzero exit code on failure, it just
prints and writes JSON) would need `overall_pass_rate` from `informational`
cases excluded from whatever aggregate gate is added. Distinct from
`expected_flaky`: a flaky case still counts toward the suite's real pass
rate at a discounted expectation; an informational case never gates the
suite at all, pass or fail — it's pure telemetry.

### Open question this proposal surfaces (not in your original 6 items)

`PROPOSED_multi_turn.md`'s Case 2 was written to reproduce the *pre-fix*
behavior on demand — turn 2 built with final-answers-only history, no
digest — as a control proving Case 1 isn't passing by accident. That needs
one more per-case knob, e.g. `history_mode: no_digest` (default: full
digest), which nothing in this session's brief asked for. Two ways to close
this, your call:

- **(a)** Add `history_mode` now, alongside `turns:`/`informational:` — small,
  same shape as the other flags.
- **(b)** Drop Case 2 as originally conceived; let `informational: true`
  alone carry a *different* control (e.g. a case that reuses the same two
  turns but asserts the OPPOSITE — that mismatch used to happen — is
  awkward without the no-digest toggle, so (a) is probably simpler in
  practice, but I'm not deciding this unilaterally since it's schema surface
  beyond what was scoped).

## 5. Per-turn logging (results JSON)

Each turn already produces the numbers the gateway prints
(`history_len`/`digest_len`/steps) — `history_len` and `digest_len` are
computable the same way `discord_gateway._run_and_report` computes them
(`len(prior_messages)`, sum of `system`-role message content lengths) right
before that turn's `run_task` call. Proposed shape, one `runs[i]` entry for
a multi-turn case:

```json
{
  "log_file": null,
  "turns": [
    {"log_file": "run_....jsonl", "history_len": 0, "digest_len": 0, "steps": 2, "final_answer": "..."},
    {"log_file": "run_....jsonl", "history_len": 3, "digest_len": 1311, "steps": 1, "final_answer": "..."}
  ],
  "checks": [...],      // flattened across turns, each check_result gains a "turn" index
  "all_passed": true
}
```

## 6. Baseline smoke check (existing runner, unmodified)

Per the "confirm nothing changed" instruction — ran the existing
`hello-baseline` case with today's unmodified `evals/run.py` (no code was
touched this session, so this is a pre-change baseline, not a regression
check, but it's a live confirmation the harness/runner still behaves as
NOTES.md describes):

```
case                 n    pass    steps   eval_cnt   swallow  flaky
hello-baseline       1     100%  2.0     53.0       0        no
```

Matches the documented baseline shape (2 steps, no skill reached for, no
swallow). Result written to
`evals/results/results_20260714T235702Z.json`.

## Summary — what I need from you before writing any code

1. Approve (or redirect) the `harness/history.py` extraction — the actual
   blocker. Nothing else in this proposal can be implemented for real
   without it (implementing against copy-pasted logic was explicitly ruled
   out).
2. Sign off on, or amend, the `turns:` / `final_answer_contains_prior_capture`
   / `informational:` syntax above.
3. Decide §4's open question (`history_mode` toggle for Case 2, or drop Case 2
   as conceived).

No `.yaml` case files written, no runner code changed, existing 6 cases
untouched.
