---
name: estimate-before-batch
description: Use when a task asks to create, modify, or process many files
  or repeat an action multiple times — plan the step count before starting.
version: 2
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
3. If the estimate fits the budget, do NOT announce your plan or say you will
   proceed — make the first tool call immediately, in this same response, and
   continue one tool call per step until all items are done.
4. Reserve your last step for the final answer — never spend the entire
   budget on tool calls.

## Worked example — budget insufficient

Task: create part_01.txt through part_15.txt, budget 10 steps.
Estimate: ~17 steps (15 writes + list_dir + answer). 17 > 10 — does not fit.

WRONG: start writing files and run out of budget at part_10 with no warning.

RIGHT: "This task needs approximately 17 steps but the budget is 10. Options:
raise the budget, or I complete the first 8 items now and report."

## Worked example — budget sufficient

Task: create part_01.txt through part_15.txt, budget 20 steps.
Estimate: ~17 steps (15 writes + list_dir + answer). 17 <= 20 — it fits.

WRONG: "The estimate is 17 and the budget is 20. I will proceed to create the files."
(A statement of intent with no tool call ends the task immediately — nothing
gets written.)

RIGHT: call write_file for part_01.txt immediately, in this same response, and
continue one file per step until done, then give the final answer.