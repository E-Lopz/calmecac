"""Tool implementations available to the agent loop.

Every tool is restricted to reading/writing inside workspace/, so the
agent can't touch the rest of the filesystem.
"""

from pathlib import Path

WORKSPACE = (Path(__file__).resolve().parent.parent / "workspace").resolve()
WORKSPACE.mkdir(exist_ok=True)


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
