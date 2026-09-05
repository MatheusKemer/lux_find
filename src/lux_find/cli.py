"""``lux-find`` command line.

Exit codes are part of the contract, because the main consumer of this tool is a
script or an agent, not a person:

===  ==========================================================================
0    at least one hit (or the command succeeded)
1    the query ran and matched nothing
2    usage error: bad flags, missing config, unreadable source, or the index
     is locked by another run (transient - the index itself is fine)
3    the index is missing or corrupt - the database path is printed
===  ==========================================================================

A distinct code for "index is broken" matters more than it looks. An empty
result list reads as a valid verdict; if a broken index also returns zero, every
caller silently concludes "there is nothing about that" and moves on.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .config import (
    CONFIG_NAME, DEFAULT_DB, Config, ConfigError, Source, guess_kind,
    load_config, render_config,
)
from .render import render_human, render_status, use_colour
from .search import find as run_search
from .store import (
    IndexBusy, IndexCorrupt, IndexMissing, PruneGuardTripped, LuxFindError, build,
    force_prune, open_db, status,
)

EXIT_OK = 0
EXIT_NO_HITS = 1
EXIT_USAGE = 2
EXIT_INDEX = 3


def _err(message: str) -> None:
    print(f"lux-find: {message}", file=sys.stderr)


def _config_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    out = []
    env = os.environ.get("LUX_FIND_CONFIG")
    if env:
        out.append(Path(env))
    out.append(Path.cwd() / CONFIG_NAME)
    out.append(Path(os.path.expanduser("~/.lux_find")) / CONFIG_NAME)
    return out


def _load(args, *, required: bool) -> Config:
    """Resolve the configuration; ``--db`` always wins over the file."""
    for candidate in _config_candidates(getattr(args, "config", None)):
        if candidate.exists():
            config = load_config(candidate)
            if getattr(args, "db", None):
                # A --db from the command line is relative to where you are
                # standing; a db written in the config is relative to the config.
                config.db = os.path.abspath(os.path.expanduser(args.db))
            return config
    if required:
        raise ConfigError(
            f"no {CONFIG_NAME} found (looked in $LUX_FIND_CONFIG, ./, ~/.lux_find/). "
            "Run `lux-find init <path> [<path> ...]` first."
        )
    return Config(sources=[], db=getattr(args, "db", None) or DEFAULT_DB)


# ------------------------------------------------------------------------ init

def cmd_init(args) -> int:
    target = Path(args.config) if args.config else Path.cwd() / CONFIG_NAME
    if target.exists() and not args.force:
        _err(f"{target} already exists (use --force to overwrite)")
        return EXIT_USAGE

    sources: list[Source] = []
    for raw in args.paths:
        # Absolute, always: a config holding "./notes" would silently point at a
        # different directory the first time you run `index` from elsewhere.
        root = Path(os.path.expanduser(raw)).resolve()
        if not root.exists():
            _err(f"path does not exist: {root}")
            return EXIT_USAGE
        kind = args.kind or guess_kind(root)
        sources.append(Source(path=str(root), kind=kind))
        print(f"  {root}  ->  kind = {kind}")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(sources, db=args.db or DEFAULT_DB), encoding="utf-8")
    print(f"\nwrote {target}")
    print("next:  lux-find index && lux-find find \"something you wrote\"")
    return EXIT_OK


# ----------------------------------------------------------------------- index

def cmd_index(args) -> int:
    config = _load(args, required=True)

    quiet = args.quiet
    started = time.time()

    def progress(message: str) -> None:
        if not quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)

    force_pruned = 0
    if args.force_prune:
        force_pruned = force_prune(config)
        progress(f"force-prune removed {force_pruned} documents whose file is gone")

    stats = build(config, full=args.full, progress=progress)
    # Anything a human is told on stderr has to be in the machine-readable
    # summary too, or an automated caller reads a different run than its owner.
    stats.pruned += force_pruned

    for error in stats.errors:
        _err(error)

    payload = stats.as_dict()
    payload["db"] = str(config.db_path)
    print("SUMMARY " + json.dumps(payload, ensure_ascii=False))
    if not quiet:
        line = (
            f"indexed {stats.indexed} documents / {stats.chunks} chunks "
            f"({stats.skipped} unchanged, {stats.duplicates} duplicates, "
            f"{stats.pruned} pruned) in {time.time() - started:.2f}s"
        )
        if stats.excluded:
            # Never let a document disappear without a word: a file that was
            # left out on purpose still has to be countable by the person who
            # wonders where it went.
            spread = ", ".join(f"{n} {reason}" for reason, n in sorted(stats.excluded.items()))
            line += f"\n  left out: {spread}"
        print(line, file=sys.stderr)
    return EXIT_OK if stats.complete else EXIT_USAGE


# ------------------------------------------------------------------------ find

def cmd_find(args) -> int:
    config = _load(args, required=False)
    stale_hours = config.stale_hours

    con = open_db(config.db_path)
    try:
        info_row = con.execute("SELECT v FROM meta WHERE k='built_at'").fetchone()
        built_at = int(info_row["v"]) if info_row else 0
        outcome = run_search(
            con,
            args.query,
            limit=args.limit,
            kinds=args.kind or None,
        )
    finally:
        con.close()

    age_hours = (time.time() - built_at) / 3600 if built_at else None
    stale = age_hours is None or age_hours > stale_hours

    if args.json:
        payload = outcome.as_dict()
        payload["db"] = str(config.db_path)
        payload["built_at"] = built_at
        payload["age_hours"] = round(age_hours, 2) if age_hours is not None else None
        payload["stale"] = stale
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if stale:
            age_text = f"{age_hours:.1f} h old" if age_hours is not None else "never built"
            _err(
                f"index is {age_text} (threshold {stale_hours} h) - "
                "results may be missing recent work. Run `lux-find index`."
            )
        print(render_human(outcome, colour=use_colour(), why=args.why))

    return EXIT_OK if outcome.hits else EXIT_NO_HITS


# ---------------------------------------------------------------------- status

def cmd_status(args) -> int:
    config = _load(args, required=False)
    info = status(config.db_path)
    if args.json:
        info["stale_hours"] = config.stale_hours
        info["stale"] = info["age_hours"] is None or info["age_hours"] > config.stale_hours
        if info["age_hours"] is not None:
            info["age_hours"] = round(info["age_hours"], 3)
        print(json.dumps(info, ensure_ascii=False))
    else:
        print(render_status(info, stale_hours=config.stale_hours, colour=use_colour()))
    return EXIT_OK


# ---------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lux-find",
        description="Full-text search over the notes, code and chats "
        "you already have on disk. No server, no cloud, no network calls.",
    )
    parser.add_argument("--version", action="version", version=f"lux-find {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help=f"path to {CONFIG_NAME}")
    common.add_argument("--db", help="override the index database path")

    p_init = subparsers.add_parser("init", parents=[common], help=f"write a {CONFIG_NAME}")
    p_init.add_argument("paths", nargs="+", help="directories (or files) to make searchable")
    p_init.add_argument(
        "--kind", choices=["notes", "code", "chat"],
        help="force a kind instead of guessing from the directory contents",
    )
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config")
    p_init.set_defaults(func=cmd_init)

    p_index = subparsers.add_parser("index", parents=[common], help="build or refresh the index")
    p_index.add_argument("--full", action="store_true", help="rebuild from scratch")
    p_index.add_argument(
        "--force-prune", action="store_true",
        help="drop documents whose backing file is gone (needed after a real deletion)",
    )
    p_index.add_argument("-q", "--quiet", action="store_true", help="only print the SUMMARY line")
    p_index.set_defaults(func=cmd_index)

    p_find = subparsers.add_parser("find", parents=[common], help="search the index")
    p_find.add_argument(
        "query",
        help="words to look for (put -- first if the query starts with a dash)",
    )
    p_find.add_argument("-n", "--limit", type=int, default=8, help="max results (default 8)")
    p_find.add_argument(
        "-k", "--kind", action="append", choices=["notes", "code", "chat"],
        help="restrict to a kind; repeat to allow several",
    )
    p_find.add_argument("--json", action="store_true", help="machine-readable output")
    p_find.add_argument("--why", action="store_true", help="show the score breakdown per hit")
    p_find.set_defaults(func=cmd_find)

    p_status = subparsers.add_parser("status", parents=[common], help="index size, age, contents")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Piped into a file under a non-UTF-8 locale, one CJK character in a
    # snippet used to end the run with an encoding traceback. Results are more
    # useful with a replacement character in them than not printed at all.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError) as exc:  # pragma: no cover - platform specific
                print(f"lux-find: could not set output encoding: {exc}", file=sys.stderr)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return args.func(args)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except IndexBusy as exc:
        _err(str(exc))
        return EXIT_USAGE
    except (IndexMissing, IndexCorrupt) as exc:
        _err(str(exc))
        _err("run `lux-find index` to (re)build it; delete the file to start over.")
        return EXIT_INDEX
    except PruneGuardTripped as exc:
        _err(str(exc))
        return EXIT_USAGE
    except LuxFindError as exc:
        _err(str(exc))
        return EXIT_INDEX
    except FileNotFoundError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except PermissionError as exc:
        # Sandboxes, restricted home directories and read-only volumes all land
        # here. A traceback would bury the one useful fact: which path was denied.
        _err(f"permission denied: {exc}")
        _err("pick a writable location with --db, or set it in [index] db = ...")
        return EXIT_USAGE
    except BrokenPipeError:  # `lux-find find x | head`
        return EXIT_OK
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
