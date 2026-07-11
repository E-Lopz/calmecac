"""Tool implementations available to the agent loop.

Every tool is restricted to reading/writing inside workspace/, so the
agent can't touch the rest of the filesystem. Skills (skills/) are
readable through the same kind of containment check but never writable.
"""

from pathlib import Path

import yaml

WORKSPACE = (Path(__file__).resolve().parent.parent / "workspace").resolve()
WORKSPACE.mkdir(exist_ok=True)

SKILLS_DIR = (Path(__file__).resolve().parent.parent / "skills").resolve()


def _resolve(path: str) -> Path:
    """Resolve a path inside WORKSPACE, refusing anything that escapes it."""
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError(f"path '{path}' must be relative to the workspace directory")

    resolved = (WORKSPACE / candidate).resolve()
    if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
        raise ValueError(f"path '{path}' escapes the workspace directory")
    return resolved


def read_file(path: str) -> str:
    return _resolve(path).read_text()


def write_file(path: str, content: str) -> str:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} bytes to {path}"


def list_dir(path: str) -> str:
    target = _resolve(path)
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


def _make_skill_func(body):
    """A no-argument tool callable that just returns one skill's pre-read body."""
    def run():
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
            _make_skill_func(body),
        )
    return entries


REGISTRY = {
    "read_file": (
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the contents of a text file inside the workspace directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace directory.",
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
                "description": "List the entries of a directory inside the workspace directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path relative to the workspace directory. Use '.' for the workspace root.",
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
