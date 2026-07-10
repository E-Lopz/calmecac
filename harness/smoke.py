"""Smoke test for the Ollama client.

Run with: python -m harness.smoke
"""

from harness.ollama_client import chat


def main():
    response = chat([{"role": "user", "content": "say hello"}])
    print(response["message"]["content"])


if __name__ == "__main__":
    main()
