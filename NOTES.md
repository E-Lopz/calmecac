# NOTES

Running log of deliberate stress tests against the harness (`harness/loop.py`,
`harness/tools.py`) with `qwen3:14b`. Goal: find where it breaks, and for each
break decide whether the fix belongs in the **prompt** (Kukulkán's system
prompt), a **skill** (a documented procedure), the **harness** (loop/tool
logic), or is an **escalation** (beyond what a 14B model can be expected to
do reliably). This taxonomy is the input to Phase 3 and to the eventual
Itzamná advisor design.

Each entry: what I did, what happened, taxonomy call, why.

---

## 1. Missing tool

Asked: "What is the current weather in Paris right now?" — no weather tool exists.

**Result:** Model answered directly in step 1, no hallucinated tool call:
> "I don't have access to real-time weather data or external services... You may want to
> consult a weather website, app, or service like Weather.com..."

No fabricated tool name, no fabricated weather data. Clean.

**Taxonomy: none needed.** This is the desired behavior already. Worth re-testing later once
more tools exist (the risk is a model hallucinating a *plausible*-sounding tool name when the
tool set is larger and the boundary between "have this" and "don't" is less obvious).

---

## 2. Malformed-output recovery

Asked for a file containing nested double/single quotes, an escaped backslash, and a newline —
classic JSON-in-JSON escaping territory for a tool call argument.

**Result, and a bigger finding along the way:** the *first* two attempts via `python -m
harness.run` both died with `httpx.ReadTimeout` inside `ollama_client.chat()` (hardcoded
`timeout=120`), before anything reached the log (the exception fires before `_log` is called in
`loop.py`, so there's zero trace of these two runs). Bypassing the harness with a direct
`httpx.post` (600s timeout) against the identical payload showed why: qwen3:14b's response used
a separate `thinking` field (not `content`) — 1456 tokens of visible chain-of-thought correctly
reasoning through the quote/backslash escaping — before emitting the tool call, taking 113.8s
wall time (~12.8 tok/s; `ollama ps` shows this model is split 23%/77% CPU/GPU, not fully
resident on GPU). That's within noise of the 120s cutoff, which is why two identical runs failed
and a third (out-of-harness) barely succeeded.

The tool call itself, once it arrived, was the interesting part: `write_file` was invoked with
only `{"path": "quotes.py"}` — the required `content` argument was silently dropped, even though
the thinking trace shows the model had correctly worked out the escaped string. Fed through the
real harness (via a scratch script that monkeypatches `httpx.post`'s timeout up for
observation only, not editing tracked code), `loop._call_tool` correctly caught this as a
`TypeError` from `write_file()` and returned `"bad arguments for 'write_file': ...missing 1
required positional argument: 'content'"` as the tool result — exactly the feed-the-error-back
design working as intended. (Full retry-loop transcript pending — see below.)

**Taxonomy — two separate findings:**
- **Harness bug:** `ollama_client.py`'s hardcoded `timeout=120` is too tight for qwen3's
  thinking-mode overhead on this hardware, and `stream: false` means a timeout gives *zero*
  visibility into whether the model was making progress or hung — it just looks like a network
  error. This will hit any nontrivial tool-calling task, not just this one. Proposed fix (not
  applied — out of scope to change unilaterally per CLAUDE.md): make the timeout configurable in
  `config.yaml`, and/or investigate `stream: true` for progress visibility. Surface to the user
  as a real blocker before doing more multi-step live testing.
- **Harness, working as designed:** the dropped-argument tool call is exactly what `_call_tool`'s
  `TypeError` branch exists for, and it worked. No fix needed here — this is a good sign for the
  error-feedback design.

**Retry-loop follow-up (same task, run to completion via a scratch script that only bumps the
observation timeout, not tracked code):** the harness fed the `TypeError` back as instructed, and
the model recovered in exactly one retry:

```
[step 1] write_file({'path': 'quotes.py'})                         -> bad arguments: missing 'content'
[step 2] write_file({'path': 'quotes.py', 'content': "MESSAGE = 'He said, \"it's a \"nested\" quote,\" then paused. \\\\ \\n'"})
                                                                     -> wrote 64 bytes
[step 3] read_file({'path': 'quotes.py'})                           -> (echoes the same content back)
[step 4] final answer: quotes the file contents, done.
```

Error-feedback retry: **1 retry to recovery, 4 steps total.** Exactly the design working.

But the *content* it recovered with is broken: `MESSAGE = 'He said, "it's a "nested" quote,"
then paused. \\ \n'` — an unescaped `'` inside a single-quoted string. Confirmed by actually
running it:
```
SyntaxError: unterminated string literal (detected at line 1)
```
The model's `thinking` trace (see above) shows it correctly reasoned about needing to escape the
inner double quotes, but never separately reasoned about the apostrophe in "it's" clashing with
its choice of single-quotes as the outer delimiter — a scope-tracking gap, not a JSON-escaping
gap. The harness's job (produce valid JSON tool arguments) succeeded; the *content* of those
arguments doesn't fulfill the task. Nothing downstream ever notices — `read_file` and the final
answer just echo the broken content back verbatim.

**Taxonomy — third finding:** **prompt**. The harness has no way to know if written content is
semantically correct (and shouldn't — validating arbitrary file content isn't its job). The gap
is that Kukulkán's system prompt (`agents/kukulkan/prompt.md`) doesn't instruct the model to
verify/self-check generated code before declaring it done, and nothing prompts it to notice the
mismatch between "I was asked to write valid Python" and "I wrote unparseable Python." A prompt
addition like "after writing code, sanity-check it before reporting success" is cheap to try;
this is exactly the kind of failure that class of instruction is meant to catch. Not a harness or
escalation issue — a 14B model got the *escaping mechanics* right when explicitly reasoning about
them, it just didn't apply the same scrutiny to its own output before calling it done.

---

## 4. Budget exhaustion

Asked for 15 files (`part_01.txt`...`part_15.txt`), one `write_file` call per step, no batching
— genuinely needs ~15-17 steps (15 writes + a `list_dir` + a final answer) against the real
`max_steps: 10` from `config.yaml`.

**Result:** textbook linear exhaustion, not a stuck loop:

```
step 1  write_file(part_01.txt) -> wrote 15 bytes
step 2  write_file(part_02.txt) -> wrote 15 bytes
...
step 9  write_file(part_09.txt) -> wrote 15 bytes
step 10 write_file(part_10.txt) -> wrote 16 bytes
step 10 aborted: exceeded max_steps (10) without a final answer
```

Confirmed on disk: exactly 10 of 15 files exist. Every step was a distinct, successful,
non-repeated tool call — it respected "one at a time," never batched, never erred, never retried
the same file. It just ran out of runway with clean, monotonic progress.

**Is the abort message useful?** No — and this is the interesting part. `"aborted: exceeded
max_steps (10) without a final answer"` (`loop.py:93`) carries zero information about *what kind*
of exhaustion happened. Reading it in isolation, you cannot tell "10/15 done, on track, just
needed more budget" apart from "spent 10 steps re-reading the same file / retrying the same
failed call / going in circles." The only way to tell those apart right now is to open the
`.jsonl` log by hand and manually check whether the tool calls were distinct and error-free. For
a human debugging one run that's tolerable; it's exactly the thing an automated escalation
(future Itzamná) would need computed *for* it, not left as an exercise.

**Taxonomy: harness — and this is explicit requirements-gathering for the advisor design.** The
fix isn't a smarter model or a bigger `max_steps`; it's surfacing a cheap, purely mechanical
signal at abort time, since everything needed to compute it is already in `messages`/the log:
- **count of distinct successful tool calls** vs. total steps (this run: 10 distinct / 10 steps —
  a "clean progress, just needs more budget" signature)
- **count of repeated identical calls or repeated errors** (would indicate a stuck loop instead)

That distinction — "ran out of room while genuinely progressing" vs. "spun in place" — is exactly
the signal a stuck-state escalation to a future advisor would need to decide *how* to intervene
(bump the budget vs. hard-stop and ask a human vs. try a different approach). Don't build the
escalation itself yet (out of phase per CLAUDE.md), but this is the concrete requirement to carry
into that design: the abort path needs a progress/stuck classifier, even a trivial one (e.g.
`len(set(successful_calls)) == len(successful_calls)` as a first-pass "was it looping" check),
before it's meaningful to hand off to anything smarter.

---

## 5. Long-context behavior

Built a ~35.5KB / ~6,300-estimated-token filler file (`workspace/big_context.txt`, 49 near-
identical paragraph blocks) with one needle line at ~65% depth: `NOTE TO READER — SECRET-CODE:
QUETZAL-7734 — this line is not part of the historical text above.` (First attempt used a
~192KB/~34k-token file — realized before running it that this would likely blow past `num_ctx:
32768` once the system prompt, tool schemas, and thinking budget were added, so shrank it rather
than test an overflow I hadn't set out to test.)

Task: read the file, write *only* the code to `found_code.txt`, then answer in a strict format
(`CODE: <code>`, no preamble, no extra text).

**Result:**
```
step 1  read_file(big_context.txt)         -> full 49-block file returned
step 2  write_file(found_code.txt, ...)    -> wrote 12 bytes    [called TWICE, see below]
step 3  final answer: "CODE: QUETZAL-7734"
```

- **Retrieval: perfect.** Needle found correctly among 49 near-identical filler blocks.
- **Format compliance: perfect.** Exactly `CODE: QUETZAL-7734`, no preamble, no bullet points, no
  extra sentence — despite qwen3's general tendency (seen in experiment 2) to wrap answers in
  explanation. Instruction-following did *not* visibly degrade at this context size.
- **Unexpected artifact:** step 2 contains the *identical* `write_file(found_code.txt,
  "QUETZAL-7734")` call twice in a row, each with a distinct call ID, confirmed in the raw
  `.jsonl` log (not a print/logging duplication — `message.tool_calls` genuinely had two entries).
  Harmless here since `write_file` is idempotent (second call just re-wrote the same 12 bytes),
  and it didn't cost an extra ReAct step (both calls executed within the same step's `tool_calls`
  list per `loop.py`'s inner `for call in tool_calls` loop) — but it's a real, unprompted
  redundant action.

**Taxonomy:**
- **Retrieval/format-compliance at this size: none needed.** ~6,300 tokens of context didn't
  budge either capability. This is a useful negative result, not a null one — it sets a floor:
  whatever degradation the "instruction-following degrades before coherence" hypothesis predicts,
  it isn't visible yet at ~20% of `num_ctx`. Would need a follow-up nearer the ceiling (~25-30k
  tokens) to actually stress this; didn't run that tonight to keep wall-clock time reasonable
  (generation is the slow part, ~13 tok/s on this hardware, not context ingestion).
- **Duplicate tool call: harness, latent.** `_call_tool` executes every entry in `tool_calls`
  unconditionally, with no dedup and no assumption that calls are idempotent. Cost nothing
  tonight because `write_file` overwrites are safe to repeat. It stops being harmless the moment
  a non-idempotent tool exists (append-only log, send-email, anything with side effects that
  compound) — the harness would silently double-execute it. Not urgent to fix now (no such tool
  exists yet, and CLAUDE.md says don't build ahead of the current phase), but worth flagging as a
  known gap to close *before* adding any non-idempotent tool, not after.

---

## Summary — taxonomy tally

| # | Experiment | Finding | Taxonomy |
|---|---|---|---|
| 1 | Missing tool | Answered honestly, no hallucination | none (already correct) |
| 2a | Malformed output | Hardcoded 120s timeout too tight for qwen3 thinking-mode latency on this hardware; failures look like generic network errors (no streaming) | **harness** |
| 2b | Malformed output | Dropped required arg correctly caught and fed back by `_call_tool` | none (working as designed) |
| 2c | Malformed output | Recovered arg was syntactically invalid Python (unescaped apostrophe); nothing downstream checks | **prompt** |
| 3 | Path escape | All traversal + symlink probes blocked | none (guard is solid) |
| 4 | Budget exhaustion | Clean linear progress (10/15 files), abort message carries no progress/stuck signal | **harness** (concrete requirement for future advisor) |
| 5a | Long context | Needle retrieval + strict format both held at ~6.3k tokens | none (no degradation at this size — floor, not ceiling) |
| 5b | Long context | Model emitted a genuine duplicate tool call in one turn | **harness**, latent (matters once non-idempotent tools exist) |

Biggest actionable item: the **120s timeout** (2a) is the one thing that will bite reliably on
any future session doing real tool-calling work with this model, and it fits CLAUDE.md's "ask
before adding config" bar — worth raising with the user directly rather than just leaving it in
notes.


## 3. Path-escape probes

Tested `tools._resolve()` directly (not through the model — this is adversarial verification of
guards the *agent* wrote, per CLAUDE.md's own precedent of `python -c` checks) with:

| probe | result |
|---|---|
| `../config.yaml` | blocked — "escapes the workspace directory" |
| `../../etc/passwd` | blocked |
| `/etc/passwd` (absolute) | blocked — "must be relative to the workspace directory" |
| `subdir/../../config.yaml` | blocked |
| `./../CLAUDE.md` | blocked |
| `notes.txt` (sanity check, valid) | allowed |
| symlink inside workspace → `/etc/passwd` | blocked |
| symlink inside workspace → `../CLAUDE.md` | blocked |

Every escape vector I could think of — including the symlink case, which isn't explicitly
mentioned in `tools.py`'s own docstring — is caught. Symlinks work because `.resolve()` follows
them to the real target path before the `WORKSPACE not in resolved.parents` containment check
runs, so a symlink pointing outside workspace fails the same check a raw `..` would.

**Taxonomy: none needed.** The guard is correct and more robust than its comment suggested
(it doesn't call out symlink handling, but handles it correctly as a side effect of resolving
before checking). Didn't additionally spend live model calls trying to get the *agent* to attempt
these paths, since the containment check is argument-content-only — it doesn't matter whether the
path string comes from a model or from `python -c`, the same code path runs either way.

---
