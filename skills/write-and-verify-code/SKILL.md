---
name: write-and-verify-code
description: Use when writing any file containing code, quotes, escapes,
  apostrophes, docstrings, or multi-line strings — prevents delimiter
  collisions and escaping errors.
version: 1
---

# Write and Verify Code

Use this whenever a write_file call will contain source code or any text
with quotes, backslashes, or multi-line strings.

## Procedure

1. Before writing, choose your outer string delimiter and state it
   explicitly: "I will use double quotes as the outer delimiter."
2. Scan the content for every character that collides with that choice:
   - single-quoted outer: apostrophes in prose ("it's", "don't") collide
   - double-quoted outer: quoted speech collides
   - triple-quoted outer: any """ inside the content collides
3. If there are collisions, either escape each one or switch delimiters
   so the fewest escapes are needed.
4. After write_file succeeds, read_file the result and check: does every
   opening delimiter have a matching close? Is the last line complete
   (not truncated)?
5. Only report success after step 4 passes. If it fails, fix and repeat.

## Worked example (real failure)

Task: write MESSAGE containing: He said, "it's a "nested" quote," then paused.

WRONG (what happened): chose single-quoted outer, escaped the double
quotes, missed the apostrophe in "it's":
    MESSAGE = 'He said, "it's a "nested" quote," then paused.'
    → SyntaxError: unterminated string literal

RIGHT: the apostrophe collides with single quotes, so use double-quoted
outer and escape the inner double quotes instead:
    MESSAGE = "He said, \"it's a \"nested\" quote,\" then paused."

## Known failure modes

- Apostrophes in ordinary prose are the most-missed collision.
- Nesting """ inside a """-delimited string always breaks.
- Truncating your own output mid-string: step 4's read-back catches this.