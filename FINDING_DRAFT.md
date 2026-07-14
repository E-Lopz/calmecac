# Finding draft: title/link mismatch across Discord turns (2026-07-14, ~19:13-19:14 local / 23:13-23:14 UTC)

Diagnostic only — not added to NOTES.md, not numbered, not implemented.

## Symptom

- Turn 1 (`logs/run_20260714T231343Z.jsonl`): user asked "extract the first paper
  titles in arxiv of attention mechanisms." Agent answered **"Visual Attention Network."**
- Turn 2 (`logs/run_20260714T231421Z.jsonl`, 38s later, same Discord channel):
  user asked "can you get me the link." Agent answered with a link for
  **"Invariant Learning Dynamics of Transformers in Inductive Reasoning Tasks,"**
  `https://arxiv.org/abs/2607.11875v1` — a different paper than turn 1.

## Evidence, in order

### 1. Turn 1 — fetch_url call and response

Exactly one `fetch_url` call:

```
"url": "https://export.arxiv.org/api/query?search_query=attention&start=0&max_results=1"
```

No `sortBy`/`sortOrder` params (arXiv API default sort applies). The raw fetched response's
only/first `<entry>`:

```
<id>http://arxiv.org/abs/2202.09741v5</id>
<title>Visual Attention Network</title>
<link href="https://arxiv.org/abs/2202.09741v5" rel="alternate" type="text/html"/>
```

Yes — "Visual Attention Network" **is** the first (and only, since `max_results=1`) entry
in the fetched response. Turn 1's answer is correct and grounded in what was fetched.

### 2. Turn 2 — fetch_url call and response

Not zero — **one** `fetch_url` call:

```
"url": "https://export.arxiv.org/api/query?search_query=attention&start=0&max_results=1&sortBy=lastUpdatedDate&sortOrder=descending"
```

This is **not identical** to turn 1's query — it adds `sortBy=lastUpdatedDate&sortOrder=descending`,
which the model was never told to use in either turn's history (history is final-answers-only;
see §3) and appears to be invented on the spot when re-deriving "the link" without the original
tool result available. Sorting by most-recently-updated instead of arXiv's default (relevance)
returns a completely different top hit:

```
<id>http://arxiv.org/abs/2607.11875v1</id>
<title>Invariant Learning Dynamics of Transformers in Inductive Reasoning Tasks</title>
<link href="https://arxiv.org/abs/2607.11875v1" rel="alternate" type="text/html"/>
```

The agent's turn-2 answer ("Invariant Learning Dynamics of Transformers in Inductive Reasoning
Tasks," `https://arxiv.org/abs/2607.11875v1`) matches this single entry's title+id pair exactly.
**Not** a binding error — title and link both come from the same entry in the same response.

### 3. Conversation history sent to the model at turn 2

From `run_start.messages` in `run_20260714T231421Z.jsonl` (`history_len: 2`, `source: "discord"`):

```
0 system   <Kukulkán system prompt + standing memory>
1 user     "can you extract the first  paper titles in arxiv of attention mechanisms"
2 assistant "The first paper title related to attention mechanisms from arXiv is:
             **\"Visual Attention Network\"**."
3 user     "can you get me the link"
```

Confirmed: history is final-answers-only, as designed in Phase 6a. Turn 1's tool call
(`fetch_url` with its specific query string) and its raw XML result are **not** present
anywhere in turn 2's context — only the prose final answer survives. The model had no way
to know what URL turn 1 used or what the fetched entry actually contained; it had to
re-derive both from the words "Visual Attention Network" and "the link."

### 4. Sanity check on arXiv ID 2607.11875

No additional live fetch was needed — turn 2's own tool result (§2) already contains full
arXiv metadata for this ID, retrieved live from `export.arxiv.org` moments before this
diagnosis: it is a real, currently-existing paper — "Invariant Learning Dynamics of
Transformers in Inductive Reasoning Tasks" (Musat, Pimentel, Zucchet, Hofmann),
published 2026-07-13, categories cs.LG/cs.AI. **Not a fabricated ID.**

## Verdict

**B — non-deterministic/different query, different result sets.**

Not A: turn 2 did call `fetch_url`; the link is grounded in a real, live-fetched result,
not invented from nothing.

Not C: within turn 2's single response, the title and link the agent reported both came
from the same `<entry>` — no cross-entry binding error.

The root cause is the combination of (a) history dropping tool results after each turn
(by design, Phase 6a) and (b) the model, lacking any record of turn 1's exact query,
improvising a *plausible but different* one (adding a recency sort) to satisfy "get me
the link" — which the arXiv API happily serves, silently returning an unrelated top hit.
The model never noticed anything was wrong because nothing in its context contradicted
the new result; it has no way to know its self-authored query at turn 2 differs from the
one that produced the title it's now trying to attach a link to.

## Candidate fixes (not implemented — trade-offs only)

**Fix A — keep last turn's tool call + a capped result digest in per-channel history,
not just the final answer.**
- Pro: directly closes the gap that caused this — turn 2 could either reuse the exact
  prior URL or at minimum see that a *different* query changes the result, and could be
  prompted to notice the title mismatch.
- Con: raw arXiv Atom responses are large (~3KB for `max_results=1`, scales with N);
  raises the token/context budget per exchange for every tool-using turn, not just
  arXiv ones. Needs a digest/truncation strategy (Phase 6a's existing ~2000-token cap
  logic would need to account for tool payloads, not just prose) — and still doesn't
  guarantee the model treats "get me the link" as "reuse the same query" rather than
  "issue a fresh, possibly different query."

**Fix B — skill/prompting rule pinning `sortBy`/deterministic query construction for
arXiv (and general REST) queries, e.g. "always sort by relevance unless asked
otherwise; never add params not already present in a prior query for the same topic
within a conversation."**
- Pro: cheap, no data-volume cost, addresses this specific failure mode (arbitrary
  sort/param drift across turns) directly.
- Con: doesn't fully solve the underlying problem — the model still has no memory of
  the *exact* prior query or result, so a differently-phrased follow-up ("what's it
  about," "who wrote it") could still trigger a fresh, divergent fetch that happens to
  agree on params but land on a different day's "most relevant" result if arXiv's
  index changes between turns. Treats a symptom of the missing-history gap, not the
  gap itself.

Fix C (structured Atom→`{title, id, url}` parsing before the model sees entries) was
considered but doesn't apply here: turn 2's title/link pairing was already internally
consistent (§2) — the mismatch is *across* turns, not within a single response's raw
text, so pre-parsing doesn't address this failure mode. It would still be good hygiene
for the planned lit-review pipeline but isn't the fix for this bug.
