"""Minimal client for Ollama's native /api/chat endpoint."""

from pathlib import Path

import httpx
import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

with open(_CONFIG_PATH) as f:
    _config = yaml.safe_load(f)


def chat(messages, tools=None):
    """Send a chat request to Ollama and return the parsed JSON response."""
    payload = {
        "model": _config["model"],
        "messages": messages,
        "stream": False,
        "think": _config["think"],
        "options": {
            "temperature": _config["temperature"],
            "num_ctx": _config["num_ctx"],
        },
    }
    if tools is not None:
        payload["tools"] = tools

    response = httpx.post(
        f"{_config['base_url']}/api/chat",
        json=payload,
        timeout=_config["timeout"],
    )
    response.raise_for_status()
    return response.json()
