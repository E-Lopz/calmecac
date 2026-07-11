---
name: estimate-before-batch
description: Use when a task asks to create, modify, or process many files
  or repeat an action multiple times — plan the step count before starting.
version: 1
---

# Estimate Before Batch

Use this when a task involves doing something N times (many files, many
items, repeated operations).

## Procedure

1. Before the first tool call, count how many tool calls the task needs.
   Rule of thumb: N items = N calls, plus 1-2 for verification and the
   final answer.
2. Compare against your step budget. If the estimate exceeds it, do NOT
   start. Instead, report immediately: "This task needs approximately X
   steps but the budget is Y. Options: raise the budget, or I complete
   the first Y-2 items now."
3. If the estimate fits, proceed, and track your count as you go.
4. Reserve your last step for the final answer — never spend the entire
   budget on tool calls.

## Worked example (real failure)

Task: create part_01.txt through part_15.txt, budget 10 steps.

WRONG (what happened): started writing immediately, produced 10 of 15
files, aborted at the budget with no warning and no summary of what was
left undone.

RIGHT: "This needs ~17 steps (15 writes + list_dir + answer) but the
budget is 10. Should I proceed with the first 8 and report, or should
the budget be raised?"