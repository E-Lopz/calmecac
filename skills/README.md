# Skills

A skill is a directory containing one file:

```
skills/<skill-name>/SKILL.md
```

`SKILL.md` is YAML frontmatter followed by markdown instructions:

```
---
name: <skill-name>
description: <one-line description>
version: 1
---

<markdown instructions the agent reads once it loads this skill>
```

- `name` should match the directory name.
- `description` is what the agent sees in the system prompt's skill index before
  loading anything — keep it to one line, specific enough to tell when the skill
  applies.
- `version` is currently just `1`; bump it when the instructions change meaningfully.

At loop start, `harness/loop.py` scans this directory, parses only the frontmatter
of each `SKILL.md`, and lists name + description in the system prompt. The model
reads the full body on demand via the `load_skill` tool. A skill with malformed
frontmatter is skipped with a printed warning, not a crash.

No scripts, no bundled resources, no other files per skill. This directory is
read-only from the tool's perspective — there is no `write_skill`.
