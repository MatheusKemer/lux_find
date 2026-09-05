"""``chat-jsonl`` connector — conversation exports.

Three shapes are understood, all of them things you already have on disk:

* **Agent transcripts** (``*.jsonl``): one JSON object per line, each with a
  ``message`` holding ``role`` and ``content``. ``content`` may be a string or a
  list of typed blocks; text and reasoning blocks are kept, tool payloads are
  summarised to their name and a short argument preview.
* **ChatGPT data export** (``conversations.json``): a JSON array of
  conversations, each with a ``title`` and a ``mapping`` of message nodes. One
  conversation becomes one document.
* **Generic JSONL**: any line with a ``text``, ``content``, ``body`` or
  ``message`` field is indexed as a message.

For chat documents the "line number" in a result is the **message number**, not
a file line — a transcript is a list of turns, and that is the coordinate a
human can actually use to find the moment again.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Iterator

from .base import Connector, DocRef, ScanReport

__all__ = ["ChatConnector"]

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000
TOOL_PREVIEW_CHARS = 300


def _blocks_to_text(content) -> str:
    """Normalise a message ``content`` field to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # ChatGPT: {"content_type": "text", "parts": [...]}
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(str(p) for p in parts if isinstance(p, (str, int, float)))
        return _blocks_to_text(content.get("text") or "")
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            out.append(str(block["text"]))
        elif btype == "thinking" and block.get("thinking"):
            out.append(str(block["thinking"]))
        elif btype == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False)[:TOOL_PREVIEW_CHARS]
            out.append(f"[tool {block.get('name', '?')}] {args}")
        elif btype == "tool_result":
            body = block.get("content")
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            out.append("[result] " + body[:TOOL_PREVIEW_CHARS])
    return "\n".join(x for x in out if x)


def _role_of(obj) -> str:
    for getter in (
        lambda o: (o.get("message") or {}).get("role"),
        lambda o: o.get("role"),
        lambda o: ((o.get("message") or {}).get("author") or {}).get("role"),
        lambda o: (o.get("author") or {}).get("role"),
        lambda o: o.get("type"),
    ):
        try:
            value = getter(obj)
        except AttributeError:
            continue
        if isinstance(value, str) and value:
            return value
    return "?"


def _text_of(obj) -> str:
    message = obj.get("message") if isinstance(obj, dict) else None
    if isinstance(message, dict) and "content" in message:
        return _blocks_to_text(message["content"])
    for key in ("content", "text", "body", "message"):
        if key in obj:
            text = _blocks_to_text(obj[key])
            if text:
                return text
    return ""


def _messages_from_jsonl(path: Path, report: "ScanReport | None" = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    bad_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw or raw[0] != "{":
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                # One truncated line is normal in a transcript that was still
                # being written. It is counted, not fatal, and never invisible.
                bad_lines += 1
                continue
            if not isinstance(obj, dict):
                continue
            text = _text_of(obj)
            if text:
                out.append((_role_of(obj), text))
    if bad_lines and report is not None:
        report.skip("unparsable-jsonl-line")
    return out


_PARSE_CACHE: dict[str, tuple[tuple, list]] = {}
_PARSE_CACHE_SIZE = 4


def _conversations_from_json(path: Path, report: "ScanReport | None" = None) -> list:
    """Return ``(inner_id, title, messages)`` from a ChatGPT-style export.

    The result is cached per (path, mtime, size). One export file holds every
    conversation, and both the scan and the load of each conversation ask for
    it - without the cache a 400-conversation export is parsed 400 times, which
    turns a first run into minutes of nothing happening.
    """
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        if report is not None:
            report.fail(f"{path}: cannot stat: {exc}")
        return []

    key = str(path)
    cached = _PARSE_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        # Not a silent skip: a file that matched the include pattern and could
        # not be parsed is something the operator has to know about.
        if report is not None:
            report.fail(f"{path}: cannot parse as JSON: {exc}")
        return []

    out = list(_walk_conversations(data))
    if len(_PARSE_CACHE) >= _PARSE_CACHE_SIZE:
        _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
    _PARSE_CACHE[key] = (stamp, out)
    return out


def _walk_conversations(data):
    conversations = data if isinstance(data, list) else [data]
    for index, conv in enumerate(conversations):
        if not isinstance(conv, dict):
            continue
        title = str(conv.get("title") or f"conversation {index + 1}")
        messages: list[tuple[str, str]] = []
        mapping = conv.get("mapping")
        if isinstance(mapping, dict):
            nodes = sorted(
                (n for n in mapping.values() if isinstance(n, dict) and n.get("message")),
                key=lambda n: (n["message"].get("create_time") or 0),
            )
            for node in nodes:
                message = node["message"]
                text = _blocks_to_text(message.get("content"))
                if text.strip():
                    messages.append((_role_of(message) or "?", text))
        elif isinstance(conv.get("messages"), list):
            for message in conv["messages"]:
                if isinstance(message, dict):
                    text = _text_of(message)
                    if text.strip():
                        messages.append((_role_of(message), text))
        if messages:
            yield str(conv.get("id") or index), title, messages


class ChatConnector(Connector):
    name = "chat-jsonl"

    def scan(self, source, report: ScanReport | None = None) -> Iterator[DocRef]:
        from .files import _matches, looks_like_credential_file

        root = source.root
        report = report if report is not None else ScanReport()
        if not root.exists():
            raise FileNotFoundError(f"source path does not exist: {root}")

        include = source.effective_include()
        exclude = source.effective_exclude()
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))

        for path in candidates:
            if not path.is_file():
                continue
            rel = path.name if path == root else str(path.relative_to(root))
            if _matches(rel, exclude) or not _matches(rel, include):
                continue
            # Same rule as the files connector: a file whose *name* says it
            # exists to hold a credential is never opened. An export folder
            # is exactly where a stray `.env.json` ends up.
            if looks_like_credential_file(path):
                report.skip("credential-named")
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                report.fail(f"{path}: cannot stat: {exc}")
                continue
            if stat.st_size == 0:
                report.skip("empty")
                continue
            if stat.st_size > MAX_FILE_BYTES:
                report.skip("over-size-limit")
                continue
            signature = f"{stat.st_mtime_ns}:{stat.st_size}"

            if path.suffix.lower() == ".json":
                for inner, title, _messages in _conversations_from_json(path, report):
                    yield DocRef(
                        uri=f"{path}#{inner}",
                        source_path=str(path),
                        title=title,
                        kind="chat",
                        signature=signature,
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        chunker="chat",
                        extra={"inner": inner},
                    )
            else:
                yield DocRef(
                    uri=str(path),
                    source_path=str(path),
                    title=rel,
                    kind="chat",
                    signature=signature,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    chunker="chat",
                )

    def load(self, ref: DocRef) -> list[tuple[int, str]]:
        path = Path(ref.source_path)
        inner = ref.extra.get("inner")
        if inner is not None:
            messages: list[tuple[str, str]] = []
            for found, _title, msgs in _conversations_from_json(path):
                if found == inner:
                    messages = msgs
                    break
        else:
            messages = _messages_from_jsonl(path)

        return [
            (number, unicodedata.normalize("NFC", f"{role}: {text[:MAX_MESSAGE_CHARS]}"))
            for number, (role, text) in enumerate(messages, 1)
            if text.strip()
        ]
