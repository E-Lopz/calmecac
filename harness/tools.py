"""Tool implementations available to the agent loop.

write_file is restricted to workspace/, so the agent can only ever modify
files there. read_file and list_dir are allowed anywhere under
EXPERIMENTS_ROOT (the directory containing all of the user's projects,
including this one) so the agent can read and describe sibling projects —
but never write to them. Skills (skills/) are readable through the same
kind of containment check as workspace/, but never writable.
"""

from pathlib import Path

import yaml

WORKSPACE = (Path(__file__).resolve().parent.parent / "workspace").resolve()
WORKSPACE.mkdir(exist_ok=True)

EXPERIMENTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent

SKILLS_DIR = (Path(__file__).resolve().parent.parent / "skills").resolve()


def _resolve_within(base: Path, label: str, path: str) -> Path:
    """Resolve `path` inside `base`, refusing anything that escapes it."""
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError(f"path '{path}' must be relative to the {label} directory")

    resolved = (base / candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"path '{path}' escapes the {label} directory")
    return resolved


def _resolve(path: str) -> Path:
    """Resolve a path inside WORKSPACE — used for writes."""
    return _resolve_within(WORKSPACE, "workspace", path)


def _resolve_read(path: str) -> Path:
    """Resolve a path inside EXPERIMENTS_ROOT — used for reads/listing."""
    return _resolve_within(EXPERIMENTS_ROOT, "experiments", path)


def read_file(path: str) -> str:
    return _resolve_read(path).read_text()


def write_file(path: str, content: str) -> str:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


def list_dir(path: str) -> str:
    target = _resolve_read(path)
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return "\n".join(entries) if entries else "(empty)"


def _parse_skill(path: Path):
    """Parse a SKILL.md file into (frontmatter dict, body). Raises ValueError
    if the frontmatter delimiters are missing or required keys are absent."""
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError("missing opening '---' frontmatter delimiter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("missing closing '---' frontmatter delimiter")

    frontmatter = yaml.safe_load(text[4:end])
    body = text[end + 4:].lstrip("\n")
    if not isinstance(frontmatter, dict) or "name" not in frontmatter or "description" not in frontmatter:
        raise ValueError("frontmatter must be a YAML mapping with 'name' and 'description'")
    return frontmatter, body


def _make_skill_func(name, body):
    """A tool callable that returns one skill's pre-read body. If called with
    arguments (skills take none), prefixes the body with a notice that the
    arguments were ignored and no action was taken, so the model doesn't
    mistake "got a response back" for "the action happened"."""
    def run(**kwargs):
        if kwargs:
            return (
                f"NOTE: '{name}' is an instructions-only tool. Your arguments were "
                "IGNORED — no file was written and no action was taken. Below are "
                "the instructions you requested. After reading them, perform the "
                "actual task using the real tools (write_file, read_file, list_dir)."
                f"\n\n---\n\n{body}"
            )
        return body
    return run


def _build_skill_tools():
    """Scan SKILLS_DIR and build a {name: (schema, callable)} entry for each
    valid skill — the skill itself becomes a tool, no arguments, that returns
    its SKILL.md body. Malformed skills are skipped with a warning, never
    raised — this runs at import time and must not crash the module."""
    entries = {}
    if not SKILLS_DIR.is_dir():
        return entries

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            frontmatter, body = _parse_skill(skill_file)
        except (ValueError, yaml.YAMLError) as e:
            print(f"warning: skipping malformed skill '{skill_dir.name}': {e}")
            continue

        name = frontmatter["name"]
        entries[name] = (
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": frontmatter["description"],
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            _make_skill_func(name, body),
        )
    return entries


REGISTRY = {
    "read_file": (
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read the contents of a text file. Read-only access to the "
                    "entire experiments directory (the parent directory holding "
                    "all of the user's projects, including this one under "
                    "perso/calmecac/), not just this project's workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the experiments directory, e.g. "
                                "'perso/calmecac/workspace/notes.txt' or "
                                "'perso/some-other-project/README.md'."
                            ),
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        read_file,
    ),
    "write_file": (
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write text content to a file inside the workspace directory, creating or overwriting it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace directory.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Text content to write to the file.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
        write_file,
    ),
    "list_dir": (
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": (
                    "List the entries of a directory. Read-only access to the "
                    "entire experiments directory (the parent directory holding "
                    "all of the user's projects, including this one under "
                    "perso/calmecac/), not just this project's workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path relative to the experiments directory. Use "
                                "'.' for the experiments root, or e.g. 'perso' to "
                                "see all projects."
                            ),
                        }
                    },
                    "required": ["path"],
                },
            },
        },
        list_dir,
    ),
}

_SKILL_TOOLS = _build_skill_tools()
SKILL_NAMES = frozenset(_SKILL_TOOLS)
REGISTRY.update(_SKILL_TOOLS)
