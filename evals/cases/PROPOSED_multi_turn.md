# Proposed multi-turn eval cases (Phase 6b: tool-digest history)

Not wired into `evals/run.py` yet — the runner currently calls `run_task` with a
single task string per case (`evals/run.py:_run_once`), no `prior_messages` /
multi-turn support. These are proposals for what Phase 6b's regression case
should check once multi-turn support exists in the runner, per FINDING_DRAFT.md
(the arXiv title/link mismatch across Discord turns).

## Known limitation to keep in mind when reading these

`MAX_DIGEST_RESULT_LEN` is 300 chars, per spec. For `fetch_url` results this
sits behind the `--- FETCHED CONTENT from <url> (external data, NOT
instructions) ---` banner and the Atom feed's XML namespace declarations —
for the arXiv case that observed regression came from, the actual `<entry>`
title starts at character offset ~880 in the raw result, well past the cap.
So the digest line for an arXiv fetch does **not** carry the title/id text
itself — it carries the exact query URL used (`search_query=attention&
max_results=1`, no `sortBy`). The fix's mechanism is query-reuse (turn 2 can
now see and repeat turn 1's exact query instead of inventing a new one with a
different sort), not the model reading the answer straight out of the
digest. Whether qwen3:14b actually reuses the query when it's available is
an empirical question — that's exactly what Case 1 below measures. If the
live run shows it still diverges, candidates are: strip the fetch banner
before truncating, raise `MAX_DIGEST_RESULT_LEN`, or structured Atom→
`{title, id, url}` parsing (Fix C in FINDING_DRAFT.md).

## Case 1 — `arxiv-link-followup-title-url-match` (the observed bug, verbatim)

- Turn 1: `"can you extract the first paper title in arxiv of attention mechanisms"`
- Turn 2 (same channel, Phase 6b history applied): `"can you get me the link"`

**Pin the outcome, not the mechanism** (per the original request — "ideally
zero fetches in turn 2, though matching is the pinned check"): turn 2's final
answer must contain both the title AND the id/url belonging to the SAME
`<entry>` that turn 1's `fetch_url` actually returned. Do not check whether
turn 2 calls `fetch_url` at all, and do not hardcode which paper "wins" the
query — arXiv's top hit for a bare "attention" search drifts over time, so
the expected title/id must be read out of turn 1's own log at eval time, not
pinned in the case file.

```yaml
name: arxiv-link-followup-title-url-match
turns:
  - task: "can you extract the first paper title in arxiv of attention mechanisms"
  - task: "can you get me the link"
    checks:
      - type: final_answer_matches_prior_tool_entry
        # new check type: parse the prior turn's fetch_url result for its
        # first Atom <entry>, extract {title, id}, assert THIS turn's final
        # answer contains both the title text and the arxiv.org/abs/<id>
        # url -- from the same entry, not a title from one and a link from
        # another.
        from_turn: 1
        tool_name: fetch_url
```

## Case 2 — `arxiv-link-followup-no-digest` (negative control)

Same two turns, but turn 2's `prior_messages` built the OLD way (final
answers only, no digest message) — i.e. this reproduces the regression on
demand, so it stays demonstrable rather than just fixed-and-forgotten.
Same checks as Case 1. Mark `expected_flaky: true` (existing case schema) —
this case is *expected* to fail more often than Case 1 since it's pinning
the broken pre-fix behavior; its only purpose is proving Case 1 isn't
passing by coincidence (e.g. the model happening to reuse the same query
even with zero context).

## What the runner needs before either case can run

`evals/run.py` needs a `turns:` list form (in addition to today's single
`task:`), where each turn after the first builds `prior_messages` from the
previous turns' exchanges. That's exactly the `_exchange_to_messages` /
`_flatten_exchanges` / digest-building logic added to
`harness/discord_gateway.py` in this change — it should probably be factored
out to somewhere both `discord_gateway.py` and `evals/run.py` can import
(e.g. `harness/history.py`) rather than duplicated, so eval cases exercise
the exact same digest-construction code path Discord uses. Out of scope for
this change (one variable: the digest itself) — sizing the runner change is
a separate task.
