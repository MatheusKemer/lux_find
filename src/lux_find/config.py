"""``lux_find.toml`` — the list of places you want searchable.

The whole configuration surface is one file with N sources:

    [index]
    db = "~/.lux_find/index.sqlite"

    [[source]]
    path = "~/notes"
    kind = "notes"
    include = ["*.md", "*.txt"]
    exclude = ["archive/**"]

``kind`` selects the connector and the chunking strategy:

===========  =========================  ==========================
kind         connector                  chunking
===========  =========================  ==========================
``notes``    ``files``                  headings (markdown-aware)
``code``     ``files``                  sliding window
``chat``     ``chat-jsonl``             one chunk per message
===========  =========================  ==========================

Parsed with :mod:`tomllib` from the standard library, which is why the minimum
Python is 3.11: a TOML parser vendored here would be a second implementation of
the same file format, and two parsers eventually disagree.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Config", "Source", "load_config", "guess_kind", "render_config", "DEFAULT_DB"]

DEFAULT_DB = "~/.lux_find/index.sqlite"
CONFIG_NAME = "lux_find.toml"

NOTE_EXTS = {".md", ".mdx", ".markdown", ".rst", ".txt", ".org", ".adoc"}
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".php", ".scala",
    ".sh", ".bash", ".zsh", ".fish", ".sql", ".lua", ".pl", ".r", ".jl",
    ".vue", ".svelte", ".css", ".scss", ".html", ".toml", ".yaml", ".yml",
}
CHAT_EXTS = {".jsonl", ".json"}

DEFAULT_INCLUDE = {
    "notes": sorted("*" + e for e in NOTE_EXTS),
    # A code source also picks up its own prose: the README and the design notes
    # in a repository are usually the part you are actually searching for.
    "code": sorted("*" + e for e in (CODE_EXTS | NOTE_EXTS)),
    "chat": ["*.jsonl", "*.json"],
}

DEFAULT_EXCLUDE = [
    ".git/**", "node_modules/**", "__pycache__/**", ".venv/**", "venv/**",
    "dist/**", "build/**", "target/**", ".mypy_cache/**", ".pytest_cache/**",
    ".next/**", "vendor/**", "*.min.js", "*.lock",
]


class ConfigError(Exception):
    """Raised when the configuration cannot be read or makes no sense."""


@dataclass
class Source:
    path: str
    kind: str = "notes"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return Path(os.path.expanduser(self.path)).resolve()

    def effective_include(self) -> list[str]:
        return self.include or DEFAULT_INCLUDE.get(self.kind, DEFAULT_INCLUDE["notes"])

    def effective_exclude(self) -> list[str]:
        return list(DEFAULT_EXCLUDE) + list(self.exclude)


@dataclass
class Config:
    sources: list[Source]
    db: str = DEFAULT_DB
    stale_hours: int = 24
    path: Path | None = None

    @property
    def db_path(self) -> Path:
        """Absolute path to the index.

        A relative ``db`` resolves next to the config file, not next to whatever
        directory you happen to be standing in - otherwise the same
        ``lux_find.toml`` would point at a different index depending on where you
        ran the command from, and every error message would print a path you
        cannot act on.
        """
        path = Path(os.path.expanduser(self.db))
        if not path.is_absolute():
            base = self.path.parent if self.path else Path.cwd()
            path = base / path
        return Path(os.path.abspath(path))


# --------------------------------------------------------------------- parsing

def _parse_toml(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc


def load_config(path: str | os.PathLike[str]) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config not found: {p}")
    data = _parse_toml(p.read_text(encoding="utf-8"))

    raw_sources = data.get("source") or []
    if isinstance(raw_sources, dict):
        raw_sources = [raw_sources]
    if not raw_sources:
        raise ConfigError(f"{p}: no [[source]] entries — nothing to index")

    sources: list[Source] = []
    for entry in raw_sources:
        if "path" not in entry:
            raise ConfigError(f"{p}: a [[source]] is missing 'path'")
        kind = str(entry.get("kind", "notes"))
        if kind not in DEFAULT_INCLUDE:
            raise ConfigError(
                f"{p}: unknown kind {kind!r} (expected one of {sorted(DEFAULT_INCLUDE)})"
            )
        sources.append(
            Source(
                path=str(entry["path"]),
                kind=kind,
                include=[str(x) for x in entry.get("include", [])],
                exclude=[str(x) for x in entry.get("exclude", [])],
            )
        )

    index = data.get("index") or {}
    return Config(
        sources=sources,
        db=str(index.get("db", DEFAULT_DB)),
        stale_hours=int(index.get("stale_hours", 24)),
        path=p,
    )


# ---------------------------------------------------------------- init helpers

def guess_kind(root: Path, sample: int = 400) -> str:
    """Guess a source kind by looking at what is actually in the directory."""
    if root.is_file():
        ext = root.suffix.lower()
        if ext in CHAT_EXTS:
            return "chat"
        return "code" if ext in CODE_EXTS else "notes"

    notes = code = chat = 0
    seen = 0
    for path in root.rglob("*"):
        if seen >= sample:
            break
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {".git", "node_modules", "__pycache__", ".venv", "venv"}:
            continue
        seen += 1
        ext = path.suffix.lower()
        if ext in NOTE_EXTS:
            notes += 1
        elif ext in CODE_EXTS:
            code += 1
        elif ext in CHAT_EXTS:
            chat += 1

    if chat and chat >= max(notes, code):
        return "chat"
    if code > notes:
        return "code"
    return "notes"


def render_config(sources: list[Source], db: str = DEFAULT_DB) -> str:
    """Serialise a config back to TOML text (what ``init`` writes)."""
    lines = [
        "# lux_find configuration.",
        "# Every path listed here becomes searchable. Nothing leaves this machine.",
        "",
        "[index]",
        f'db = "{db}"',
        "# Warn when the index has not been rebuilt in this many hours.",
        "stale_hours = 24",
    ]
    for src in sources:
        lines += [
            "",
            "[[source]]",
            f'path = "{src.path}"',
            f'kind = "{src.kind}"',
        ]
        if src.include:
            lines.append("include = [" + ", ".join(f'"{x}"' for x in src.include) + "]")
        if src.exclude:
            lines.append("exclude = [" + ", ".join(f'"{x}"' for x in src.exclude) + "]")
    return "\n".join(lines) + "\n"
