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

## 6. Empty-response flake: skill names silently mistaken for tool names

Discovered while measuring `load_skill` trigger rate (`scripts/trigger_test.py`) for the two
hand-written skills from Phase 3 (`write-and-verify-code`, `estimate-before-batch`). Five
identical runs of the A′ task — "Write a Python file containing tricky quotes: nested double and
single quotes, an apostrophe in prose, and a triple-quoted string" — against the real harness
(skills index in the system prompt, `think: false`, the four registered tools including
`load_skill`) all came back the same way: `content: ""`, no `tool_calls`, step 1, no data on
whether the skill would have triggered at all.

**Reproduced outside the harness** (`scripts/probe_flake.py`, direct `/api/chat` calls with the
exact same system prompt, tools list, and options the harness sends): 10/10 across two passes at
`temperature: 0.1`. Every flake shows real generation happened — `eval_count` 100–184,
`done_reason: "stop"` — but nothing surfaces anywhere: not `content`, not `thinking` (`think:
false`), not `tool_calls`. Silent, not a crash, not a timeout.

**Narrowing the variable** (3-cell grid: index+tools+think=false / index+tools+think=true /
no-index+tools+think=false): the no-index cell (the exact Phase 2.5 acceptance shape) was clean
2/2, ruling out "tools + think=false is broken in general." The index+think=true cell was only
1/2 clean, ruling out "thinking-suppression is the sole cause." The one constant across every
broken cell was the skills index text itself.

**Seeing the raw generation** (`scripts/raw_probe.py`, hand-rendered ChatML template against
`/api/generate` with `raw: true`, bypassing Ollama's chat templating and tool-call parsing
entirely — note this still splits `<think>` content into a separate field even in raw mode, a
finding in itself): with the model forced past an empty `<think></think>` block, it emitted a
well-formed `<tool_call>` naming **`write-and-verify-code`** — a skill name, not a tool name —
with `write_file`-shaped arguments (`path`, `content`).

**Direct confirmation via `/api/chat`:** a "clincher" probe asking the model to call a wholly
invented tool name (`frobnicate_file`, no skills index present) got an honest, correct decline in
plain text (`content`: "The tool `frobnicate_file` is not available.", `eval_count: 14`) — so
Ollama does *not* silently drop just any unknown-name call. But asking it to call
`write-and-verify-code` directly (skills index present, real tools list) reproduced the exact
flake signature: `content: ""`, `tool_calls: 0`, `done_reason: "stop"`, `eval_count: 24`. The
trigger isn't "unknown name" in general — it's a name that's real, present in the prompt three
lines above the tool list, and not registered as a tool.

**Attempted prompt fix, didn't hold:** rewrote the skill index (`harness/loop.py`) to state
explicitly "these are NOT tools" and give a worked `load_skill(name="write-and-verify-code")`
example. Reran A′ 5x: still 5/5 flake, same signature (`eval_count` 110–129). The rewrite closed
the narrow direct-collision case above but didn't change the model's behavior on the real task.

**Settling malformed-JSON vs. wrong-name** (`scripts/ablation_probe.py`, content-dependence
ablation): a trivial write (`hello.txt`) and a one-escaped-quote write (`greeting.txt`) both
passed 3/3 with valid `write_file` calls; the full A′ task flaked 3/3. Consistent with either
"malformed JSON under quote load" or "wrong tool name" — ambiguous on its own. Resolved by
recapturing the actual A′ generation with the revised (already-"NOT tools") system prompt: all 3
raw captures produced a well-formed `<tool_call>`, **valid JSON every time** (`json.loads`
succeeded, no parse errors to report), naming `write-and-verify-code` instead of `write_file` in
all three. The escaping was correct; the tool name was wrong.

**Taxonomy — two separate findings:**
- **Prompt/skill-design.** The model treats skill names as directly callable once they're listed
  near the real tools, and this confusion survives an explicit "these are NOT tools, call
  `load_skill(name=...)`" rewrite of the index — the disambiguation didn't take. Whatever fixes
  this (rewording again, restructuring the index, a stronger example, moving skill names further
  from the tool-call vocabulary) is a prompt-design question, not resolved by this investigation.
- **Harness/upstream, and the more urgent one.** Ollama 0.31.2's `/api/chat` silently drops a
  well-formed `tool_call` whose name isn't in the request's `tools` list — no error, no
  degraded-content fallback, just an empty message with real tokens spent generating it
  (confirmed directly via the `frobnicate_file` clincher). This defeats the harness's
  feed-the-error-back design (`_call_tool`'s unknown-tool-name branch, finding 2b) by construction
  — that branch only runs if a `tool_calls` entry reaches the harness at all, and here nothing
  does. From the harness's point of view this is indistinguishable from the model silently giving
  up. Whether the fix belongs in the harness (detect zero-tool_calls + zero-content + nonzero
  eval_count as a distinct "swallowed call" case, distinct from finding 4's abort classifier) or
  is worth an upstream Ollama report is an open question — not resolved here, per this session's
  diagnosis-only scope.

All probe scripts (`scripts/probe_flake.py`, `scripts/raw_probe.py`, `scripts/ablation_probe.py`,
`scripts/trigger_test.py`) are scratch/diagnostic, not part of the harness proper.

---

## 7. Skills-as-tools redesign (Phase 3.5): finding 6b fixed, a residual flake surfaces

Following finding 6's diagnosis (skills listed as a text index alongside real tools caused the
model to call skill names directly, which Ollama's `/api/chat` then silently dropped), Phase 3.5
redesigned skills to be tools: `harness/tools.py` now scans `skills/` at import time and registers
each skill's frontmatter `name`/`description` directly into `REGISTRY`, with a no-argument
callable that returns the SKILL.md body. `load_skill`, `_resolve_skill`, and `list_skills` are
gone; so is the "Available skills" text block `loop.py` used to append to the system prompt —
skills are now indistinguishable from `read_file`/`write_file`/`list_dir` as far as the model's
`tools` list is concerned. Tool-call logging still tags skill executions with `"skill_loaded":
<name>`, now keyed off `tools.SKILL_NAMES` (a frozenset of registered skill names) rather than a
`load_skill` argument.

**Acceptance, A′ ×5** (the same tricky-quotes task that flaked 15/15 across finding 6's probes):
0/5 completed cleanly, but run 2 is the direct confirmation the targeted mechanism is fixed —
`write-and-verify-code({'path': ..., 'content': ...})` was called (the model still reached for the
skill by name, using `write_file`'s argument shape instead of no arguments), and this time it
**surfaced**: executed, hit a real `TypeError` (`_make_skill_func.<locals>.run() got an unexpected
keyword argument 'path'`), and got fed back through the normal `_call_tool` error path — logged
with `skill_loaded: "write-and-verify-code"` and `error: true`. Under finding 6's Ollama-side
silent-drop mechanism, that exact call would have vanished with zero trace. It didn't.

**But a residual flake persists, unrelated to skill naming.** The other 4/5 A′ runs were bare
`step 1: empty content, no tool_calls` — no call attempted at all, no skill name, nothing to drop.
This same signature was seen once before skills existed (Phase 3 testing, same task) — it's not
new, and it's not what Phase 3.5 targeted. `T1` (trivial write_file task) stayed clean 3/3, and the
weather question stayed an honest decline 1/1, so this residual flake looks specific to task
complexity/content again (echoing finding 6's `ablation_probe.py` result), not a general
regression.

**Taxonomy:**
- **6b considered fixed.** Registering skills as real tools means there's no longer a plausible,
  well-formed call to a name absent from `tools` — the specific silent-drop path Ollama 0.31.2
  exhibited is structurally closed off by this design, not papered over. Confirmed by direct
  observation (run 2), not just inference.
- **6a superseded, not fixed.** The prompt-side confusion (model treats skill names as callable)
  wasn't fixed by the earlier prompt rewrite and is arguably still present in spirit — the model in
  run 2 called the skill with `write_file`'s arguments rather than no arguments, suggesting it
  still doesn't fully understand what a skill-as-tool call is supposed to *do*. It stopped mattering
  operationally because the redesign makes any such call harness-visible instead of invisible, but
  the underlying model confusion about skill semantics is unresolved.
- **New, open: residual empty-response flake, mechanism unknown.** 4/5 A′ runs produced zero tool
  calls and empty content on step 1 — no plausible parser-side cause (no call was attempted at
  all), so this isn't finding 6's mechanism. Content-dependent (T1 clean, A′ flaky), consistent
  with finding 6's `ablation_probe.py` result, but not yet isolated the way finding 6 was.
  Undiagnosed as of this writing — explicitly out of scope for Phase 3.5 per instructions (no
  skill-body or prompt tuning this session).

---

## 8. Skill-tool leniency: from silent TypeError to false-success to fixed

Phase 3.6 added two harness changes on top of Phase 3.5's skills-as-tools redesign, both driven
directly by finding 7: (1) diagnostic logging — `loop.py` now logs `eval_count`,
`prompt_eval_count`, `done_reason` on every `model_call` log line, a `run_start` line recording
the full outbound messages and registered tool names, and a `generation_swallowed` flag (+
stdout warning) when a step's response has empty content, no `tool_calls`, and `eval_count > 20`
— the mechanical signature finding 7 left undiagnosed; and (2) skill-tool argument leniency —
`_make_skill_func` in `tools.py` was changed to accept and silently ignore any keyword
arguments, since Phase 3.5's A′ run 2 showed the model calling skill-tools with
`write_file`-shaped arguments (`path`, `content`) and getting a bare `TypeError` back.

**The leniency fix backfired in a new way.** Rerunning A′ ×5 after the change: 3/5 runs called
`write-and-verify-code` with `write_file`-shaped args, got the skill body back with **no error**
— and then the model declared success in the very next step **without ever calling
`write_file`**. No file landed in `workspace/` in any of the 5 runs. The other 2/5 hit the new
`generation_swallowed` detector cleanly (the same undiagnosed residual flake from finding 7,
confirmed still present and now instrumented). The diagnostic logging worked exactly as
designed — it caught this new failure mode immediately, in the very first acceptance run after
shipping it.

**Phase 3.7 fixed the false-success problem directly.** `_make_skill_func` now prefixes the
returned body with an explicit notice when called with arguments: `"NOTE: '<name>' is an
instructions-only tool. Your arguments were IGNORED — no file was written and no action was
taken..."` followed by the skill body. Rerunning A′ ×5 again: **4/5 runs now proceed to a real
`write_file` call** after receiving the notice (up from 0/5), all four written files parse as
valid Python (`ast.parse`, zero `SyntaxError`s), and escaping is correct in every case. The 5th
run hit the same pre-existing `generation_swallowed` flake before any tool call was attempted —
unrelated to this change, confirmed by the log.

**One gap survives the fix.** The skill's own procedure (`write-and-verify-code/SKILL.md`)
explicitly instructs: "After write_file succeeds, read_file the result and check... Only report
success after step 4 passes." None of the 4 successful runs called `read_file` — every one went
`write_file` → final answer directly. The model absorbed the skill's *escaping* guidance (the
written content is consistently correct) but skipped its *verification* step every time. Not
investigated further per this session's scope.

**Taxonomy:**
- **False-success failure mode (Phase 3.6 leniency): fixed by Phase 3.7's notice prefix.**
  Confirmed by direct observation — 4/5 vs 0/5 real `write_file` calls, before/after.
- **Diagnostic logging (`generation_swallowed`, `run_start`, `eval_count`/`prompt_eval_count`/
  `done_reason`): working as designed.** It surfaced the false-success mode immediately and
  continues to correctly flag finding 7's residual flake every time it recurs (2/5 here) — this
  is now the standing instrumentation for diagnosing future skill-tool and swallow-related
  issues.
- **New, open: skill procedure adherence is partial.** The model follows a skill's substantive
  content (escaping rules) without following its explicit process instructions (the read-back
  verification step). Whether this needs a prompt-level fix, a skill-wording fix, or is out of
  scope for a 14B model is unresolved — not investigated this session.
- **Finding 7's residual empty-response flake: still open, unaffected by Phase 3.6/3.7.**
  Recurred at a similar rate across both phases here (2/5, then 1/5 — small samples, not
  directly comparable), independent of the skill-tool changes made in either phase.

---

## 9. Phase 4 baseline regression: `estimate-before-batch` triggers, then the batch never happens

Surfaced by the Phase 4 eval suite's `batch-15-files` case (`evals/cases/batch-15-files.yaml`,
same 15-file task as finding 4), not by manual probing — the first case where the harness was
exercised through the automated runner instead of a one-off script. v0.1 baseline, `--n 5`
against `qwen3:14b`: **0/5**, but not for finding 4's reason.

**Result — identical shape across all 5 runs.** Transcript from run 1
(`logs/run_20260712T002315Z.jsonl`, not committed — `logs/` is gitignored; excerpted here as the
evidence for this finding):

```
step 1  model_call: content='', tool_calls=[{function: {name: 'estimate-before-batch', arguments: {}}}]
step 1  tool_call: estimate-before-batch({}) -> ok (returns the skill body)
step 2  model_call: content="This task needs approximately 15 steps (one for each file write)
                     plus a few for verification and the final answer. Since the budget is not
                     specified, I will proceed with creating the files. Let's start with the
                     first few files." tool_calls=None
```

Step 2's `content` is treated as the final answer (no `tool_calls`) — the run ends there. All 5
baseline runs match this shape almost verbatim (see `evals/results/results_20260712T002611Z.json`
for the other 4 final-answer strings), with estimates ranging "approximately 15" / "15 file
writes" / "one for each file creation" — the estimate itself is consistently correct.

**This is not finding 4.** Finding 4 (pre-skill) showed clean linear progress — 10/10 distinct,
error-free `write_file` calls before running out of budget. Here, **zero** `write_file` calls
happen in any of the 5 runs. The model correctly triggers the skill, correctly computes the step
count, states "I will proceed... let's start," and then stops — never emitting the tool call that
sentence claims is coming next.

**It also doesn't follow the skill it just read.** `estimate-before-batch/SKILL.md` step 2 is
explicit: if the estimate exceeds the budget, "do NOT start. Instead, report immediately: '...
Options: raise the budget, or I complete the first Y-2 items now.'" The model did neither branch
— it didn't decline with that framing, and it didn't start. It also never learned what the budget
*is* ("Since the budget is not specified") — `max_steps` is never communicated to the model
anywhere in the system prompt, task, or skill body, so the skill's own step 2 comparison ("compare
against your step budget") has no value to compare against. That gap predates this finding but is
now directly implicated: the model can't decline-with-reason against a budget it was never told.

**Why the eval case didn't catch this cleanly at first.** The case's original single check,
`final_answer_regex: {pattern: "progressing"}`, was written to detect finding 4's abort signature
(`_abort_stats`'s "progressing" verdict string) and correctly failed here too, since no abort ever
happens — but it failed for the right *symptom*, not the right *reason*: a run that legitimately
completed the whole batch early (unlikely for a 15-step task, but not the point) would also not
say "progressing," and would also fail this check, indistinguishably from the zero-progress case
observed here. Added `tool_called: {name: write_file}` to the case so a future run is graded on
whether real progress happened, not just on the absence of one specific abort-classifier string.

**Taxonomy: prompt/skill-design, open.** Not a harness bug — `_call_tool`, the abort classifier,
and the skill-tool plumbing all behaved exactly as designed; the model simply never called
`write_file`. Candidate fixes (none applied — out of scope for a diagnosis/eval-authoring session
per this phase's instructions): communicate `max_steps` to the model somewhere in-context so
`estimate-before-batch`'s budget comparison is meaningful, and/or strengthen the skill's step 2
wording so "I will proceed" is never accepted as a substitute for an actual next tool call.
**Follow-up below (finding 10): the budget-communication candidate fix was tried and the
"I will proceed" pattern is gone — see there before assuming both fixes are still needed.**

---

## 10. Finding 9 follow-up: budget injection alone fixes the follow-through failure

Finding 9 named two independent candidate fixes: (a) tell the model what `max_steps` is
(a harness change — the skill's own step 2 asks it to "compare against your step budget," but
the budget was never in context anywhere), and (b) a task-independent prompt line forcing a tool
call or explicit decline after consulting any skill (a prompt change, to stop "I will proceed"
from being accepted as if it were an action). Ran them as separate arms deliberately, budget
injection alone first — if the model can do the comparison, it may decline or batch correctly
with no follow-through pressure needed, which would mean the prompt line isn't needed at all.

**Arm 1 change (harness, `harness/loop.py`, one line):** `run_task` now reads `max_steps` before
building the system prompt and appends `f"\n\nYou have a budget of {max_steps} steps for this
task."` to it. `agents/kukulkan/prompt.md` and the skill files are untouched — this is pure
harness-transparency, nothing new is asked of the model.

**Result — full suite, `--n 5`, arm 1 only:** `hello-baseline`, `tricky-quotes`,
`weather-decline`, `swallow-canary` all unchanged (100%/100%/100%/no new swallows) — no
regression from the added system-prompt text. `batch-15-files` is still 0/5 against its current
checks (`final_answer_regex: progressing`, `tool_called: write_file`), but the *shape* of the
failure changed completely, and consistently, across all 5 runs. Transcript, run 1
(`logs/run_20260712T005727Z.jsonl`):

```
step 1  model_call: content='', tool_calls=[{function: {name: 'estimate-before-batch', arguments: {}}}]
step 1  tool_call: estimate-before-batch({}) -> ok
step 2  model_call: content="This task needs approximately 17 steps (15 writes + list_dir +
                     answer) but the budget is 10. Options: raise the budget, or I complete the
                     first 8 and report." tool_calls=None
```

All 5 runs produced this same message near-verbatim (`logs/run_20260712T005731Z.jsonl`,
`...005734Z`, `...005738Z`, `...005741Z`). Compare against `estimate-before-batch/SKILL.md`'s own
worked example: *"RIGHT: 'This needs ~17 steps (15 writes + list_dir + answer) but the budget is
10. Should I proceed with the first 8 and report, or should the budget be raised?'"* — the model
is now reproducing the skill's own prescribed response almost word for word, with correct
arithmetic (17 vs. the actual budget of 10, not a guess), instead of finding 9's "the budget is
not specified... I will proceed... let's start" non sequitur.

**Interpretation.** Finding 9's "I will proceed" pattern — declaring intent to act and then
answering instead of acting — is gone in all 5 runs, replaced by an honest, immediate,
correctly-reasoned decline that follows `estimate-before-batch` step 2 to the letter. That
satisfies the decision rule set going in: the model declines properly with no follow-through
pressure applied, so finding 9 was a harness-transparency bug, not a follow-through gap — **arm 2
(the "next step must be a tool call or explicit decline" prompt line) is not needed for this
failure mode** and was not added, preserving that prompt real estate. Arm 1 is committed as a
standing harness change on this evidence.

**Open, not closed by this experiment: the eval case itself.** `batch-15-files`'s checks
(`final_answer_regex: progressing`, `tool_called: write_file`) were written for finding 4's
shape (partial linear progress, then an abort) and finding 9's shape (zero progress, no abort,
no decline). Neither anticipated arm 1's new outcome — an explicit, correct, budget-aware decline
with no `write_file` calls and no abort. Under the *current* checks this still reads as 0/5
"failure," which is no longer an accurate description of what's happening. Whether to update the
case to recognize "explicit decline" as a passing outcome, keep it strictly scoped to the
original budget-exhaustion shape, or split it into two cases is a real design decision, not
resolved here — flagged rather than silently patched, since loosening a check to force green
without deciding what "correct" means here would hide the judgment call rather than make it.

**Taxonomy: harness (fixed, this phase) — the transparency gap named in finding 9 is closed and
validated by direct before/after transcript comparison.** The residual "8c"-style gap (does the
model's decline actually happen, not just get planned) turned out not to exist once the budget
was visible; no prompt change was needed to get there.
**Revised by finding 11 below: this conclusion only holds for the decline branch. The same
"I will proceed" pattern reappears, unchanged, once the budget is sufficient and proceeding is
the correct call — read finding 11 before treating arm 2 as unnecessary in general.**

---

## 11. Finding 10's conclusion only covers the decline branch — "I will proceed" persists when proceeding is correct

Finding 10's test only exercised one branch of `estimate-before-batch` step 2: budget insufficient
(10 vs. ~17 needed) → correct decline. To check whether budget-injection fixed the underlying
declaration-substituting-for-execution pattern in general, or only happened to fix the branch
where declining *was* the correct action anyway, added a companion case,
`batch-15-files-sufficient-budget.yaml` — identical task, `max_steps: 20` (task needs ~15-17), so
completing the batch is the correct call, not declining.

**Result — `--n 5`, same arm-1 harness (budget already injected): 0/5, uniform across all 5
runs.** `completed` check passes 100% (the run never aborts) but every `file_written` check for
`part_01.txt` through `part_15.txt` is 0% — **zero files were written in any of the 5 runs.**
Transcript, run 1 (`logs/run_20260712T010421Z.jsonl`):

```
step 1  model_call: content='', tool_calls=[{function: {name: 'estimate-before-batch', arguments: {}}}]
step 1  tool_call: estimate-before-batch({}) -> ok
step 2  model_call: content="This task needs approximately 15 steps (15 writes) but the budget
                     is 20. I will proceed to create all 15 files." tool_calls=None
```

All 5 runs match this shape (`logs/run_20260712T010424Z.jsonl`, `...010428Z`, `...010432Z`,
`...010435Z`): the model correctly computes the estimate, correctly reads the now-visible budget,
correctly concludes 20 ≥ 17 so it should proceed — states "I will proceed to create all 15
files" — and then the run ends there, on step 2, as a final answer. No `write_file` call, in any
run, ever happens.

**This falsifies finding 10's "arm 2 not needed" conclusion.** The declaration-substituting-for-
execution pattern from finding 9 is not fixed by budget visibility — it was never really about the
budget. What changed between finding 9 and finding 10's test was that, coincidentally, the correct
action given an insufficient budget *is* an immediate text answer (a decline), so a model that
turns its own stated intent into a final answer looks correct by accident. The moment the correct
action is a *tool call* (proceeding with real writes), the same underlying bug — "I said I would
act" gets treated as equivalent to "I acted" — reappears identically, budget known or not.

**Taxonomy: prompt/skill-design, open again.** Finding 10's harness fix (budget injection) stands
on its own merits — the decline branch is measurably better and the change is harmless elsewhere
(no regressions across the other 4 baseline cases) — but it does not close finding 9. The
task-independent follow-through line proposed alongside finding 9 (arm 2: "after consulting a
skill, your next step must be a tool call or an explicit decline — never a statement of intent")
is back on the table as the more likely actual fix, since this finding shows the bug is
budget-independent. Not applied here — flagging for a decision before editing
`agents/kukulkan/prompt.md`, since finding 10's premature conclusion is exactly the kind of miss
a second data point was needed to catch.

---

## Summary — taxonomy tally

| # | Experiment | Finding | Taxonomy |
|---|---|---|---|
| 1 | Missing tool | Answered honestly, no hallucination | none (already correct) |
| 2a | Malformed output | Hardcoded 120s timeout too tight for qwen3 thinking-mode latency on this hardware; failures look like generic network errors (no streaming) | **harness** (fixed Phase 2.5) |
| 2b | Malformed output | Dropped required arg correctly caught and fed back by `_call_tool` | none (working as designed) |
| 2c | Malformed output | Recovered arg was syntactically invalid Python (unescaped apostrophe); nothing downstream checks | **prompt** (addressed Phase 2.5) |
| 3 | Path escape | All traversal + symlink probes blocked | none (guard is solid) |
| 4 | Budget exhaustion | Clean linear progress (10/15 files), abort message carries no progress/stuck signal | **harness** (addressed Phase 2.5) |
| 5a | Long context | Needle retrieval + strict format both held at ~6.3k tokens | none (no degradation at this size — floor, not ceiling) |
| 5b | Long context | Model emitted a genuine duplicate tool call in one turn | **harness**, latent (matters once non-idempotent tools exist) |
| 6a | Empty-response flake | Model calls skill names as if they were tools; explicit "NOT tools" prompt rewrite didn't fix it | **prompt** (superseded by 7, not fixed) |
| 6b | Empty-response flake | Ollama 0.31.2 silently drops well-formed tool_calls naming an unregistered tool — no error ever reaches the harness | **harness/upstream** (**fixed Phase 3.5**, see 7) |
| 7 | Skills-as-tools redesign | Skill-as-tool calls now surface and error cleanly instead of vanishing; a separate, content-dependent empty-response flake remains on the same task | **harness** (6b fixed) + **open** (residual flake, undiagnosed) |
| 8a | Skill-tool leniency | Argument leniency (Phase 3.6) let the model treat "got a response back" as "action happened" — 0/5 real writes | **harness** (**fixed Phase 3.7**, notice prefix) |
| 8b | Skill-tool leniency | New diagnostic logging (`generation_swallowed`, `run_start`, eval/done fields) caught 8a immediately on first use | none (working as designed) |
| 8c | Skill-tool leniency | Model follows a skill's escaping guidance but skips its explicit verification step every time | **prompt/skill-design** (open) |
| 9 | Phase 4 baseline (`batch-15-files`, 0/5) | `estimate-before-batch` triggers and estimates correctly, then the model says "I will proceed" and stops — zero `write_file` calls, budget never communicated to it | **prompt/skill-design** (harness-transparency arm fixed by 10, but 11 shows the underlying "declare intent, don't act" bug is still open) |
| 10 | Finding 9 follow-up (budget injection, arm 1) | One-line harness fix (inject `max_steps` into the system prompt) turns "I will proceed" into an honest, correctly-reasoned decline matching the skill's own worked example, on the tight-budget case | **harness** (**fixed this phase** — real, no regressions — but see 11: doesn't close 9 in general) |
| 11 | Finding 10 follow-up (sufficient budget, same task) | Same task with `max_steps: 20` (headroom to actually finish) still goes 0/5 on every `file_written` check — `completed` passes but zero files get written; "I will proceed" persists even when proceeding is correct, falsifying 10's "no prompt change needed" conclusion | **prompt/skill-design** (open again — arm 2, the follow-through line in `agents/kukulkan/prompt.md`, is back on the table, not yet applied) |

Biggest actionable item: **2a, 6b, 8a are fixed** (Phases 2.5, 3.5, 3.7 respectively), and **10's
harness fix is real and stands** (budget transparency, no regressions) — but it only fixed the
decline branch. **9 is not actually closed**: finding 11 shows the same "say it, don't do it"
failure recurs, unchanged, once proceeding rather than declining is the correct call. Arm 2
(a task-independent follow-through instruction in Kukulkán's system prompt) is the leading
candidate but is not yet implemented or tested. The current standout open items are, in order:
**9/11's follow-through bug** (now the best-evidenced open item — two independent 0/5 runs),
**7's residual flake** — still recurring at roughly 1-in-5 on the A′ task, still a bare empty
response with zero tool calls attempted, still unexplained — and **8c**, a smaller but real gap
where skills that specify a procedure don't get that procedure fully followed even when they're
being read.

---

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
