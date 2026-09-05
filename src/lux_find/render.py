"""Human-facing output.

Two audiences read these results: a person at a terminal, and an agent paying
tokens per byte. The person gets grouped, coloured, highlighted output; the
agent gets ``--json``. Same data, same code path, different renderer — so a bug
in one is never invisible in the other.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import time

from .search import SearchOutcome

__all__ = ["render_human", "render_status", "shorten_path", "sanitize"]

_MIN_WRAP_WIDTH = 40
_SNIPPET_INDENT = "   "

KIND_TAG = {"notes": "note", "code": "code", "chat": "chat"}

_ANSI = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
    "reset": "\033[0m",
}


# Control characters that must never reach a terminal. Indexed content is
# written by other people - a note, a scraped page, a chat export, a filename -
# and an escape sequence in it can repaint the screen, set the window title or
# hide text. C0 (minus tab/newline), DEL and the C1 range all go.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize(text: str) -> str:
    """Strip terminal control sequences from untrusted text before printing."""
    return _CONTROL.sub("", text)


def _colour(enabled: bool):
    if enabled:
        return _ANSI
    return {k: "" for k in _ANSI}


def use_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def shorten_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _highlight(snippet: str, c: dict, colour: bool) -> str:
    if not colour:
        # Keep the literal markers when there is no colour to carry them. A
        # snippet piped into a file or a pager must still say which words
        # matched, otherwise the highlight silently disappears.
        return snippet
    return snippet.replace("<<", c["yellow"] + c["bold"]).replace(">>", c["reset"])


def _resolve_wrap_width(width: int | None) -> int | None:
    """Decide the column width to wrap snippet bodies at, or None to not wrap.

    ``width`` forces wrapping at that value (tests use this so they don't need
    a real TTY). Left at the default (``None``), wrapping only kicks in when
    stdout is a terminal; piped human output (a file, a pager, an agent) stays
    exactly as it always has, one line per hit. Either way the width never
    goes below ``_MIN_WRAP_WIDTH``: a narrower wrap would chop words into
    confetti rather than fix the wrapping problem.
    """
    if width is not None:
        return max(_MIN_WRAP_WIDTH, width)
    if not sys.stdout.isatty():
        return None
    columns = shutil.get_terminal_size((100, 24)).columns
    return max(_MIN_WRAP_WIDTH, columns)


def _wrap_snippet(snippet: str, width: int, c: dict, colour: bool) -> list[str]:
    """Wrap a snippet body to ``width`` columns at word boundaries.

    Wrapping runs on the raw ``<<``/``>>``-marked text first, then colour is
    applied per physical line — simpler than measuring visible width through
    ANSI codes, and it keeps a marker pair from ever being torn apart by the
    wrap itself (textwrap never splits inside a run of non-space characters
    when ``break_long_words`` is off). The one edge case colour has to handle
    is a highlighted phrase wide enough to straddle a line break: the state
    carries into the next physical line, and the line that opened it gets an
    explicit reset so the colour never bleeds past the line it was printed on.
    """
    flat = snippet.replace("\n", " ")
    wrapper = textwrap.TextWrapper(
        width=width,
        initial_indent=_SNIPPET_INDENT,
        subsequent_indent=_SNIPPET_INDENT,
        break_long_words=False,
        break_on_hyphens=False,
    )
    raw_lines = wrapper.wrap(flat) or [_SNIPPET_INDENT]

    if not colour:
        return raw_lines

    out = []
    active = False
    for raw in raw_lines:
        buf = [c["yellow"] + c["bold"]] if active else []
        i = 0
        while i < len(raw):
            if raw.startswith("<<", i):
                buf.append(c["yellow"] + c["bold"])
                active = True
                i += 2
            elif raw.startswith(">>", i):
                buf.append(c["reset"])
                active = False
                i += 2
            else:
                buf.append(raw[i])
                i += 1
        if active:
            buf.append(c["reset"])
        out.append("".join(buf))
    return out


def _age(mtime: float) -> str:
    if not mtime:
        return ""
    days = (time.time() - mtime) / 86400
    if days < 1:
        return "today"
    if days < 30:
        return f"{int(days)}d ago"
    if days < 365:
        return f"{int(days / 30)}mo ago"
    return f"{days / 365:.1f}y ago"


def render_human(
    outcome: SearchOutcome,
    *,
    colour: bool = True,
    why: bool = False,
    width: int | None = None,
) -> str:
    c = _colour(colour)
    wrap_width = _resolve_wrap_width(width)
    lines: list[str] = []
    collapsed_note = f" (+{outcome.collapsed} similar collapsed)" if outcome.collapsed else ""
    header = (
        f'{c["dim"]}lux_find "{sanitize(outcome.query)}" - {len(outcome.hits)} hits - '
        f'{outcome.ms:.1f} ms - confidence {outcome.confidence}{collapsed_note}{c["reset"]}'
    )
    lines.append(header)

    if not outcome.hits:
        lines.append("")
        lines.append("  no matches in the index.")
        lines.append(
            f'  {c["dim"]}try fewer or rarer words, or run `lux-find index` '
            f'if the content is new.{c["reset"]}'
        )
        return "\n".join(lines)

    for number, hit in enumerate(outcome.hits, 1):
        tag = KIND_TAG.get(hit.kind, hit.kind)
        location = sanitize(f"{shorten_path(hit.path)}:{hit.line}")
        age = _age(hit.mtime)
        lines.append("")
        lines.append(
            f'{c["bold"]}{number}.{c["reset"]} {c["cyan"]}{location}{c["reset"]}  '
            f'{c["dim"]}[{tag}]{(" - " + age) if age else ""}{c["reset"]}'
        )
        if wrap_width is None:
            body = _highlight(sanitize(hit.snippet), c, colour).replace("\n", " ")
            lines.append(f"{_SNIPPET_INDENT}{body}")
        else:
            lines.extend(_wrap_snippet(sanitize(hit.snippet), wrap_width, c, colour))
        if why:
            parts = [f"bm25={hit.bm25:.3f}"] + [f"{k}=+{v:.3f}" for k, v in hit.boosts.items()]
            lines.append(f'   {c["dim"]}score {hit.score:.3f} = ' + " ".join(parts) + c["reset"])

    return "\n".join(lines)


def render_status(info: dict, *, stale_hours: int, colour: bool = True) -> str:
    c = _colour(colour)
    age = info["age_hours"]
    if age is None:
        freshness = f'{c["red"]}never built{c["reset"]}'
    elif age > stale_hours:
        freshness = f'{c["red"]}STALE - {age:.1f} h old{c["reset"]}'
    else:
        freshness = f'{c["green"]}fresh - {age:.1f} h old{c["reset"]}'

    built = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(info["built_at"]))
        if info["built_at"]
        else "-"
    )
    lines = [
        f'{c["bold"]}lux_find index{c["reset"]}  {shorten_path(info["db"])}',
        f'  documents   {info["docs"]:,}'
        + (f' ({info["duplicates"]:,} duplicate copies collapsed)' if info["duplicates"] else ""),
        f'  chunks      {info["chunks"]:,}',
        f'  size        {info["db_mb"]} MB',
        f'  built at    {built}  ({freshness})',
    ]
    if info["by_kind"]:
        spread = "  ".join(f"{k}={v:,}" for k, v in info["by_kind"].items())
        lines.append(f"  by kind     {spread}")
    for root in info["sources"]:
        lines.append(f'  source      {sanitize(shorten_path(root))}')
    return "\n".join(lines)
