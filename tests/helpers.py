"""Shared fixtures: a tiny synthetic corpus built at test time.

Nothing is committed as a fixture file on purpose - the corpus is generated, so
the tests carry no sample data that could ever drift into real content.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lux_find.config import Config, Source  # noqa: E402

NOTES = {
    "index.md": "# Home\n\nStart here. Links to the sync design and the retro.\n",
    "design/sync-protocol.md": (
        "# Sync protocol\n\n"
        "The client sends a vector clock and the server replies with a delta.\n\n"
        "## Conflict resolution\n\n"
        "Last writer wins, but we keep the loser under a tombstone so nothing is\n"
        "silently destroyed. Tombstones expire after ninety days.\n\n"
        "## Backpressure\n\n"
        "When the queue is deeper than a thousand envelopes the client slows down.\n"
    ),
    "design/storage.md": (
        "# Storage layout\n\n"
        "Documents live in a single append-only log with an index sidecar.\n"
        "Compaction runs nightly and rewrites the log without the tombstones.\n"
    ),
    "meetings/2031-04-12-retro.md": (
        "# Retro, April\n\n"
        "We agreed the sync protocol needs backpressure before the pilot.\n"
        "Action: write the tombstone expiry down somewhere findable.\n"
    ),
    "recipes/bread.md": (
        "# Sourdough\n\n"
        "Ferment overnight. The starter needs feeding twice a day in summer.\n"
    ),
}

CODE = {
    "app/queue.py": (
        "import time\n\n\n"
        "class Backpressure:\n"
        '    """Slow the producer when the queue gets deep."""\n\n'
        "    def __init__(self, limit=1000):\n"
        "        self.limit = limit\n\n"
        "    def should_pause(self, depth):\n"
        "        return depth > self.limit\n"
    ),
    "app/tombstone.py": (
        "EXPIRY_DAYS = 90\n\n\n"
        "def is_expired(age_days):\n"
        "    return age_days > EXPIRY_DAYS\n"
    ),
    "README.md": "# app\n\nA toy service that demonstrates the sync protocol.\n",
}

# Fake credentials are assembled at runtime instead of written as literals, so
# this source file itself never contains anything shaped like a real key.
FAKE_AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
FAKE_GH_TOKEN = "ghp" + "_" + ("0123456789abcdefghij" * 2)
FAKE_PASSWORD = "correct-horse-battery-staple"

SECRETS_NOTE = (
    "# Deploy notes\n\n"
    "Old runbook, kept for history.\n\n"
    f"    export AWS_ACCESS_KEY_ID={FAKE_AWS_KEY}\n"
    f"    password = {FAKE_PASSWORD}\n"
    f"    github token: {FAKE_GH_TOKEN}\n"
)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def write_chat_exports(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    transcript = [
        {"type": "user", "message": {"role": "user",
         "content": "how do we handle a conflict during sync?"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text",
             "text": "Last writer wins, with a tombstone kept for ninety days."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file": "design/sync-protocol.md"}}]}},
    ]
    (root / "session.jsonl").write_text(
        "\n".join(json.dumps(x) for x in transcript) + "\n", encoding="utf-8"
    )

    export = [
        {
            "id": "conv-1",
            "title": "Choosing a queue",
            "mapping": {
                "a": {"message": {"author": {"role": "user"}, "create_time": 1,
                                  "content": {"content_type": "text",
                                              "parts": ["should we use a log or a queue?"]}}},
                "b": {"message": {"author": {"role": "assistant"}, "create_time": 2,
                                  "content": {"content_type": "text",
                                              "parts": ["An append-only log, with backpressure."]}}},
            },
        }
    ]
    (root / "conversations.json").write_text(json.dumps(export), encoding="utf-8")


def make_corpus(base: Path) -> Config:
    """Create notes + code + chat under ``base`` and return a matching config."""
    write_tree(base / "notes", NOTES)
    (base / "notes" / "deploy.md").write_text(SECRETS_NOTE, encoding="utf-8")
    write_tree(base / "project", CODE)
    write_chat_exports(base / "chats")
    return Config(
        sources=[
            Source(path=str(base / "notes"), kind="notes"),
            Source(path=str(base / "project"), kind="code"),
            Source(path=str(base / "chats"), kind="chat"),
        ],
        db=str(base / "index.sqlite"),
    )


class TempCorpus:
    """Context manager wrapping :func:`make_corpus` in a temporary directory."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.config = make_corpus(self.base)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False
