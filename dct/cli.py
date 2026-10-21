"""CLI entry point for dct (Deterministic Context Tracker)."""

import argparse
import atexit
import contextlib
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
from datetime import UTC
from pathlib import Path

from dct import __version__, migrate, paths
from dct.config import Config, load_config, save_config
from dct.core.version import VERSION_FILES

# Startup cost (#921): `dct --version`, `--help` and the hooks' fast paths
# never touch the database, so nothing at import time may pull in SQLAlchemy
# or dct.db (~200 ms of a ~300 ms start). The database entry points below are
# thin wrappers that import on first call; the vocabularies the parser needs
# as `choices` are mirrored here, locked to their core definitions by
# tests/test_cli_startup.py (dct.core.items / dct.core.releases import dct.db).
TERMINAL_STATUSES = ("resolved", "wont_fix")  # = dct.core.items.TERMINAL_STATUSES
RELEASE_FLOW = ("stamped", "files_written", "committed", "tagged", "pushed",
                "completed")  # = dct.core.releases.RELEASE_FLOW


def bootstrap_engine(*args, **kwargs):
    """dct.core.engine.bootstrap_engine, imported on first use."""
    from dct.core.engine import bootstrap_engine as _bootstrap_engine

    return _bootstrap_engine(*args, **kwargs)


def get_engine(*args, **kwargs):
    """dct.db.get_engine, imported on first use."""
    from dct.db import get_engine as _get_engine_impl

    return _get_engine_impl(*args, **kwargs)


def create_tables(*args, **kwargs):
    """dct.db.create_tables, imported on first use."""
    from dct.db import create_tables as _create_tables

    return _create_tables(*args, **kwargs)


# Windows-policy seam (tests flip it): dispatches the daemonization,
# liveness-probe and signal paths that differ on win32 (W8-W10, ADR #52).
_IS_WINDOWS = os.name == "nt"


# ── web daemon runtime files (platform state dir, ADR #52) ───────────────────
def _web_pidfile() -> Path:
    return paths.state_dir() / "dct-web.pid"


def _web_logfile() -> Path:
    return paths.state_dir() / "dct-web.log"


def _get_engine():
    return bootstrap_engine(create=True)


def _get_readonly_engine():
    """The read-only engine (K-84) for commands that promise no side effects;
    a missing database or older schema exits 1 naming `dct db upgrade`."""
    from dct.core.engine import readonly_engine
    from dct.db import SchemaNotReady

    try:
        return readonly_engine()
    except SchemaNotReady as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_db_upgrade(args):
    """`dct db upgrade` — the explicit bootstrap: create the database (and
    its directory) or upgrade its schema. Every write command runs the same
    bootstrap implicitly; read-only commands never do (K-84)."""
    bootstrap_engine(create=True).dispose()
    print("✓ Database schema is up to date")


def _detect_project(engine) -> str:
    """Auto-detect project slug from CWD. Exits if not found."""
    import os

    from dct.core.projects import get_project_by_path

    cwd = os.getcwd()
    project = get_project_by_path(engine, cwd)
    if not project:
        print(f"✗ Cannot detect project from {cwd}. Use: dct clog <cmd> <project>", file=sys.stderr)
        sys.exit(1)
    return project["slug"]


def _fmt_date(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt[:10]
    return dt.strftime("%Y-%m-%d")


def _prompt_version(default: str = "0.1.0") -> str | None:
    """Ask the user for a starting version when auto-detect failed. Empty input → `default`.

    Returns None when stdin has no tty (non-interactive) — caller must handle.
    """
    if not sys.stdin.isatty():
        return None
    print("  Pick a starting version (empty = default):")
    print("    1) 0.0.1  (pre-alpha)")
    print("    2) 0.1.0  (alpha — recommended)")
    print("    3) 1.0.0  (stable)")
    print("    or type your own (e.g. 2.3.4)")
    choice = input(f"  Version [{default}]: ").strip()
    if not choice:
        return default
    shortcut = {"1": "0.0.1", "2": "0.1.0", "3": "1.0.0"}
    return shortcut.get(choice, choice)


def _read_text_arg(text: str | None, path: str | None) -> str | None:
    """Resolve a `--foo` / `--foo-file` flag pair to text ('-' = stdin).

    The file wins when both are given. Returns None when neither is supplied,
    so callers can tell "not passed" from an empty string. Raises (OSError) on
    an unreadable path — call it INSIDE the command's try/except so the user
    gets `✗ Error: ...` instead of a traceback.

    Encoding is pinned to UTF-8 on BOTH paths: handoff prompts routinely carry
    '✓', '—' and non-ASCII prose, and the platform default (cp1252 on Windows,
    or a C-locale ASCII stdin) would mangle or reject them.
    """
    if path:
        if path != "-":
            return Path(path).read_text(encoding="utf-8")
        # Decode the raw byte stream ourselves so a C-locale stdin still takes
        # UTF-8. `buffer` is absent when stdin is a StringIO (tests, embedders)
        # — then the text stream is already decoded.
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is None:
            return sys.stdin.read()
        return buffer.read().decode("utf-8")
    return text


def _refuse_two_stdin_reads(*paths: str | None) -> None:
    """stdin can be read ONCE: a second `--…-file -` would silently get an
    empty text. Raises ValueError before anything is read or written."""
    if sum(p == "-" for p in paths) > 1:
        raise ValueError("only one --…-file flag can read stdin ('-'); pass the other as a file")


def _json_default(obj):
    """json.dumps fallback for core payloads — datetime → ISO 8601, rest → str.

    Core dicts carry real datetime objects (created_at, consumed_at, ...);
    without this every `--json` surface would raise TypeError.
    """
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _default_identity() -> str:
    """Actor identity used when pickup/handoff callers omit theirs.

    Shape: `<client> @ <UTC minute>` — the provenance client when the host
    self-identifies (Claude Code, a G2 adapter), else "cli". Commands ECHO the
    resolved value so attribution is never silent.
    """
    from datetime import datetime

    from dct.core.provenance import get_provenance

    client = get_provenance().actor_client or "cli"
    return f"{client} @ {datetime.now(UTC).strftime('%Y-%m-%dT%H:%MZ')}"


def _print_table(headers: list[str], rows: list[list[str]], max_widths: dict[int, int] | None = None) -> None:
    """Print a simple aligned table."""
    if not rows:
        print("(no results)")
        return

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    if max_widths:
        for i, mw in max_widths.items():
            widths[i] = min(widths[i], mw)

    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["─" * w for w in widths]))
    for row in rows:
        truncated = [str(c)[:widths[i]] for i, c in enumerate(row)]
        print(fmt.format(*truncated))


# ── Init ──────────────────────────────────────────────────────────────────────

def _require_init_terminal():
    if not sys.stdin.isatty():
        print("dct init needs an interactive terminal. Run `dct init` in your own terminal.",
              file=sys.stderr)
        sys.exit(1)


def _print_reference_install_result(result: dict) -> None:
    """Show both independently handled reference assets, or a single result."""
    steps = [result["canonical"], result["legacy"]] if "legacy" in result else [result]
    for step in steps:
        mark = "⚠" if step["status"] == "preserved" else "✓"
        print(f"  {mark} {step['message']}")
        if step.get("remedy"):
            print(f"    → {step['remedy']}")


def cmd_init(args):
    _require_init_terminal()
    print("=== dct — Deterministic Context Tracker — setup ===\n")

    # Database
    db_type = input("Database backend [sqlite/postgresql] (default: sqlite): ").strip().lower() or "sqlite"
    if db_type == "postgresql":
        default_url = "postgresql://localhost:5432/dct"
        url = input(f"PostgreSQL URL [{default_url}]: ").strip() or default_url
    else:
        db_path = paths.default_db_path()
        url = paths.sqlite_url(db_path)
        print(f"Using SQLite at {db_path}")

    # Test connection
    try:
        engine = get_engine(url)
        with engine.connect() as conn:
            import sqlalchemy as sa_mod
            conn.execute(sa_mod.text("SELECT 1"))
        print("Connection OK")
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    # Save config — init always targets the platform-neutral location, even on
    # a machine whose config was so far read from legacy ~/.claude (ADR #52).
    config = Config(database_url=url)
    path = save_config(config, paths.config_dir() / "dct.toml")
    print(f"Config saved to {path}")

    # Create tables
    create_tables(engine)
    print("Tables created.\n")

    # Register projects
    from dct.core.projects import create_project, set_version

    while True:
        reg = input("Register a project? [Y/n]: ").strip().lower()
        if reg == "n":
            break

        slug = input("Project slug: ").strip()
        if not slug:
            continue
        proj_path = input("Project path [.]: ").strip() or "."
        import os
        proj_path = os.path.abspath(proj_path)
        name = input(f"Project name [{slug}]: ").strip() or slug

        try:
            project = create_project(engine, slug=slug, name=name, path=proj_path)
            print(f"  ✓ Project '{slug}' registered")
            _warn_unsupported_detection(project)
            version = project.get("current_version")
            source = project.get("_detected_source")
            if version and source:
                print(f"    ↪ Detected version {version} from {source}")
            elif not version:
                picked = _prompt_version()
                if picked:
                    stored = set_version(engine, slug, picked)["current_version"]
                    print(f"    ✓ Version set to {stored}")
                else:
                    print(f"    ⚠ No version — run `dct project set-version {slug} <v>` later")
        except Exception as e:
            print(f"  ✗ Error: {e}")

        another = input("Register another? [Y/n]: ").strip().lower()
        if another == "n":
            break

    # Install hook, skills, and register MCP server (user scope)
    from dct.integrations.claude import (
        install_hook,
        install_reference,
        install_skills,
        register_mcp,
    )

    ok, msg = register_mcp(config)
    if ok:
        print(f"\n  ✓ MCP server — {msg}")
    if install_hook(config):
        print("  ✓ Global hook installed")
    if install_skills(config):
        print("  ✓ Skills installed")
    # The dct reference read on demand (K-36) — an installed asset like the
    # skills, independent of the CLAUDE.md consent below.
    reference = install_reference(config)
    _print_reference_install_result(reference)

    # Wire hooks into ~/.claude/settings.json (idempotent; consent-gated).
    # args.wire_hooks: True (--wire-hooks) / False (--no-wire-hooks) / None (ask
    # on a tty, skip when non-interactive — hooks rule 1).
    wire = getattr(args, "wire_hooks", None)
    if wire is None:
        if sys.stdin.isatty():
            answer = input(
                "Wire hooks into ~/.claude/settings.json? [Y/n]: "
            ).strip().lower()
            wire = answer != "n"
        else:
            wire = False
    if wire:
        from dct.integrations.claude import wire_hooks_into_settings
        try:
            result = wire_hooks_into_settings()
        except ValueError as e:
            print(f"  ⚠ Hook wiring skipped: {e}")
        else:
            if result["added"]:
                print(f"  ✓ Hooks wired into settings.json: {', '.join(result['added'])}")
                if result["backup"]:
                    print(f"    (backup: {result['backup']})")
            else:
                print("  ✓ Hooks already wired into settings.json")
    else:
        print("  ⚠ Hooks not wired — run `dct hooks wire` later (see docs/hooks.md).")

    # Attach the CLAUDE.md pointer (idempotent; consent-gated — a short block
    # naming the canonical shared reference, so the assistant knows dct exists and
    # where to read when to reach for it; plain "track this" works reliably).
    # args.attach_snippet: True (--attach-snippet) / False (--no-attach-snippet)
    # / None (ask on a tty, skip when non-interactive).
    attach = getattr(args, "attach_snippet", None)
    if attach is None:
        if sys.stdin.isatty():
            answer = input(
                "Add a short dct pointer to ~/.claude/CLAUDE.md "
                f"(it names {paths.dct_reference_path()}, read on demand)? [Y/n]: "
            ).strip().lower()
            attach = answer != "n"
        else:
            attach = False
    if attach:
        from dct.integrations.claude import attach_claude_md_snippet
        try:
            result = attach_claude_md_snippet()
        except ValueError as e:
            print(f"  ⚠ CLAUDE.md pointer skipped: {e}")
        else:
            if result["status"] == "preserved":
                print(f"  ⚠ CLAUDE.md pointer not updated: {result['message']}")
                if result.get("remedy"):
                    print(f"    → {result['remedy']}")
            else:
                verb = {
                    "added": "added to",
                    "refreshed": "refreshed in",
                    "unchanged": "already present in",
                }[result["status"]]
                print(f"  ✓ dct pointer {verb} {result['path']}")
                if result.get("backup"):
                    print(f"    (backup: {result['backup']})")
    else:
        print(
            "  ⚠ CLAUDE.md pointer not attached — re-run with --attach-snippet, or "
            "paste the block below '---' in dct/assets/claude-md-snippet.md yourself."
        )

    print("\nTo register more projects:  dct project add <slug> <path>")
    print("To import existing data:    use /dct-import in a project session")
    print("\nSetup complete! Start a NEW Claude Code session now — skills,")
    print("hooks and MCP tools all load at session start.")


# ── Hooks commands ────────────────────────────────────────────────────────────

def cmd_hooks_wire(args):
    from dct.integrations.claude import wire_hooks_into_settings

    try:
        result = wire_hooks_into_settings(settings_path=args.settings)
    except ValueError as e:
        print(f"✗ {e}")
        sys.exit(1)

    if result["added"]:
        print(f"✓ Wired {len(result['added'])} hook(s) into {result['settings_path']}:")
        for name in result["added"]:
            print(f"    + {name}")
        if result["backup"]:
            print(f"  backup: {result['backup']}")
    if result["skipped"]:
        print(f"  already wired: {', '.join(result['skipped'])}")
    if not result["added"] and not result["skipped"]:
        print("✓ Nothing to wire.")


# ── Project commands ──────────────────────────────────────────────────────────

def cmd_project_add(args):
    import os

    from dct.core.projects import create_project, set_version

    # #227: realpath (not abspath) so a symlinked path is stored canonically and
    # matches os.getcwd() (physical) at detect time. os.getcwd() is already physical.
    path = os.path.realpath(args.path) if args.path else os.getcwd()
    name = args.name or args.slug
    engine = _get_engine()

    try:
        project = create_project(engine, slug=args.slug, name=name, path=path, current_version=args.version)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Project '{args.slug}' registered at {path}")
    _warn_unsupported_detection(project)
    # #876: the changelog file a release would write — reported, never stored
    # (the release proposal binds the detected path per release).
    _print_changelog_file(project, stored_note=True)

    version = project.get("current_version")
    source = project.get("_detected_source")
    if version:
        if source and not args.version:
            print(f"  ↪ Detected version {version} from {source}")
        else:
            print(f"  Version: {version}")
        return

    # No version on the row — try an interactive prompt, else warn & bail out.
    picked = _prompt_version() if sys.stdin.isatty() else None
    if picked:
        try:
            stored = set_version(engine, args.slug, picked)["current_version"]
        except ValueError as e:
            print(f"✗ Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"  ✓ Version set to {stored}")
    else:
        what = ("No supported version stored" if project.get("_detected_unsupported")
                else "No version detected")
        print(
            f"  ⚠ {what}. Run `dct project set-version {args.slug} <version>` "
            f"before using `/release`.",
            file=sys.stderr,
        )


def _warn_unsupported_detection(project: dict) -> None:
    """Say so when create_project detected a version the contract refuses
    (K-54) — it was NOT stored and nothing was substituted."""
    unsupported = project.get("_detected_unsupported")
    if unsupported:
        print(
            f"  ⚠ Detected version {unsupported['version']!r} in {unsupported['source']} "
            f"is not stored: {unsupported['reason']}",
            file=sys.stderr,
        )


def cmd_project_set_version(args):
    from dct.core.projects import set_version

    engine = _get_engine()
    try:
        project = set_version(engine, args.slug, args.version)
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    if project:
        # The STORED (normalized) form — what /release and tags will use.
        print(f"✓ {args.slug} → {project['current_version']}")
    else:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)


def cmd_project_rename(args):
    from dct.core.projects import update_project

    engine = _get_engine()
    new_slug = getattr(args, "new_slug", None)
    new_name = getattr(args, "name", None)
    new_description = getattr(args, "description", None)
    new_path = getattr(args, "path", None)

    if all(v is None for v in (new_slug, new_name, new_description, new_path)):
        print(
            "✗ Nothing to rename — pass at least one of --slug, --name, --description, --path",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        project = update_project(
            engine,
            args.slug,
            new_slug=new_slug,
            new_name=new_name,
            new_description=new_description,
            new_path=new_path,
        )
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)

    if project is None:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)

    print(f"✓ Project '{args.slug}' updated")
    print(f"  slug:        {project['slug']}")
    print(f"  name:        {project['name']}")
    if project.get("description"):
        print(f"  description: {project['description']}")
    if project.get("path"):
        print(f"  path:        {project['path']}")


def cmd_project_changelog(args):
    from dct.core.projects import set_manages_changelog

    engine = _get_engine()
    project = set_manages_changelog(engine, args.slug, args.state == "on")
    if project:
        state = "ON" if project["manages_changelog"] else "OFF"
        print(f"✓ {args.slug}: dct changelog enforcement {state}")
    else:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)


def cmd_project_release_executor(args):
    """`dct project release-executor <slug> [own|dct]` — get or set who
    performs the project's release effects (K-85; default dct)."""
    from dct.core.projects import get_project, set_release_executor

    engine = _get_engine()
    if args.executor is None:
        project = get_project(engine, args.slug)
    else:
        project = set_release_executor(engine, args.slug, args.executor)
    if not project:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.slug}: release-executor → {project['release_executor']}"
          if args.executor is not None else f"{args.slug}: {project['release_executor']}")


def cmd_project_changelog_pr_refs(args):
    """`dct project changelog-pr-refs <slug> [on|off]` — get or set the
    `(#N)` PR-reference render opt-in (K-90; default off)."""
    from dct.core.projects import get_project, set_changelog_pr_refs

    engine = _get_engine()
    if args.state is None:
        project = get_project(engine, args.slug)
    else:
        project = set_changelog_pr_refs(engine, args.slug, args.state == "on")
    if not project:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    state = "on" if project["changelog_pr_refs"] else "off"
    print(f"✓ {args.slug}: changelog-pr-refs → {state}" if args.state is not None
          else f"{args.slug}: {state}")


def _trailer_answer(value: bool | None) -> str:
    """One commit-trailer preference in words: on / off / not answered."""
    return "not answered" if value is None else ("on" if value else "off")


def cmd_project_trailers(args):
    """`dct project trailers <slug> [--co-authored-by on|off]
    [--session-link on|off]` — get or store the project's commit-trailer
    preferences (asked once at the project's first configuration; /commit
    applies them without another question). A flag left out keeps that
    preference as stored."""
    from dct.core.projects import get_commit_trailers, set_commit_trailers

    engine = _get_engine()
    answers = {key: getattr(args, key) for key in ("co_authored_by", "session_link")}
    given = {key: value == "on" for key, value in answers.items() if value is not None}
    stored = None
    if not given or set_commit_trailers(engine, args.slug, **given) is not None:
        stored = get_commit_trailers(engine, args.slug)
    if stored is None:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    arrow = " → " if given else " "
    shown = (f"Co-Authored-By{arrow}{_trailer_answer(stored['co_authored_by'])} · "
             f"session link{arrow}{_trailer_answer(stored['session_link'])}")
    print(f"✓ {args.slug}: {shown}" if given else f"{args.slug}: {shown}")


def cmd_project_version_sync_script(args):
    """`dct project version-sync-script <slug> [--kind FILE]` — write the
    project's scripts/dct-version-sync.sh from dct's packaged template with
    the block of its version file switched on (default: the file
    detect_version finds), so every release sets the version there (K-76).
    Never overwrites an existing script."""
    from dct.core.projects import get_project
    from dct.core.version import detect_version
    from dct.core.version_sync import SCRIPT_PATH, write_script

    project = get_project(_get_engine(), args.slug)
    if project is None:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    root = project["path"]
    kind = args.kind or detect_version(root)[1]
    if kind is None:
        print(f"✗ No version file found in {root} (VERSION, pyproject.toml, package.json, "
              "Cargo.toml, src-tauri/tauri.conf.json) — name one with --kind", file=sys.stderr)
        sys.exit(1)
    try:
        write_script(root, kind)
    except FileExistsError:
        print(f"✗ {SCRIPT_PATH} already exists in {root} — nothing was written", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Wrote {SCRIPT_PATH}: every release sets the new version in {kind}, "
          "and changes nothing else. It runs once it is committed.")


def cmd_project_manages_changelog(args):
    """Hook query — exit 0 if the project opts into dct changelog enforcement,
    else exit 1. Silent by design (the changelog hook reads only the exit code).
    """
    from dct.core.projects import project_manages_changelog

    engine = _get_engine()
    sys.exit(0 if project_manages_changelog(engine, args.slug) else 1)


def _print_changelog_file(project: dict, *, stored_note: bool = False) -> None:
    """Print the changelog file a release of `project` would write — the ONE
    resolver (core.changelog_file, #876) at the HEAD of its checkout (else
    its directory): the path and how it was found, or the problem with its
    next steps (stderr). Read-only; a failed git read is a warning."""
    import os

    from dct.core import gitinfo
    from dct.core.changelog_file import explain, resolve_at_head

    try:
        found, base = resolve_at_head(project)
    except gitinfo.GitReadError as exc:
        print(f"  ⚠ changelog file not resolved: {exc}", file=sys.stderr)
        return
    if not found["managed"]:
        print("  changelog file: none (dct's changelog file is off for this project)")
        return
    if found["problem"]:
        print(f"  ⚠ changelog file ({found['problem']}): {explain(found)}", file=sys.stderr)
        return
    if base is None:
        return
    where = os.path.join(base, *found["path"].split("/"))
    note = " — nothing stored" if stored_note and found["source"] == "detected" else ""
    print(f"  changelog file: {found['path']} ({found['source']}){note} → {where}")


def cmd_project_changelog_path(args):
    """Get or choose a project's changelog file, relative to its root (#225).

    With a relpath → the owner's EXPLICIT choice (validated to stay within
    the project root; #876 K-14: also exactly changelog.md). With --detect →
    back to detection (core.projects.reset_changelog_path). Without either
    → print the stored value, whether it is a choice, and the file a release
    would write (core.changelog_file.resolve_changelog).
    """
    from dct.core.projects import get_project, reset_changelog_path, set_changelog_path

    engine = _get_engine()

    if args.relpath is not None and args.detect:
        print("✗ give a path OR --detect, not both", file=sys.stderr)
        sys.exit(2)
    if args.relpath is None and not args.detect:
        project = get_project(engine, args.slug)
        if not project:
            print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
            sys.exit(1)
        print(f"{args.slug}: {_changelog_setting(project)}")
        _print_changelog_file(project)
        return

    try:
        project = (reset_changelog_path(engine, args.slug) if args.detect
                   else set_changelog_path(engine, args.slug, args.relpath))
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    if not project:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.slug}: changelog-path → {_changelog_setting(project)}")
    _print_changelog_file(project)


def _changelog_setting(project: dict) -> str:
    """The stored changelog path and what it means (#876 K-14)."""
    if project.get("changelog_path_explicit"):
        return f"{project['changelog_path']} (explicit choice)"
    return f"{project['changelog_path']} (not chosen — detect)"


# ── Plan rescan (v0.2.0) ─────────────────────────────────────────────────────

def _plan_classification_command(args, *, batch: bool):
    """Resolve a CLI ULID alias; the semantic core rechecks the project on write."""
    import json

    from dct.core.ids import is_valid_ulid
    from dct.core.plans.classification import (
        resolve_uncertain_sections,
        set_plan_section_type,
    )
    from dct.core.plans.crud import get_plan_by_ulid, get_plan_section_by_ulid

    try:
        alias = args.plan_ulid if batch else args.section_ulid
        if not is_valid_ulid(alias):
            raise ValueError("Expected an explicit plan/section ULID, not an integer id")
        if not args.project or not args.project.strip():
            raise ValueError("--project must name the expected project")
        engine = _get_engine()
        row = (get_plan_by_ulid if batch else get_plan_section_by_ulid)(engine, alias)
        if row is None:
            raise ValueError("Plan/section ULID not found")
        if batch:
            ack = resolve_uncertain_sections(engine, args.project, row["id"])
        else:
            ack = set_plan_section_type(engine, args.project, row["id"], args.section_type)
        if args.json:
            print(json.dumps(ack, default=_json_default))
        else:
            print(ack)
    except Exception as exc:
        print(f"✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_plan_set_section_type(args):
    _plan_classification_command(args, batch=False)


def cmd_plan_resolve_uncertain(args):
    _plan_classification_command(args, batch=True)


def cmd_clog_import_released(args):
    import json

    from dct.workflows.changelog_import import import_history, parse_released_at

    try:
        stated_time = parse_released_at(args.released_at)
        ack = import_history(_get_engine(), args.project, args.category, args.entry,
            args.version, stated_time, component=args.component,
            source_file=args.source_file, source_lines=args.source_lines,
            related_item_id=args.related_item)
        if args.json:
            print(json.dumps(ack, default=_json_default))
        else:
            print(f"✓ Imported history #{ack['id']} [{ack['version']}] — "
                  f"{ack['released_at']} ({ack['released_at_source']})")
    except Exception as exc:
        print(f"✗ Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_plan_rescan(args):
    """Re-scan a plan markdown file — hook entrypoint and manual CLI.

    Idempotent: hash short-circuits unchanged files. Brand-new files (no
    plans row yet) get the full first-import treatment including ULID
    injection + [x] state reading (§9.2).
    """
    import os

    from dct.core.plans.ingest import ingest_file
    from dct.core.projects import get_project_by_path

    engine = _get_engine()
    path = os.path.abspath(args.file)
    project = get_project_by_path(engine, path)
    if project is None:
        if not args.quiet:
            print(f"✗ No registered project owns {path}", file=sys.stderr)
        sys.exit(0)  # graceful — hook must not block user action

    try:
        report = ingest_file(
            engine, path,
            project_slug=project["slug"],
            auto_resolve_uncertain=args.resolve_uncertain,
        )
    except Exception as e:
        if not args.quiet:
            print(f"✗ rescan failed: {e}", file=sys.stderr)
        sys.exit(0)  # graceful — never block the user

    if not args.quiet:
        print(
            f"✓ {path} — plan #{report['plan_id']}: "
            f"{report['sections_created']} new sections, "
            f"{report['checkpoints_created']} new checkpoints, "
            f"{report['uncertain_remaining']} uncertain"
        )


def cmd_project_list(args):
    from dct.core.projects import list_projects

    engine = _get_engine()
    projects = list_projects(engine)
    headers = ["Slug", "Version", "Name", "Path"]
    rows = [[p["slug"], p.get("current_version") or "—", p["name"], p.get("path") or ""] for p in projects]
    _print_table(headers, rows)


def cmd_project_remove(args):
    from dct.core.projects import delete_project

    engine = _get_engine()
    if delete_project(engine, args.slug):
        print(f"✓ Project '{args.slug}' removed (soft delete)")
    else:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)


# ── Item commands ─────────────────────────────────────────────────────────────

def cmd_add(args):
    from dct.core.items import create_item

    engine = _get_engine()
    kwargs = {}
    if args.priority:
        kwargs["priority"] = args.priority
    if args.component:
        kwargs["component"] = args.component
    if args.short_title:
        kwargs["short_title"] = args.short_title
    if args.tags:
        # Shell passes `--tags "a,b,c"` as a string; core expects list[str].
        # Normalize at the CLI boundary so core stays pure (see #31).
        kwargs["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.description:
        kwargs["description"] = args.description
    if args.source_file:
        kwargs["source_file"] = args.source_file

    try:
        item = create_item(engine, args.project, args.type, args.title, **kwargs)
        print(f"✓ #{item['id']} {item['item_type']}/{item['priority']} — {item['title']}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    from dct.core.items import list_items
    from dct.core.text import visible_label

    engine = _get_engine()
    kwargs = {}
    if args.project:
        kwargs["project_slug"] = args.project
    if args.status:
        kwargs["status"] = args.status
    if args.type:
        kwargs["item_type"] = args.type
    if args.priority:
        kwargs["priority"] = args.priority
    if args.search:
        kwargs["search"] = args.search
    if args.tag:
        kwargs["tag"] = args.tag
    if args.gate:
        kwargs["gate"] = args.gate

    try:
        items = list_items(engine, **kwargs)
    except ValueError as e:
        # Unknown tag (the core error carries the known-names suggestion) or a
        # non-gate --gate kind.
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    headers = ["#", "Project", "Type", "Pri", "Status", "Label", "Title"]

    # Build project_id → slug cache
    from dct.core.projects import list_projects
    slug_map = {p["id"]: p["slug"] for p in list_projects(engine)}

    rows = []
    for i in items:
        slug = slug_map.get(i.get("project_id"), "?")
        rows.append([
            str(i["id"]), slug, i["item_type"], i["priority"], i["status"],
            visible_label(i.get("short_title")) or "", i["title"][:60],
        ])

    # Label is capped below its 50-char column so the title keeps its room.
    _print_table(headers, rows, max_widths={5: 30, 6: 60})


def cmd_show(args):
    from dct.core.items import get_item
    from dct.core.text import visible_label

    engine = _get_engine()
    item = get_item(engine, args.id)
    if not item:
        print(f"✗ Item #{args.id} not found", file=sys.stderr)
        sys.exit(1)

    print(f"#{item['id']}  {item['item_type']}/{item['priority']}  [{item['status']}]")
    print(f"Title:       {item['title']}")
    if visible_label(item.get("short_title")):
        print(f"Short title: {item['short_title']}")
    if item.get("description"):
        print(f"Description: {item['description']}")
    if item.get("component"):
        print(f"Component:   {item['component']}")
    if item.get("source_file"):
        loc = item["source_file"]
        if item.get("source_lines"):
            loc += f":{item['source_lines']}"
        print(f"Location:    {loc}")
    if item.get("tags"):
        print(f"Tags:        {', '.join(item['tags'])}")
    if item.get("resolution"):
        print(f"Resolution:  {item['resolution']}")
    print(f"Created:     {_fmt_date(item['created_at'])}")
    if item.get("resolved_at"):
        print(f"Resolved:    {_fmt_date(item['resolved_at'])}")

    if item.get("notes"):
        print(f"\n── Notes ({len(item['notes'])}) ──")
        for n in item["notes"]:
            print(f"  [{_fmt_date(n['created_at'])}] {n['note']}")

    # The full list the resolve_item gate refusal points at (#917 K-56):
    # every checkpoint with its kind and untruncated text.
    if item.get("checkpoints"):
        print(f"\n── Checkpoints ({len(item['checkpoints'])}) ──")
        for c in item["checkpoints"]:
            print(f"  #{c['id']} [{c['kind']}] {c['status']}: {c['text']}")


def cmd_update(args):
    from dct.core.items import update_item

    engine = _get_engine()
    kwargs = {}
    if args.title:
        kwargs["title"] = args.title
    if args.status:
        kwargs["status"] = args.status
    if args.priority:
        kwargs["priority"] = args.priority
    if args.component:
        kwargs["component"] = args.component
    if args.resolution:
        kwargs["resolution"] = args.resolution
    if args.short_title is not None:
        # `is not None` on purpose: `--short-title ""` clears the label (core
        # stores "" as NULL), while omitting the flag leaves it untouched.
        kwargs["short_title"] = args.short_title
    if args.tags is not None:
        # `is not None` on purpose: `--tags ""` is a legal CLEAR (empty list),
        # while omitting the flag leaves the item's tags untouched.
        kwargs["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.reason is not None:
        kwargs["reason"] = args.reason

    try:
        item = update_item(engine, args.id, **kwargs)
        if item:
            print(f"✓ #{item['id']} updated — {item['title']}")
        else:
            print(f"✗ Item #{args.id} not found", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_resolve(args):
    from dct.core.items import resolve_item

    engine = _get_engine()
    try:
        item = resolve_item(engine, args.id, status=args.status, resolution=args.resolution)
        if item:
            print(f"✓ #{item['id']} → {item['status']}")
        else:
            print(f"✗ Item #{args.id} not found", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_note(args):
    from dct.core.items import add_note

    engine = _get_engine()
    try:
        add_note(engine, args.id, args.note)
        print(f"✓ Note added to #{args.id}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_delete(args):
    from dct.core.items import delete_item

    engine = _get_engine()
    if delete_item(engine, args.id):
        print(f"✓ #{args.id} soft-deleted")
    else:
        print(f"✗ Item #{args.id} not found", file=sys.stderr)
        sys.exit(1)


def _ids(raw: str | None, flag: str, noun: str = "item") -> list[int] | None:
    """Parse a comma-separated id flag. None → flag unused; empty/garbage →
    ValueError (a blank shell var must never read as "no ids" or wipe
    anything — K-27)."""
    if raw is None:
        return None
    try:
        ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise ValueError(
            f"{flag} expects comma-separated {noun} ids, got: {raw!r}"
        ) from None
    if not ids:
        raise ValueError(f"{flag} got no ids ({raw!r}) — omit the flag instead")
    return ids


# ── Handoff commands ──────────────────────────────────────────────────────────

def cmd_handoff_create(args):
    """`dct handoff create` — session-end op: set sprint membership + leave the
    handoff prompt in ONE transaction (workflows.session.handoff_work)."""
    import json

    from dct.workflows.session import handoff_work

    engine = _get_engine()
    try:
        project = args.project or _detect_project(engine)
        _refuse_two_stdin_reads(args.prompt_file, args.context_summary_file)
        prompt = _read_text_arg(args.prompt, args.prompt_file)
        # The summary the owner approved on the printed draft, stored as is
        # (#877 K-58) — a file / stdin keeps every line of a long one.
        context_summary = _read_text_arg(args.context_summary, args.context_summary_file)
        identity = args.created_by or _default_identity()

        result = handoff_work(
            engine, project,
            prompt=prompt or "",
            created_by=identity,
            item_ids=_ids(args.items, "--items"),
            add_item_ids=_ids(args.add_items, "--add-items"),
            remove_item_ids=_ids(args.remove_items, "--remove-items"),
            sprint=args.sprint,
            scope=args.scope,
            context_summary=context_summary,
        )
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, default=_json_default))
        return

    h = result["handoff"]
    print(f"✓ Handoff #{h['id']} created (scope: {h.get('scope') or 'none'}, by {identity})")
    sprint = result.get("sprint")
    if sprint:
        print(f"  sprint #{sprint['sprint_id']}: {sprint['open_size']} open items")
    else:
        print("  sprint untouched")


def cmd_handoff_update(args):
    from dct.core.handoff import update_handoff

    engine = _get_engine()

    try:
        _refuse_two_stdin_reads(args.prompt_file, args.context_summary_file)
        prompt = _read_text_arg(args.prompt, args.prompt_file)
        result = update_handoff(
            engine, args.id,
            prompt=prompt or None,
            scope=args.scope,
            context_summary=_read_text_arg(args.context_summary, args.context_summary_file),
        )
        if result is None:
            print(f"✗ Handoff #{args.id} not found, consumed, or deleted", file=sys.stderr)
            sys.exit(1)
        print(f"✓ Handoff #{args.id} updated")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


# ── Session pickup (#413) ─────────────────────────────────────────────────────

def _cols_rows_dicts(table: dict | None) -> list[dict]:
    """{cols, rows} → list of dicts — the compact wire shape read by name."""
    if not table:
        return []
    return [dict(zip(table["cols"], row)) for row in table["rows"]]


_CONTROL_CHARS = {c: None for c in range(0x20) if c not in (0x09, 0x0A)} | {0x7F: None}


def _strip_controls(text: str | None) -> str:
    """Drop C0 control characters from DB text before printing it.

    Handoff prompts and item titles are free text written by another session;
    an ESC sequence in there would repaint or clear the reader's terminal. Tabs
    and newlines are legitimate formatting and survive — everything else below
    0x20, plus DEL, is removed. None → "".
    """
    if not text:
        return ""
    return text.translate(_CONTROL_CHARS)


def _render_pickup(snap: dict, identity: str | None) -> None:
    """Human view of `dct pickup`: what was consumed, then the state that
    matters at session start. Sections dropped via --include are simply
    absent from the snapshot, so every lookup is guarded. ALL free text coming
    from the DB is passed through _strip_controls — a stored escape sequence
    must not drive the reader's terminal."""
    picked = snap.get("picked_up")
    summary = snap.get("summary", {})

    if picked:
        print(f"✓ Picked up handoff #{picked['id']} (consumed as {identity})")
        print()
        for line in _strip_controls(picked.get("prompt")).splitlines():
            print(f"  {line}")
        if picked.get("context_summary"):
            print("\nContext summary:")
            for line in _strip_controls(picked["context_summary"]).splitlines():
                print(f"  {line}")
    else:
        print(f"— no handoff consumed ({summary.get('handoffs_pending', 0)} pending)")

    sprint = snap.get("sprint")
    if sprint:
        items = _cols_rows_dicts(sprint.get("items"))
        pending_cp = sum(
            1 for c in _cols_rows_dicts(sprint.get("checkpoints")) if c.get("status") == "pending"
        )
        print(f"\nSprint items ({pending_cp} pending checkpoints):")
        _print_table(
            ["#", "sprint", "prio", "status", "cp", "title"],
            [
                [
                    str(i["id"]), str(i.get("sprint_id") or ""), i.get("priority") or "",
                    i.get("status") or "", "{}/{}".format(*(i.get("cp") or [0, 0])),
                    _strip_controls(i.get("title")),
                ]
                for i in items
            ],
            max_widths={5: 60},
        )

    gates = _cols_rows_dicts(snap.get("gates"))
    if gates:
        print("\nPending gates:")
        _print_table(
            ["#", "cp", "kind", "prio", "sprint", "title", "text"],
            [
                [
                    str(g["item_id"]), str(g["checkpoint_id"]), g.get("kind") or "",
                    g.get("priority") or "", "yes" if g.get("in_current_sprint") else "",
                    _strip_controls(g.get("item_title")),
                    _strip_controls(g.get("text")),
                ]
                for g in gates
            ],
            max_widths={5: 30, 6: 50},
        )

    if summary:
        print(
            f"\nSprint: size {summary.get('sprint_size', 0)}, "
            f"done {summary.get('sprint_completion', '0/0')} · "
            f"open in project: {summary.get('total_open_in_project', 0)} · "
            f"handoffs pending: {summary.get('handoffs_pending', 0)} · "
            f"gates: {summary.get('gates_pending', 0)}"
        )


def cmd_pickup(args):
    """`dct pickup` — session-start op: one snapshot read + (possibly) consume
    ONE handoff (workflows.session.pickup_work)."""
    import json

    from dct.workflows.session import parse_sections, pickup_work

    consume = {"auto": None, "always": True, "never": False}[args.consume]
    engine = _get_readonly_engine() if consume is False else _get_engine()
    # consumed_by is REQUIRED by pickup_work whenever consumption is possible;
    # default it (and echo it) rather than let the op fail on a missing identity.
    identity = args.consumed_by or (None if consume is False else _default_identity())

    try:
        project = args.project or _detect_project(engine)
        include = parse_sections(args.include)
        snap = pickup_work(
            engine, project,
            consume=consume,
            handoff_id=args.handoff_id,
            scope=args.scope,
            consumed_by=identity,
            since=args.since,
            include=include,
        )
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(snap, indent=2, default=_json_default))
        return
    _render_pickup(snap, identity)


# ── Changelog commands ────────────────────────────────────────────────────────

def cmd_clog_add(args):
    from dct.core.changelog import add_entry, resolve_branch_fields

    engine = _get_engine()
    kwargs = {}
    if args.component:
        kwargs["component"] = args.component
    if args.source_file:
        kwargs["source_file"] = args.source_file
    if args.item_id:
        kwargs["related_item_id"] = args.item_id

    try:
        project_slug = args.project or _detect_project(engine)
        # The CLI runs in the caller's directory, so detected context is the
        # caller's own → branch_source='client' either way.
        kwargs.update(resolve_branch_fields(
            engine, project_slug, branch=args.branch, worktree=args.worktree,
            use_process_cwd=True, detected_source="client",
        ))
        e = add_entry(engine, project_slug, args.category, args.entry, **kwargs)
        where = f" @ {e['branch']}" if e.get("branch") else ""
        print(f"✓ Changelog #{e['id']} [{e['category']}]{where} — {e['entry'][:60]}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def _branch_filter_kwargs(args) -> dict:
    """`clog list` / `clog count` branch filters → core keyword arguments.

    A relative --worktree is refused here (exit 1, message on stderr) under
    the same contract core applies (normalize_worktree, K-42)."""
    from dct.core.changelog import normalize_worktree

    try:
        worktree = normalize_worktree(args.worktree)
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    return {
        "branch": args.branch or None,
        "worktree": worktree,
        "include_unbranched": args.include_unbranched,
    }


def cmd_clog_list(args):
    from dct.core.changelog import list_entries

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    entries = list_entries(
        engine, project_slug, unreleased_only=args.unreleased, **_branch_filter_kwargs(args)
    )
    headers = ["#", "Category", "Entry", "Version", "Branch", "Created"]
    rows = []
    for e in entries:
        rows.append([
            str(e["id"]),
            e["category"],
            e["entry"][:50],
            e["version"] or "(unreleased)",
            e["branch"] or "",
            _fmt_date(e["created_at"]),
        ])
    _print_table(headers, rows, max_widths={2: 50})


def cmd_clog_count(args):
    from dct.core.changelog import count_unreleased

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    # Bare integer on stdout — hooks parse it (never decorate this line).
    count = count_unreleased(engine, project_slug, **_branch_filter_kwargs(args))
    print(count)


def _release_ids(raw: str | None, flag: str = "--entries") -> list[int] | None:
    try:
        return _ids(raw, flag, "entry")
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def _print_effects(out: dict) -> None:
    """Human rendering of an effects-layer result (dct.workflows.release.effects)."""
    for step in out["steps"]:
        extra = ", ".join(f"{k}={v}" for k, v in step.items() if k not in ("step", "result"))
        print(f"  {step['step']}: {step['result']}{' (' + extra + ')' if extra else ''}")
    record = out.get("release")
    if record:
        print(f"  release record {record['version']}: {record['status']}")
    for warning in out["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)


def _run_effects(action: str, project_slug: str, as_json: bool, call) -> dict:
    """Run one effects-layer call; a refusal / failure prints its reason
    (or the JSON result with outcome 'refused' / 'failed' / 'conflict') and
    exits with its code (1, the failing version-sync script's code, or 75
    for a lost serialization race — K-88: the effects layer reports the
    step, the effects it observed and the re-read record; nothing is
    retried)."""
    import json

    from dct.core.gitinfo import CheckoutVerificationError
    from dct.workflows.release.effects import (
        ReleaseConflict,
        ReleaseRefused,
        SyncFailed,
        result,
    )

    try:
        out = call()
    except ReleaseRefused as e:
        outcome = ("failed" if isinstance(e, SyncFailed)
                   else "conflict" if isinstance(e, ReleaseConflict) else "refused")
        code = e.exit_code
        failure = result(action, project_slug, outcome, release=e.release, steps=e.steps,
                         details={**e.details, "error": str(e)},
                         version=(e.release or {}).get("version"))
        message = str(e)
    except (CheckoutVerificationError, ValueError) as e:
        code, message = 1, str(e)
        failure = result(action, project_slug, "refused", details={"error": message})
    else:
        if as_json:
            print(json.dumps(out, indent=2, default=_json_default))
        return out
    if as_json:
        print(json.dumps(failure, indent=2, default=_json_default))
    else:
        for step in failure["steps"]:
            print(f"  {step['step']}: {step['result']}")
        # K-109: a release commit that changed older changelog history is
        # SHOWN — the diff of what it changed besides its own section.
        for item in failure["details"].get("history") or []:
            total = item.get("diff_lines", len(item["diff"]))
            print(f"  changelog history changed by {item['commit']} ({total} diff line(s), "
                  f"sha256 {item['diff_sha256']}):", file=sys.stderr)
            for line in item["diff"]:
                print(f"    {line}", file=sys.stderr)
            if total > len(item["diff"]):
                print(f"    … {total - len(item['diff'])} more line(s) — see --json",
                      file=sys.stderr)
    print(f"✗ {message}", file=sys.stderr)
    sys.exit(code)


REMOVED_CONFIRM_VERSION_FILES = (
    "--confirm-version-files was removed: the version-file set is no longer "
    "confirmed after the stamp. Run `dct release prepare --version V (--entries … | "
    "--no-entries) --plan-digest D`, review the proposed files and pass its "
    "proposal_sha256 as --proposal-digest; nothing released.")


def _refuse_removed_flag(args) -> None:
    """The removed --confirm-version-files (kept as a hidden, refusing flag so
    an old invocation gets the new procedure instead of a usage error)."""
    if getattr(args, "confirm_version_files", False):
        print(f"✗ {REMOVED_CONFIRM_VERSION_FILES}", file=sys.stderr)
        sys.exit(1)


RELEASE_EXECUTOR_EPILOG = (
    "Executor: a project whose release executor is 'own' (`dct project release-executor "
    "<slug> own`) gets no effect from dct — prepare / apply / resume refuse before any write; "
    "its own tool uses `dct release plan --json` and the ledger (MCP release_changelog / "
    "advance_release). Resume performs only a release dct stamped with the owner's consent. "
    "A lost database serialization race exits 75 (outcome 'conflict': the step, the effects "
    "already performed, the re-read record) — never retried automatically. ")

RELEASE_LOCK_EPILOG = (
    "Locks: the command holds the release operation lock of the repository of the "
    "project's REGISTERED path (one at a time, also across worktrees; another release "
    "command refuses at once). A project without a registered path, or with a path that "
    "cannot be verified, is refused — dct never locks on a substitute key. ")

RELEASE_PUSH_EPILOG = RELEASE_EXECUTOR_EPILOG + RELEASE_LOCK_EPILOG + (
    "Commit (needs git >= 2.36): the repository's hooks run explicitly (pre-commit, "
    "prepare-commit-msg, commit-msg on a temporary index holding the approved tree), the "
    "commit is created on the pinned candidate and published by a compare-and-swap of the "
    "branch HEAD names, then post-commit. A refusing hook, a hook that changes the approved "
    "content or message, a HEAD that moved or a branch switch stops the release with "
    "NOTHING published (resume after fixing; nothing is reset). Push (after the tag): `git "
    "push --atomic --no-follow-tags --recurse-submodules=no <url> "
    "<commit>:refs/heads/<branch> <commit>:refs/tags/v<V>` to ONE approved destination — "
    "exactly those two refs: no tag push.followTags would add, no submodule commit (a "
    "submodule is never pushed; publishing the commit a gitlink names is the owner's). A "
    "NEW release that pushes needs the COMPLETE approved target: --push-remote, "
    "--push-branch and --push-url, the `push_target` that `dct release prepare` showed (the "
    "current branch's upstream and its push URL, `git remote get-url --push --all`) and the "
    "owner approved — apply never selects it itself, a missing or mismatching part is "
    "refused before the stamp; --no-push is an explicit local release. The URL must be one "
    "of the remote's push URLs (several: it chooses ONE, no mirror mode) and must not be "
    "readable as a remote NAME or rewritten by insteadOf (write a path as ./name, an "
    "absolute path or a real URL). The same URL is read before the stamp and to verify "
    "the push. A failed push leaves the release 'tagged'; `dct release resume` retries only "
    "the push, to the approved URL.")


def _add_push_flags(parser) -> None:
    """The push target of `release apply` / `clog release` (K-97 F2; K-123:
    all three are required there for a new release that pushes), of
    `release takeover` (its preview derives what is not given) and of
    `release prepare --preview` (the same: default the upstream)."""
    parser.add_argument(
        "--push-remote",
        help="Remote to push the release commit and tag to. apply / clog release: REQUIRED "
             "for a new release that pushes, with --push-branch and --push-url (the "
             "push_target `dct release prepare` showed). takeover, prepare --preview: "
             "default the branch's upstream")
    parser.add_argument(
        "--push-branch",
        help="Branch of that remote. apply / clog release: REQUIRED with --push-remote. "
             "takeover: default the upstream's branch when it is on that remote")
    parser.add_argument(
        "--push-url",
        help="Which ONE of the remote's push URLs (`git remote get-url --push --all`) the "
             "release goes to; refused when it is not one of them. apply / clog release: "
             "REQUIRED with --push-remote. takeover: required when the remote has several")
    parser.add_argument(
        "--no-push", action="store_true",
        help="An explicit local release: commit and tag, nothing pushed")


def _apply_from_args(args, *, strict: bool) -> None:
    """`dct release apply` and its alias `dct clog release` (strict)."""
    from dct.workflows.release.effects import apply_release

    _refuse_removed_flag(args)
    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    version = getattr(args, "version", None) or None
    if not version and not args.bump:
        print("✗ Provide a version or --patch/--minor/--major", file=sys.stderr)
        sys.exit(1)
    entry_ids = None if args.no_entries else _release_ids(args.entries)
    as_json = getattr(args, "json", False)
    out = _run_effects("apply", project_slug, as_json, lambda: apply_release(
        engine, project_slug, version=version, bump_type=None if version else args.bump,
        entry_ids=entry_ids, no_entries=args.no_entries,
        confirm=args.confirm, strict=strict,
        plan_digest=None if strict else args.plan_digest,
        proposal_digest=args.proposal_digest, push_remote=args.push_remote,
        push_branch=args.push_branch, push_url=args.push_url, no_push=args.no_push))
    if as_json:
        return
    details = out["details"]
    if details.get("bump"):
        print(f"  {details['previous_version']} → {out['version']} ({details['bump']})")
    if out["outcome"] == "completed":
        print(f"✓ Released {details['released']} entries as {out['version']} "
              f"(mode: {details['mode']}) — current version: {details['current_version']}")
        target = details.get("push_target")
        print(f"  pushed to {target['remote']} {target['url']} ({target['branch']}, "
              f"{target['tag']})" if target else "  not pushed (no push target approved)")
    else:
        print(f"✓ {details['requested_version']} already released — nothing to do "
              f"(mode: {details['mode']}); current version: {details['current_version']}")
    _print_effects(out)


def cmd_clog_release(args):
    """`dct clog release` — an alias of `dct release apply` (K-72).

    Flags map 1:1 (version or --patch/--minor/--major, --entries /
    --no-entries, --proposal-digest, and the COMPLETE approved push target
    --push-remote + --push-branch + --push-url, or --no-push — K-123, as
    for apply); --confirm is REQUIRED as for apply (K-96 D1:
    the alias commits, tags and pushes — running it is not the
    confirmation), and so is --proposal-digest for a new release (K-117:
    the alias never prepares or approves a proposal by itself — run `dct
    release prepare` and approve its proposal_sha256).
    Decisions that need the owner are REFUSED instead of
    guessed: a missing proposal digest, unproven entries (no --entries =
    every unreleased entry, all of which must be proven), a proof range
    that does not start at v<current_version>. No --plan-digest: the plan
    is derived inside the call for the candidate it pins, the stamp is
    bound to it the same way (K-80/K-81), and the approved proposal must
    have been prepared for exactly that plan (K-116 H2). Effects, order
    and verification: see dct.workflows.release.effects.
    """
    _apply_from_args(args, strict=True)


def cmd_release_apply(args):
    """`dct release apply` — one release from the APPROVED plan and the
    APPROVED proposal (effects layer, K-80 / K-76): --plan-digest is the
    snapshot_digest of `dct release plan` with the same version and
    selection; the plan is re-derived for the candidate apply pins and a
    different snapshot is refused before any write. --proposal-digest is the
    proposal_sha256 of `dct release prepare` (required for a new release).

    `--consent C`: the one-dialog release (effects.apply_consent) — the
    screen of `prepare --preview` whose consent_digest is C; every binding
    flag of the path above is refused with it (`consent_flag_conflict`)."""
    if args.consent is not None:
        return _apply_consent_from_args(args)
    parser = args.apply_parser
    if args.plan_digest is None:
        parser.error("the following arguments are required: --plan-digest (or --consent)")
    if args.entries is None and not args.no_entries:
        parser.error("one of the arguments --entries --no-entries is required (or --consent)")
    _apply_from_args(args, strict=False)


# The flags of the D/P path that bind a release — `apply --consent` takes
# none of them: the preview's consent view binds them all (R1).
_CONSENT_BINDING_FLAGS = (("version", "--version"), ("entries", "--entries"),
                          ("no_entries", "--no-entries"), ("plan_digest", "--plan-digest"),
                          ("proposal_digest", "--proposal-digest"),
                          ("push_remote", "--push-remote"), ("push_branch", "--push-branch"),
                          ("push_url", "--push-url"), ("no_push", "--no-push"))


def _apply_consent_from_args(args) -> None:
    from dct.workflows.release.effects import (
        NOTHING_MADE,
        ReleaseRefused,
        apply_consent,
    )

    _refuse_removed_flag(args)
    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    conflicts = _prepare_flags_in_use(args, _CONSENT_BINDING_FLAGS)
    if args.bump is not None:
        conflicts.append(f"--{args.bump}")

    def call() -> dict:
        if conflicts:
            raise ReleaseRefused(
                f"--consent releases exactly the screen the owner approved — it takes no "
                f"{', '.join(sorted(conflicts))} (the preview binds the version, the entries "
                f"and the push target); preview again with them instead; nothing done.",
                details={"consent_flag_conflict": sorted(conflicts),
                         "made": dict(NOTHING_MADE)})
        return apply_consent(engine, project_slug, args.consent, confirm=args.confirm)

    out = _run_effects("apply", project_slug, args.json, call)
    if args.json:
        return
    details = out["details"]
    made = details["made"]
    for name, label in (("work_commit", "your changes committed as"),
                        ("attest_commit", "entries attested in")):
        if made[name]:
            print(f"  {label} {made[name][:12]}")
    print(f"✓ Released {details['released']} entries as {out['version']} "
          f"(mode: {details['mode']}) — current version: {details['current_version']}")
    target = details.get("push_target")
    print(f"  pushed to {target['remote']} {target['url']} ({target['branch']}, "
          f"{target['tag']})" if target else "  not pushed (a local release)")
    _print_effects(out)


_PREVIEW_ONLY_FLAGS = (("attest", "--attest"), ("message_file", "--message-file"),
                       ("include_untracked", "--include-untracked"),
                       ("push_remote", "--push-remote"), ("push_branch", "--push-branch"),
                       ("push_url", "--push-url"), ("no_push", "--no-push"),
                       ("no_variants", "--no-variants"))


def _prepare_flags_in_use(args, names) -> list[str]:
    """The flags of `names` ((dest, flag) pairs) the command line GAVE —
    by presence, never truthiness: a value flag is absent only as None
    (its default; `--version ""` is present), a store_true flag only as
    False (its default). An empty value never reads as "no flag"."""
    return [flag for name, flag in names
            if (value := getattr(args, name, None)) is not None and value is not False]


def cmd_release_prepare(args):
    """`dct release prepare` — the PREPARE stage before the stamp (K-76):
    the version-sync script runs once, isolated (a temporary worktree of
    the pinned candidate, or a copy of a project outside git), and its
    result is stored as the project's pending version-file proposal for the
    owner to approve (`apply --proposal-digest`). Nothing is stamped.

    `--preview`: the one-dialog release's screen (effects.preview_release —
    the consent view of each variant and its digest, stored in the
    project's preview slot; nothing committed). `--discard-preview`: clear
    that slot (effects.discard_preview)."""
    parser = args.prepare_parser
    if args.discard_preview:
        extra = _prepare_flags_in_use(args, (
            ("version", "--version"), ("bump", "--patch/--minor/--major"),
            ("entries", "--entries"), ("no_entries", "--no-entries"), *_PREVIEW_ONLY_FLAGS))
        if extra:
            parser.error(f"--discard-preview takes no other flag than -P and --json "
                         f"(got {', '.join(extra)})")
        return _discard_preview_from_args(args)
    if args.preview:
        return _preview_from_args(args)
    extra = _prepare_flags_in_use(args, _PREVIEW_ONLY_FLAGS)
    if extra:
        parser.error(f"{', '.join(extra)} only with --preview")
    if args.plan_digest is None:
        parser.error("the following arguments are required: --plan-digest (or --preview)")
    if args.entries is None and not args.no_entries:
        parser.error("one of the arguments --entries --no-entries is required (or --preview)")
    from dct.workflows.release.effects import prepare_release

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    version = args.version or None
    if not version and not args.bump:
        print("✗ Provide a version or --patch/--minor/--major", file=sys.stderr)
        sys.exit(1)
    entry_ids = None if args.no_entries else _release_ids(args.entries)
    out = _run_effects("prepare", project_slug, args.json, lambda: prepare_release(
        engine, project_slug, version=version, bump_type=None if version else args.bump,
        entry_ids=entry_ids, no_entries=args.no_entries, plan_digest=args.plan_digest))
    if args.json:
        return
    details = out["details"]
    contract = details["contract"]
    proposal = details["proposal"]
    print(f"✓ Prepared {out['version']} — version files: "
          f"{', '.join(proposal['paths']) or '(none)'}")
    if contract["change"]:
        print(f"  contract: {contract['confirmed'] if contract['confirmed'] is not None else '(not learned yet)'}"
              f" → {contract['proposed']} — approving the proposal records it")
    for check in details["checks"]:
        extra = check.get("value") or f"{check.get('changed_lines')} changed line(s)"
        print(f"  {check['path']}: {check['check']} ({extra})")
    sync = details["sync"]
    if sync and sync["output"]:
        dropped = sync["output_dropped_bytes"]
        print(f"  sync output ({sync['output_bytes']} bytes"
              f"{f', {dropped} dropped' if dropped else ''}):")
        for line in sync["output"].splitlines():
            print(f"    {line}")
    print(f"  proposal digest {details['proposal_sha256']}")
    target = details.get("push_target")
    print(f"  push target (default): {target['remote']} {target['branch']} ({target['url']}), "
          f"tag {target['tag']}" if target else
          "  push target: none by default — apply needs --push-remote … --push-branch … "
          "--push-url … or --no-push (see the warnings)")
    push = (f"--push-remote {shlex.quote(target['remote'])} --push-branch "
            f"{shlex.quote(target['branch'])} --push-url {shlex.quote(target['url'])}"
            if target else "(--push-remote … --push-branch … --push-url … | --no-push)")
    print(f"  next: dct release apply --version {out['version']} … --plan-digest "
          f"{details['plan_digest']} --proposal-digest {details['proposal_sha256']} {push} "
          f"--confirm")
    _print_effects(out)


def _discard_preview_from_args(args) -> None:
    from dct.workflows.release.effects import discard_preview

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    out = _run_effects("prepare", project_slug, args.json,
                       lambda: discard_preview(engine, project_slug))
    if args.json:
        return
    discarded = out["details"]["discarded"]
    print(f"✓ Discarded the preview {discarded}" if discarded else
          "✓ No preview to discard — nothing changed")
    _print_effects(out)


def _preview_from_args(args) -> None:
    from dct.workflows.release.effects import preview_release

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    entry_ids = None if args.no_entries else _release_ids(args.entries)
    attest_ids = _release_ids(args.attest, "--attest")
    out = _run_effects("prepare", project_slug, args.json, lambda: preview_release(
        engine, project_slug, version=args.version or None,
        bump_type=None if args.version else args.bump, entry_ids=entry_ids,
        no_entries=args.no_entries, attest_ids=attest_ids, message_file=args.message_file,
        include_untracked=args.include_untracked or (), push_remote=args.push_remote,
        push_branch=args.push_branch, push_url=args.push_url, no_push=args.no_push,
        variants=not args.no_variants))
    if args.json:
        return
    details = out["details"]
    work = details["work"]
    if work:
        print(f"Work in progress ({work['subject']}):")
        for item in work["paths"]:
            print(f"  {item['change']:<8} {item['path']} (staged: {item['staged']})")
        if work["untracked_left"]:
            print(f"  left out (untracked): {', '.join(work['untracked_left'])}")
    for item in details["variants"]:
        view, display = item["consent"], item["display"]
        selected = ", ".join(f"#{e['id']}" for e in display["entries"]["selected"]) or "none"
        waiting = ", ".join(f"#{e['id']} ({e['reason']})"
                            for e in display["entries"]["waiting"]) or "none"
        push = view["push"]
        where = ("not pushed" if push is None else
                 f"pushed to {push['remote']} {push['branch']} ({push['url']})"
                 if push["mode"] == "push" else f"not pushed ({push['reason']})")
        print(f"{item['name']}: {view['version']['target']} — entries {selected}; waiting "
              f"{waiting}; {where}")
        print(f"  consent digest {item['consent_digest']}")
    if details["all_unavailable"]:
        print(f"  no \"with all\" variant: {details['all_unavailable']}")
    _print_effects(out)


def cmd_release_resume(args):
    """`dct release resume` — finish the unfinished release record. No
    --confirm (K-97 F4): it executes the consent stored at apply (version,
    entries, candidate, proposal, push target) and refuses whatever lies
    outside it."""
    from dct.workflows.release.effects import resume_release

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    _refuse_removed_flag(args)
    out = _run_effects("resume", project_slug, args.json, lambda: resume_release(
        engine, project_slug))
    if args.json:
        return
    if out["outcome"] == "noop":
        print("✓ No unfinished release — nothing to resume")
    else:
        print(f"✓ Release {out['version']} completed")
    _print_effects(out)


def cmd_release_attest(args):
    """`dct release attest --entries … --confirm` — the empty attestation commit."""
    from dct.workflows.release.effects import attest

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    ids = _release_ids(args.entries)
    out = _run_effects("attest", project_slug, args.json, lambda: attest(
        engine, project_slug, ids or [], confirm=args.confirm))
    if args.json:
        return
    details = out["details"]
    print(f"✓ Attested {', '.join(f'#{i}' for i in details['entry_ids'])} in "
          f"{details['head']} — the release candidate is now this commit")


def cmd_release_abandon(args):
    """`dct release abandon --version V --reason … --confirm` (K-60)."""
    from dct.workflows.release.effects import abandon

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    out = _run_effects("abandon", project_slug, args.json, lambda: abandon(
        engine, project_slug, args.version, args.reason or "", confirm=args.confirm))
    if args.json:
        return
    record = out["release"]
    if out["outcome"] == "noop":
        print(f"✓ Release {record['version']} was already abandoned ({record['abandon_reason']})")
        return
    print(f"✓ Release {record['version']} abandoned — the record, the stamped entries and "
          f"current_version stay; release the work under a new version")


def _print_scope(scope: dict) -> None:
    """The recorded scope of a reconstruction (K-110), one line per fact."""
    if scope.get("adapter") == "own":
        print(f"  verified (own adapter): artifacts {scope['artifacts']}, commit "
              f"{'yes' if scope['commit'] else 'no'}, tag {'yes' if scope['tag'] else 'no'}")
    else:
        contract = scope["contract"]
        shown = "UNKNOWN (never confirmed)" if contract is None else (
            "confirmed: none" if not contract else "confirmed: " + ", ".join(contract))
        print(f"  contract:       {shown}")
        print(f"  verified:       changelog {scope['changelog']}, history {scope['history']}, "
              f"version files {scope['version_files']}, commit "
              f"{'yes' if scope['commit'] else 'no'}, tag {'yes' if scope['tag'] else 'no'}")
    if scope["limited"]:
        print("  scope:          LIMITED — no changelog section and no version file verified")


def cmd_release_reconstruct(args):
    """`dct release reconstruct --version V [--release-commit SHA |
    --evidence FILE] [--confirm]` (K-60, legacy releases; K-109, K-110,
    K-119: the own adapter takes --evidence)."""
    from dct.workflows.release.effects import reconstruct

    # The preview (no --confirm) writes nothing — it reads through the
    # read-only engine (K-84); only --confirm bootstraps.
    engine = _get_engine() if args.confirm else _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)

    def call():
        supplied = None
        if args.evidence:
            try:
                with open(args.evidence, encoding="utf-8") as fh:
                    supplied = _json_arg("--evidence", fh.read())
            except OSError as exc:
                raise ValueError(f"--evidence: cannot read {args.evidence}: {exc}") from None
        return reconstruct(engine, project_slug, args.version, confirm=args.confirm,
                           release_commit=args.release_commit or None, evidence=supplied)

    out = _run_effects("reconstruct", project_slug, args.json, call)
    if args.json:
        return
    evidence = out["details"]["evidence"]
    print(f"Evidence for {evidence['version']}:")
    print(f"  entries:        {', '.join(f'#{i}' for i in evidence['entry_ids']) or '(none)'}")
    print(f"  candidate:      {evidence['candidate_sha'] or '-'}")
    print(f"  release commit: {evidence['release_commit_sha'] or '-'}")
    print(f"  tag:            {evidence['tag'] or '-'}")
    manifest = evidence["manifest"]
    if manifest.get("changelog"):
        print(f"  changelog:      {manifest['changelog']['path']} "
              f"({manifest['changelog']['section_date']})")
    files = manifest.get("version_files", manifest.get("artifacts") or [])
    print(f"  {'artifacts' if 'artifacts' in manifest else 'version files'}:  "
          f"{', '.join(f['path'] for f in files) or '-'}")
    _print_scope(evidence["verification"])
    for other in evidence.get("candidates") or []:
        if other["commit"] != evidence["release_commit_sha"]:
            print(f"  other candidate: {other['commit']} — "
                  f"{'verified (not used)' if other['verified'] else other['reason']}")
    print(f"  stage proven:   {evidence['status']}")
    # M9: the executor is an assignment made NOW, never historical provenance.
    print(f"  responsibility: {out['details']['executor']['assigned']} — the executor "
          f"ASSIGNED to the record for its further handling (the project's current setting), "
          f"not who made the release")
    for warning in out["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)
    if out["outcome"] == "preview":
        print("  (preview — nothing written; re-run with --confirm to record it)")
    else:
        print(f"✓ Release {out['version']} recorded as {out['release']['status']} "
              f"(mode: reconstructed)")
    for way in out["details"]["finish"]:
        print(f"  to finish ({way['way']}): {way['command']}")
        print(f"      {way['what']}")


def cmd_release_takeover(args):
    """`dct release takeover --version V [push flags | --no-push]
    [--takeover-digest D --confirm]` (K-119 M3, K-117 J4) — dct takes over
    an existing record without a consent from its VERIFIED artifacts."""
    from dct.workflows.release.effects import takeover

    engine = _get_engine() if args.confirm else _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)
    out = _run_effects("takeover", project_slug, args.json, lambda: takeover(
        engine, project_slug, args.version, confirm=args.confirm,
        takeover_digest=args.takeover_digest or None, push_remote=args.push_remote,
        push_branch=args.push_branch, push_url=args.push_url, no_push=args.no_push))
    if args.json:
        return
    details = out["details"]
    consent, taken = details["consent"], details["consent"]["takeover"]
    print(f"Takeover of release {out['version']} (record: {details['record_status']}):")
    print(f"  entries:        {', '.join(f'#{i}' for i in consent['entry_ids']) or '(none)'}")
    print(f"  candidate:      {consent['candidate_sha']}")
    print(f"  release commit: {taken['release_commit_sha']} (tree {taken['tree']})")
    print(f"  artifacts:      sha256 {taken['artifacts_sha256']} — "
          f"{', '.join(details['proposal']['paths']) or 'no version files'}")
    tag = details["evidence"]["tag_proof"]
    print(f"  tag:            {tag['name'] + ' (exists, verified)' if tag else 'missing'}")
    target = consent["push_target"]
    print(f"  push target:    {target['remote']} {target['branch']} ({target['url']}), tag "
          f"{target['tag']}" if target else "  push target:    none (--no-push: finished locally)")
    print(f"  proven stage:   {details['stage']}; record after the takeover: "
          f"{details['status_after']}")
    print(f"  still missing:  {', '.join(details['missing_effects']) or 'nothing'} "
          f"(performed by `dct release resume`)")
    print("  responsibility: dct — assigned by this takeover, not who made the release")
    if out["outcome"] == "preview":
        print("  (preview — nothing written: no bump, no re-stamp, no recorded value changes)")
        print(f"  next: dct release takeover --version {out['version']} … --takeover-digest "
              f"{details['takeover_digest']} --confirm   (same push flags)")
    else:
        print(f"✓ Release {out['version']} taken over by dct — next: dct release resume")
    _print_effects(out)


def cmd_release_version_files(args):
    """`dct release version-files [--set PATHS | --none] [--confirm]`
    (K-110) — show / confirm the project's version-file contract."""
    from dct.workflows.release.effects import version_files_contract

    writes = args.confirm and (args.set is not None or args.none)
    engine = _get_engine() if writes else _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)
    paths = None
    if args.none:
        paths = []
    elif args.set is not None:
        paths = [part.strip() for part in args.set.split(",") if part.strip()]
        if not paths:
            print("✗ --set needs at least one path (use --none for a project without "
                  "version files)", file=sys.stderr)
            sys.exit(1)
    if args.confirm and paths is None:
        print("✗ --confirm needs --set <paths> or --none", file=sys.stderr)
        sys.exit(1)
    out = _run_effects("version-files", project_slug, args.json, lambda: version_files_contract(
        engine, project_slug, paths=paths, confirm=args.confirm))
    if args.json:
        return
    contract = out["details"]["contract"]

    def shown(files):
        return "UNKNOWN (never confirmed)" if files is None else (
            "none (confirmed: the project has no version files)" if not files
            else ", ".join(files))

    print(f"Version-file contract of {project_slug}: {shown(contract['version_files'])}"
          + (f" — confirmed {contract['confirmed_at']}" if contract["confirmed_at"] else ""))
    if out["outcome"] == "preview":
        print(f"  proposed:       {shown(out['details']['proposed'])}"
              f"{'' if out['details']['change'] else ' (no change)'}")
        print("  (preview — nothing written; re-run with --confirm to confirm it)")
    elif out["outcome"] == "confirmed":
        print("✓ Contract confirmed")
    elif out["outcome"] == "noop":
        print("✓ Contract unchanged (already confirmed)")
    elif out["outcome"] == "none":
        print("  confirm it: dct release version-files --set <path>[,<path>…] --confirm   or   "
              "dct release version-files --none --confirm")


def _print_release_record(record: dict) -> None:
    from dct.core.releases import proof_pr_origins

    ids = ", ".join(f"#{i}" for i in record["entry_ids"]) or "(none — version only)"
    print(f"Release {record['version']}: {record['status']} (mode: {record['mode']})")
    print(f"  entries:       {ids}")
    print(f"  candidate sha: {record['candidate_sha'] or '-'}")
    print(f"  release commit: {record['release_commit_sha'] or '-'}")
    tag, push = record.get("tag_proof"), record.get("push_proof")
    print(f"  tag proof:     {tag['name']} → {tag['commit']}"
          f"{' (annotated)' if tag['annotated'] else ''}, recorded {tag['recorded_at']}"
          if tag else "  tag proof:     -")
    print(f"  push proof:    {push['target']['remote']} {push['target']['branch'] or '-'} "
          f"{push['target']['tag']} → {push['tag_commit']}, recorded {push['recorded_at']}"
          if push else "  push proof:    -")
    print(f"  checkout root: {record['checkout_root'] or '-'}")
    for dup in record.get("duplicates") or []:
        print(f"  DUPLICATE:     record #{dup['id']} ({dup['version']}, {dup['status']}) — "
              f"the ledger is ambiguous; every write of this version is refused")
    manifest = record["manifest"] or {}
    print(f"  manifest:      {', '.join(sorted(manifest)) or '-'}")
    proof = record["proof"] or {}
    print(f"  proof:         {len(proof)} of {len(record['entry_ids'])} entries"
          if proof else "  proof:         -")
    # K-116 H7: a PR number is shown WITH its origin — a client's declaration
    # is never presented as one recognised in git.
    origins = {"git": "recognised in git (the entry's merge)", "client": "client attestation"}
    for entry_id, ref in proof_pr_origins(proof).items():
        print(f"    #{entry_id}: PR #{ref['pr']} — "
              f"{origins.get(ref['pr_source'], 'origin not recorded')}")
    print(f"  created:       {_fmt_date(record['created_at'])}")
    print(f"  updated:       {_fmt_date(record['updated_at'])}")
    if record["completed_at"]:
        print(f"  completed:     {_fmt_date(record['completed_at'])}")
    if record["abandoned_at"]:
        print(f"  abandoned:     {_fmt_date(record['abandoned_at'])} — {record['abandon_reason']}")


def cmd_clog_release_status(args):
    """`dct clog release-status` — read a release record (read-only, K-41).

    --version V → that release's record, whatever its status; without it →
    the latest unfinished (neither completed nor abandoned) record. Mirrors
    the get_release MCP tool.
    """
    from dct.core.releases import get_release

    engine = _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        record = get_release(engine, project_slug, args.version or None)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    if getattr(args, "json", False):
        import json

        from dct.workflows.release.effects import result

        print(json.dumps(result("status", project_slug, "found" if record else "none",
                                version=record["version"] if record else None,
                                release=record), indent=2, default=_json_default))
        return
    if record is None:
        print(f"No release record for {args.version}" if args.version
              else "No unfinished release")
        return
    _print_release_record(record)


def _json_arg(flag: str, raw: str):
    """Parse a CLI argument that carries JSON; ValueError naming the flag
    for text that is not JSON (its SHAPE is validated by the core)."""
    import json

    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{flag} must be a JSON object: {exc}") from None


def cmd_clog_release_advance(args):
    """`dct clog release-advance V STATUS` — the LEDGER path (K-96 D10): an
    adapter of effects.record_progress — core advance_release as the actor
    'ledger' (state machine, write-once fields, the release commit rule),
    under the project operation lock. For a release dct does not execute (a
    project's own tool, a record stamped outside `dct release apply`); a
    release dct executes is refused (`dct release resume` records its
    progress). candidate_sha is immutable — not accepted; the manifest is
    written by the release workflow. --tag-proof / --push-proof (K-115 G4)
    take the JSON identities the caller verified — the ledger requires them
    for tagged / pushed / completed of a record with git. Errors exit 1 (a
    lost serialization race 75)."""
    from dct.workflows.release.effects import record_progress

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    as_json = getattr(args, "json", False)

    def advance():
        proofs = {}
        for name in ("tag_proof", "push_proof"):
            raw = getattr(args, name, None)
            if raw:
                proofs[name] = _json_arg(f"--{name.replace('_', '-')}", raw)
        return record_progress(engine, project_slug, args.version, args.status,
                               release_commit_sha=args.release_commit_sha or None, **proofs)

    out = _run_effects("advance", project_slug, as_json, advance)
    if as_json:
        return
    record = out["release"]
    if not out["details"]["updated"]:
        print(f"✓ Release {record['version']} unchanged: {record['status']}")
        return
    print(f"✓ Release {record['version']}: {out['details']['previous_status'] or '?'} → "
          f"{record['status']}")


def cmd_clog_release_abandon(args):
    """`dct clog release-abandon V --reason "…" --confirm` — an adapter of
    `dct release abandon` (K-96 D10): the same effects.abandon (core
    abandon_release under the project operation lock). Errors exit 1."""
    cmd_release_abandon(args)


def _proof_layer_error(e: Exception) -> None:
    """Exit 1 with a one-line reason for a failed read-only plan / candidates."""
    print(f"✗ Error: {e}", file=sys.stderr)
    sys.exit(1)


def _print_entry_line(entry: dict, extra: str = "") -> None:
    where = f" @ {entry['branch']}" if entry.get("branch") else ""
    flag = " [uncertain]" if entry.get("uncertain") else ""
    print(f"  #{entry['id']} [{entry['category']}]{where}{flag} {entry['entry'][:60]}{extra}")


def cmd_release_plan(args):
    """`dct release plan` — the read-only proof layer (K-72 layer 1).

    Prints the plan of the release cut from the CURRENT checkout (verified
    strictly, K-52) — or, with --json, the stable JSON document
    (dct.workflows.release.PLAN_KEYS; schema in docs/release-workflow.md).
    Writes nothing: no database row, no ref, index or work-tree change.
    """
    import json

    from dct.workflows.release import build_plan

    engine = _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)
    entry_ids = None if args.no_entries else _release_ids(args.entries)
    try:
        plan = build_plan(engine, project_slug, version=args.version or None,
                          bump_type=None if args.version else args.bump,
                          entry_ids=entry_ids, no_entries=args.no_entries)
    except Exception as e:
        _proof_layer_error(e)
    if args.json:
        print(json.dumps(plan, indent=2, default=_json_default))
        return
    version = plan["version"]
    print(f"Release plan for {plan['project']} ({plan['kind']}): "
          f"{version['current'] or '-'} → {version['target'] or '(no target version given)'}")
    routing = plan["executor"]
    print(f"  executor: {routing['project']} (the project's setting — it decides a NEW release)")
    if plan["unfinished_release"]:
        rec = plan["unfinished_release"]
        print(f"  unfinished release {rec['version']}: {rec['status']} — resume or abandon it")
        owned = routing["record"]
        # K-119: an unfinished release is routed by its RECORD.
        print(f"    recorded executor: {owned['executor'] or 'dct (not recorded)'}; progress "
              f"recorded by: {owned['progress_owner']}; consent: "
              f"{'recorded' if owned['consent'] else 'none'}")
    checkout = plan["checkout"]
    resume = plan["resume"]
    if not checkout["git"]:
        print(f"  checkout: none ({checkout['reason']})")
    elif resume is not None:
        # K-95 C5: the RECORDED release first; today's HEAD separately.
        print(f"  recorded candidate: {resume['candidate_sha'] or '-'} — entries "
              f"{', '.join(f'#{i}' for i in resume['entry_ids']) or '(version-only)'}; "
              f"status {resume['status']}")
        done = [k for k, v in resume["effects"].items() if v]
        print(f"  effects recorded: {', '.join(done) or '-'}")
        print(f"  current HEAD: {resume['head']} on {checkout['branch'] or '(detached HEAD)'}"
              f"{'' if resume['head_is_candidate'] else ' — NOT the recorded candidate'}")
    else:
        print(f"  candidate: {checkout['candidate_sha']} on "
              f"{checkout['branch'] or '(detached HEAD)'} in {checkout['repo_root']}")
        rng = plan["range"]
        print(f"  range: {rng['display'] or '(empty)'} — {rng['commit_count']} commits"
              f"{' [confirm the base]' if rng['confirm_required'] else ''}"
              f"{' [approximate]' if rng['approximate'] else ''}")
        for alt in rng["alternatives"]:
            print(f"    equally near: {alt['name']} ({alt['sha'][:12]})")
    if resume is not None:
        new = resume["new_unreleased"]
        print(f"New unreleased entries since the record ({len(new)}) — not part of it:")
        for entry in new:
            _print_entry_line(entry, "")
        for warning in plan["warnings"]:
            print(f"  ⚠ {warning}", file=sys.stderr)
        return
    proven, unproven = plan["entries"]["proven"], plan["entries"]["unproven"]
    print(f"Proven ({len(proven)}):")
    for entry in proven:
        merge = f" via {entry['merge_commit'][:12]}" if entry["merge_commit"] else ""
        pr = f" (#{entry['pr']})" if entry["pr"] else ""
        pr += " [several merges — ambiguous]" if entry.get("ambiguous") else ""
        _print_entry_line(entry, f" ← {', '.join(c[:12] for c in entry['commits'])}{merge}{pr}")
    print(f"Unproven ({len(unproven)}):")
    for entry in unproven:
        _print_entry_line(entry, f" — {entry['reason']}")
    for i, group in enumerate(plan["attestation_groups"]):
        print(f"Attestation proposal {i} ({group['branch'] or 'unbranched'}): "
              f"{', '.join(f'#{n}' for n in group['entry_ids'])} — "
              f"squash {', '.join(c[:12] for c in group['squash_commits'])} "
              f"[{', '.join(group['signals'])}]")
    for cite in plan["foreign_citations"]:
        print(f"  cited #{cite['id']}: {cite['reason']}"
              f"{' (' + cite['version'] + ')' if cite['version'] else ''} — not stamped")
    index = plan["index"]
    if index is not None:
        dirty = [a["path"] for a in index["release_artifacts"]
                 if a["staged"] or a["unstaged"] or a["conflicted"]]
        print(f"Index: {'clean' if index['clean'] else 'NOT clean'}"
              f"{' — release artifacts changed: ' + ', '.join(dirty) if dirty else ''}")
    files = (plan["version_files"] or {}).get("version_files")
    print(f"Version files: {', '.join(files) if files else '(not learned yet)' if files is None else '(none)'}")
    if plan["snapshot"] is not None:
        selected = plan["snapshot"]["selected_ids"]
        print(f"Snapshot: {', '.join(f'#{i}' for i in selected) or '(version-only)'} → "
              f"{plan['version']['target'] or '-'}; plan digest {plan['snapshot_digest']}")
        print(f"Next: dct release prepare --version {plan['version']['target'] or 'V'} "
              f"{'--entries ' + ','.join(map(str, selected)) if selected else '--no-entries'} "
              f"--plan-digest {plan['snapshot_digest']} — runs the version sync isolated and "
              f"shows the version files for approval")
    for warning in plan["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)


def cmd_clog_candidates(args):
    """`dct clog candidates` — /commit's changelog candidates (K-53), read-only.

    confirmed (client provenance of THIS checkout and branch) vs uncertain
    (server / no provenance, or any entry on a detached HEAD — never
    attached automatically), minus the ids the range already proves, plus
    the staged file list and the changed files whose path looks like a
    secret (the release preview's predicate). --json prints the stable document
    (dct.workflows.release.CANDIDATE_KEYS).
    """
    import json

    from dct.workflows.release import commit_candidates

    engine = _get_readonly_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        result = commit_candidates(engine, project_slug)
    except Exception as e:
        _proof_layer_error(e)
    if args.json:
        print(json.dumps(result, indent=2, default=_json_default))
        return
    checkout = result["checkout"]
    if checkout["git"]:
        print(f"Checkout: {checkout['toplevel']} on "
              f"{checkout['branch'] or '(detached HEAD)'} at {checkout['head'] or '-'}")
    for title, key in (("Confirmed", "confirmed"), ("Uncertain — confirm before attaching",
                                                    "uncertain"),
                       ("Other branch in this worktree", "other_branch")):
        print(f"{title} ({len(result[key])}):")
        for entry in result[key]:
            reason = entry.get("uncertain_reason")
            _print_entry_line(entry, f" — {reason}" if reason else "")
    if result["already_proven"]:
        print(f"Already proven in the range: "
              f"{', '.join(f'#{i}' for i in result['already_proven'])}")
    print(f"Staged files ({len(result['staged_files'])}): {', '.join(result['staged_files'])}")
    for item in result["secret_like"]:
        if item["staged"]:
            print(f"  ⚠ {item['path']} looks like a secret and is staged — unstage it: "
                  f"{item['unstage']}")
        else:
            print(f"  ⚠ {item['path']} looks like a secret — never staged by a commit dialog")
    for warning in result["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)


def cmd_clog_update(args):
    from dct.core.changelog import update_entry

    engine = _get_engine()
    # An empty value ("") clears a nullable field; entry/category cannot be
    # cleared (core refuses an empty entry). --item-id 0 unlinks the item.
    fields = {}
    for name in ("entry", "category"):
        if getattr(args, name) is not None:
            fields[name] = getattr(args, name)
    for name in ("component", "source_file", "source_lines", "branch", "worktree"):
        value = getattr(args, name)
        if value is not None:
            fields[name] = value or None
    if args.item_id is not None:
        fields["related_item_id"] = args.item_id or None
    try:
        project_slug = args.project or _detect_project(engine)
        e = update_entry(engine, project_slug, args.id, **fields)
        print(f"✓ Changelog #{e['id']} updated: {', '.join(fields)}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_clog_delete(args):
    from dct.core.changelog import delete_entry

    engine = _get_engine()
    try:
        project_slug = args.project or _detect_project(engine)
        delete_entry(engine, project_slug, args.id)
        print(f"✓ Changelog #{args.id} deleted")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_clog_export(args):
    from dct.export import export_changelog_md

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    md = export_changelog_md(engine, project_slug)
    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(f"✓ Exported to {args.output}")
    else:
        print(md)


# ── Checkpoint commands ──────────────────────────────────────────────────────

def cmd_internal_task_mirror(args):
    """Internal task-mirror worker; read one native hook payload from stdin.

    Claude remains the default and preserves TaskCreate/TodoWrite append-only
    behavior. The Codex bridge supplies only successful update_plan events;
    --client codex selects its independent config gate and requires actual
    session_id plus turn_id. Native statuses are observations in checkpoint
    text, not authoritative task completion or bidirectional synchronization.

    Config ask/off modes, empty tasks and missing Codex identity return before
    database bootstrap. Empty project resolves from the process CWD; an
    unregistered directory is a silent no-op for this background worker.
    """
    import json

    from dct.config import load_config

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return  # fire-and-forget: swallow, exit clean

    client = getattr(args, "client", "claude")
    cfg = load_config()
    mode = getattr(cfg.integrations, client).task_mirror
    if client == "claude":
        mode = mode or "auto"  # preserve the legacy worker's empty-value fallback
    if mode != "auto":
        return
    from dct.workflows.task_mirror import mirror_tasks, tasks_from_payload

    if not tasks_from_payload(payload, client):
        return
    if client == "codex":
        identity = (args.session_id or payload.get("session_id"),
                    getattr(args, "turn_id", "") or payload.get("turn_id"))
        if not all(isinstance(value, str) and value.strip() for value in identity):
            return
    result = mirror_tasks(
        _get_engine(), payload, client=client, project=args.project,
        session_id=args.session_id, turn_id=getattr(args, "turn_id", ""),
    )
    if result:
        print(f"mirror ok: item #{result['item_id']}, {result['added']} checkpoint(s) added")


def cmd_config_path(args):
    """Print the resolved config/data/cache/state locations and their
    sources (env / platform default / legacy) — the discoverability
    companion of the dctools family dir (ADR #52, CP #1258)."""
    cfg_path = paths.default_config_path()
    if os.environ.get("DCT_CONFIG_PATH"):
        cfg_src = "DCT_CONFIG_PATH env"
    elif paths.is_legacy(cfg_path):
        cfg_src = "legacy ~/.claude — run `dct migrate-home`"
    else:
        cfg_src = "platform default"
    db_path = paths.default_db_path()
    if os.environ.get("DCT_DATA_DIR"):
        db_src = "DCT_DATA_DIR env"
    elif paths.is_legacy(db_path):
        db_src = "legacy ~/.claude — run `dct migrate-home`"
    else:
        db_src = "platform default"
    print(f"config: {cfg_path}  [{cfg_src}]")
    print(f"data:   {db_path}  [{db_src}]")
    print(f"cache:  {paths.cache_dir()}")
    print(f"state:  {paths.state_dir()}")
    if os.environ.get("DCT_DATABASE_URL"):
        print("database url: set via DCT_DATABASE_URL env (overrides everything)")


def cmd_config_get(args):
    """Read a dotted config key — e.g. `dct config get integrations.claude.task_mirror`.

    Supports: database.url, dct_root, plans.<field>, classification.<field>,
    integrations.claude.<field>, mirror.<field> (compat view), web.<field>,
    update.<field>, triage.<field>.
    Prints the value (or empty string for unset). Exit 1 on unknown key.
    """
    from dct.config import load_config

    cfg = load_config()
    key = args.key.strip()

    # Flat keys first
    if key == "database.url":
        print(cfg.database_url)
        return
    if key == "dct_root":
        print(cfg.dct_root)
        return

    # Nested dataclass access — arbitrary depth, so three-part paths like
    # integrations.claude.task_mirror resolve too (ADR #52 sections).
    parts = key.split(".")
    if len(parts) < 2:
        print(f"✗ Unknown config key: '{key}'", file=sys.stderr)
        sys.exit(1)

    obj = cfg
    for part in parts[:-1]:
        obj = getattr(obj, part, None)
        if obj is None:
            print(f"✗ Unknown config key: '{key}'", file=sys.stderr)
            sys.exit(1)
    field_name = parts[-1]
    if not hasattr(obj, field_name):
        print(f"✗ Unknown config key: '{key}'", file=sys.stderr)
        sys.exit(1)

    value = getattr(obj, field_name)
    if isinstance(value, bool):
        print(str(value).lower())
    elif value is None:
        print()  # unset (e.g. triage.template) — the docstring's empty string
    else:
        print(value)


def cmd_checkpoint_add(args):
    from dct.core.checkpoints import add_checkpoint

    engine = _get_engine()
    try:
        cp = add_checkpoint(engine, args.item_id, args.text, kind=args.kind)
        kind_tag = "" if cp.get("kind", "task") == "task" else f" [{cp['kind']}]"
        print(f"✓ Checkpoint #{cp['id']}{kind_tag} added to item #{args.item_id}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_checkpoint_list(args):
    from dct.core.checkpoints import get_progress, list_checkpoints

    engine = _get_engine()
    cps = list_checkpoints(engine, args.item_id)
    progress = get_progress(engine, args.item_id)

    headers = ["#", "Status", "Text"]
    icon = {"done": "✓", "pending": "·", "rejected": "✗"}
    rows = [[str(c["id"]), f"{icon.get(c['status'], '?')} {c['status']}", c["text"][:80]] for c in cps]
    _print_table(headers, rows, max_widths={2: 80})
    print(f"\nProgress: {progress['done']}/{progress['active']} done ({progress['percent']}%), {progress['rejected']} rejected")


def cmd_checkpoint_done(args):
    from dct.core.checkpoints import set_status

    engine = _get_engine()
    result = set_status(engine, args.checkpoint_id, "done")
    if result:
        print(f"✓ #{args.checkpoint_id} → done")
    else:
        print(f"✗ Checkpoint #{args.checkpoint_id} not found", file=sys.stderr)
        sys.exit(1)


def cmd_checkpoint_reject(args):
    from dct.core.checkpoints import set_status

    engine = _get_engine()
    result = set_status(engine, args.checkpoint_id, "rejected")
    if result:
        print(f"✗ #{args.checkpoint_id} → rejected")
    else:
        print(f"✗ Checkpoint #{args.checkpoint_id} not found", file=sys.stderr)
        sys.exit(1)


def cmd_checkpoint_reopen(args):
    from dct.core.checkpoints import set_status

    engine = _get_engine()
    result = set_status(engine, args.checkpoint_id, "pending")
    if result:
        print(f"↺ #{args.checkpoint_id} → pending")
    else:
        print(f"✗ Checkpoint #{args.checkpoint_id} not found", file=sys.stderr)
        sys.exit(1)


def cmd_checkpoint_delete(args):
    from dct.core.checkpoints import delete_checkpoint

    engine = _get_engine()
    if delete_checkpoint(engine, args.checkpoint_id):
        print(f"✓ Checkpoint #{args.checkpoint_id} soft-deleted")
    else:
        print(f"✗ Checkpoint #{args.checkpoint_id} not found", file=sys.stderr)
        sys.exit(1)


# ── Sprints ──────────────────────────────────────────────────────────────────

def cmd_sprint_list(args):
    from dct.core.sprints import list_sprints

    engine = _get_engine()
    project = args.project or _detect_project(engine)
    sprints = list_sprints(
        engine, project_slug=project,
        status=args.status or None, kind=args.kind or None,
    )
    headers = ["#", "Code", "Kind", "Status", "Started", "Ended", "Name"]
    rows = [
        [
            str(s["id"]), s.get("code") or "", s["kind"], s["status"],
            _fmt_date(s.get("started_at")), _fmt_date(s.get("ended_at")), s["name"],
        ]
        for s in sprints
    ]
    _print_table(headers, rows, max_widths={6: 50})


def cmd_sprint_show(args):
    from dct.core.sprints import get_sprint

    engine = _get_engine()
    s = get_sprint(engine, args.id)
    if not s:
        print(f"✗ Sprint #{args.id} not found", file=sys.stderr)
        sys.exit(1)

    print(f"#{s['id']}  {s['kind']}/{s['status']}")
    print(f"Name:     {s['name']}")
    if s.get("code"):
        print(f"Code:     {s['code']}")
    if s.get("goal"):
        print(f"Goal:     {s['goal']}")
    print(f"Started:  {_fmt_date(s.get('started_at'))}")
    print(f"Ended:    {_fmt_date(s.get('ended_at'))}")
    print(f"Created:  {_fmt_date(s['created_at'])}")


def cmd_sprint_create(args):
    from dct.core.sprints import create_sprint

    engine = _get_engine()
    project = args.project or _detect_project(engine)
    kwargs = {}
    if args.code:
        kwargs["code"] = args.code
    if args.kind:
        kwargs["kind"] = args.kind
    if args.status:
        kwargs["status"] = args.status
    if args.goal:
        kwargs["goal"] = args.goal
    try:
        s = create_sprint(engine, project, args.name, **kwargs)
        print(f"✓ Sprint #{s['id']} [{s['status']}] — {s['name']}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_sprint_update(args):
    """Handles `sprint update` and the defer/activate/complete verb aliases.

    The aliases set `_forced_status` via argparse defaults; the plain update
    subcommand carries the explicit --status/--name/... flags. getattr keeps
    both namespaces working through one code path."""
    from dct.core.sprints import update_sprint

    engine = _get_engine()
    kwargs = {}
    status = getattr(args, "_forced_status", None) or getattr(args, "status", None)
    if status:
        kwargs["status"] = status
    if getattr(args, "name", None):
        kwargs["name"] = args.name
    if getattr(args, "goal", None):
        kwargs["goal"] = args.goal
    if getattr(args, "code", None):
        kwargs["code"] = args.code
    if getattr(args, "kind", None):
        kwargs["kind"] = args.kind
    try:
        s = update_sprint(engine, args.id, **kwargs)
        if s is None:
            print(f"✗ Sprint #{args.id} not found", file=sys.stderr)
            sys.exit(1)
        print(f"✓ Sprint #{s['id']} → {s['status']} — {s['name']}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


# ── Tag dictionary (#611) ────────────────────────────────────────────────────

def cmd_tag_list(args):
    """The whole tag dictionary with usage counts — GLOBAL, not project-scoped
    (the dictionary is user-scoped by design; item lists stay project-scoped)."""
    from dct.core.tags import list_tags

    engine = _get_engine()
    rows_in = list_tags(engine)
    headers = ["#", "Name", "Items", "Created", "Description"]
    rows = [
        [
            str(t["id"]), t["name"], str(t["item_count"]),
            _fmt_date(t.get("created_at")), t.get("description") or "",
        ]
        for t in rows_in
    ]
    _print_table(headers, rows, max_widths={4: 50})


def cmd_tag_add(args):
    from dct.core.tags import add_tag

    engine = _get_engine()
    try:
        tag = add_tag(engine, args.name, description=args.description or None,
                      force=args.force)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    verb = "created" if tag["created"] else "exists"
    print(f"✓ Tag #{tag['id']} {verb} — {tag['name']}")


def cmd_tag_merge(args):
    from dct.core.tags import merge_tags

    engine = _get_engine()
    try:
        res = merge_tags(engine, args.from_name, args.into_name)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"✓ Merged '{res['from']}' → '{res['into']}' "
        f"({res['items_moved']} item(s) moved)"
    )


# ── Triage findings ledger (#909) ───────────────────────────────────────────

# Every triage command addresses its cycle the same way (#930 §8): the
# positional code within --project (else the CWD's project), or --cycle-id
# (checked against --project only when that is given). A code is never re-read
# as an id; the workflow's one resolver (resolve_cycle) decides.


def _require_one_triage_selector(args) -> None:
    """Exactly one of the positional code / --cycle-id, else exit 1 — before
    any database or file is touched."""
    if (args.cycle is None) == (args.cycle_id is None):
        print("✗ Error: give the cycle's code OR --cycle-id, not both or neither",
              file=sys.stderr)
        sys.exit(1)


def _triage_cycle_ref(args, engine) -> tuple:
    """(cycle, project) for a dct.workflows.triage call: (id, --project or
    None) or (code, --project or the CWD's project — exit 1 when undetected)."""
    if args.cycle_id is not None:
        return args.cycle_id, args.project
    return args.cycle, args.project or _detect_project(engine)


def _triage_undecided(seqs) -> str:
    return ", ".join(f"K-{s}" for s in seqs) or "none"


def cmd_triage_export(args):
    """`dct triage export (<cycle-code> | --cycle-id N)` — the triage page's
    constants (read-only).

    The cycle is its code within the project (--project, else the CWD's
    project) or, with --cycle-id, its global id (checked against --project
    only when that is given). A code is never re-read as an id (K-13):
    export_triage resolves both through the workflow's one resolver,
    resolve_cycle.

    Default: the three JavaScript constant declarations CLOSED / SUGGESTED /
    CARDS, ready to replace the template's slots, serialised by
    page_constants_json — safe inside an inline <script> whatever the texts
    hold (K-1). --json: the whole export {cycle, cards, closed, suggested}
    as plain JSON with the texts as recorded. `dct triage page` builds the
    whole page from the same export.
    """
    from dct.workflows.triage import export_triage, page_constants_json

    _require_one_triage_selector(args)
    engine = _get_readonly_engine()
    try:
        cycle, project = _triage_cycle_ref(args, engine)
        out = export_triage(engine, cycle, project=project)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        sys.stdout.write(_triage_export_json(out))
        return
    print(f"  const CLOSED = {page_constants_json(out['closed'])};")
    print(f"  const SUGGESTED = {page_constants_json(out['suggested'])};")
    print(f"  const CARDS = {page_constants_json(out['cards'])};")


def _triage_export_json(export: dict) -> str:
    """The export as `dct triage export --json` prints it — also the bytes of
    the cards.json `dct triage page` writes beside the page (texts as
    recorded, one trailing newline)."""
    import json

    return json.dumps(export, indent=2, ensure_ascii=False) + "\n"


# The companion files `dct triage page` keeps beside the page: the full
# export for consultation (owner decision 2026-10-21) and the plain layer.
_TRIAGE_CARDS_FILENAME = "cards.json"


# Safe file-name component of a cycle code (#930 Q2): anything outside
# [A-Za-z0-9._-] becomes '-', leading dots are dropped (no hidden or '..' name).
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _triage_page_filename(code: str) -> str:
    """`<safe code>.html` — the file name `dct triage page` uses when --out
    names a directory."""
    stem = _UNSAFE_FILENAME_CHARS.sub("-", code).lstrip(".")
    return f"{stem or 'triage'}.html"


def _triage_template_path(args) -> Path:
    """--template, else [triage] template of dct.toml (both ~-expanded), else
    the template dct ships (#991, triage_page.SHIPPED_TEMPLATE)."""
    raw = args.template
    if raw is None:
        raw = load_config().triage.template
    if not raw:
        from dct.workflows.triage_page import SHIPPED_TEMPLATE

        return SHIPPED_TEMPLATE
    return Path(raw).expanduser()


def _triage_page_target(out_arg: str, code: str, template: Path, force: bool) -> Path:
    """The file `dct triage page` writes. A directory → `<dir>/<safe code>.html`.
    Refused: a missing parent directory (never created), the template itself
    (also through a symlink), an existing path (a symlink too, even a
    dangling one) without --force, a directory at the target."""
    out = Path(out_arg).expanduser()
    if out.is_dir():
        out = out / _triage_page_filename(code)
    if not out.parent.is_dir():
        raise ValueError(f"{out.parent}: no such directory — create it first (--out is "
                         "never given missing parents)")
    if out.resolve() == template.resolve():
        raise ValueError(f"{out}: --out is the template itself — refusing to overwrite it")
    if out.is_dir():
        raise ValueError(f"{out}: is a directory")
    from dct.workflows.triage_page import PLAIN_FILENAME

    if out.name in (_TRIAGE_CARDS_FILENAME, PLAIN_FILENAME):
        raise ValueError(f"{out}: {out.name} is the name of a file kept beside the page — "
                         "name the page differently")
    if os.path.lexists(out) and not force:
        raise ValueError(f"{out} already exists — pass --force to replace it (a published "
                         "or closed page must never be overwritten by accident)")
    return out


def _triage_cards_target(page: Path, force: bool) -> Path:
    """`cards.json` beside the page, checked before anything is written: a
    directory there is refused, an existing path (a symlink too) needs
    --force, like the page."""
    target = page.parent / _TRIAGE_CARDS_FILENAME
    if target.is_dir() and not target.is_symlink():
        raise ValueError(f"{target}: is a directory")
    if os.path.lexists(target) and not force:
        raise ValueError(f"{target} already exists — pass --force to replace it (the "
                         "export beside a published page is never overwritten by accident)")
    return target


def _write_new_file(out: Path, text: str, force: bool) -> None:
    """Write `text` (UTF-8, bytes as built) to `out`. Without force the file
    is created exclusively (a file appearing meanwhile is not overwritten);
    with force a sibling temp file replaces `out` atomically — a failure
    (the write, or the chmod below) leaves any existing file as it was. A
    symlink at `out` is replaced by the file, never written through.

    Permissions (K-36): a new file — with or without force — is created
    with mode 0666 minus the process umask, as `open()` does (the umask is
    applied by the kernel, never read or changed here). With force onto an
    existing REGULAR file, the replacement keeps that file's rwx bits
    (0600 stays 0600); special bits (setuid / setgid / sticky) are not
    carried over, and a symlink's target is never consulted — the link is
    replaced by a file under the creation policy."""
    import secrets
    import stat

    data = text.encode("utf-8")
    if not force:
        try:
            with open(out, "xb") as stream:
                stream.write(data)
        except FileExistsError:
            raise ValueError(f"{out} already exists — pass --force to replace it") from None
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        return
    try:
        current = os.lstat(out)
    except FileNotFoundError:
        current = None
    keep_mode = (stat.S_IMODE(current.st_mode) & 0o777
                 if current is not None and stat.S_ISREG(current.st_mode) else None)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _attempt in range(100):
        name = out.parent / f".{out.name}.dct-{secrets.token_hex(6)}"
        try:
            fd = os.open(name, flags, 0o666)
            break
        except FileExistsError:
            continue
    else:
        raise ValueError(f"{out.parent}: no free temporary file name for the page")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if keep_mode is not None:
            os.chmod(name, keep_mode)
        os.replace(name, out)
    finally:
        name.unlink(missing_ok=True)


def _read_plain_file(path: Path) -> str:
    """The plain layer's text, strict UTF-8 (copied byte for byte later)."""
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"plain: {path} is not UTF-8 ({exc.reason} at byte {exc.start})") from None
    except OSError as exc:
        raise ValueError(f"plain: cannot read {path}: {exc.strerror or exc}") from None


def _triage_plain_target(page: Path, source: Path, force: bool) -> Path:
    """`plain.json` beside the page, checked before anything is written:
    a directory there is refused, an existing path (a symlink too) needs
    --force — unless it already is the source file itself."""
    from dct.workflows.triage_page import PLAIN_FILENAME

    target = page.parent / PLAIN_FILENAME
    if target.is_dir() and not target.is_symlink():
        raise ValueError(f"{target}: is a directory")
    if os.path.lexists(target) and not force and target.resolve() != source.resolve():
        raise ValueError(f"{target} already exists — pass --force to replace it (the "
                         "published plain layer is never overwritten by accident)")
    return target


def cmd_triage_page(args):
    """`dct triage page (<code> | --cycle-id N) --out <file>` — build a
    cycle's triage page from its export (#930 §3; read-only on the database).

    The template comes from --template, else [triage] template in dct.toml
    (else the template dct ships, #991). The page is built in memory by build_triage_page
    (dct/workflows/triage_page.py) from export_triage only; nothing is written
    unless the build succeeds, and an existing --out is replaced only with
    --force (atomically). --out naming a directory writes
    `<dir>/<safe code>.html`; an empty --out is refused before anything is
    read. Prints `<out>: <n> cards, closed=<yes|no>, suggested=<by>,
    digest=<hex>` (or `suggested=none`) — the digest of the very export the
    page holds.

    --plain <file> (#988): the plain-language layer. The file is validated
    against the same export (validate_plain_layer) BEFORE anything is
    written, then copied byte for byte as `plain.json` beside the page under
    the page's --force rule (a file already there is replaced only with
    --force; a source that already is that file is validated, not
    rewritten). Prints a further line `<dir>/plain.json: plain layer, <n>
    cards`.

    cards.json (owner decision 2026-10-21): every build also writes the
    export it was built from beside the page — the bytes `dct triage export
    --json` prints — so the cards, options, proposals and decisions can be
    handed to a consultant without reading the HTML. Same --force rule; every
    target (page, cards.json, plain.json) is checked before the first write,
    and --out may not be named cards.json or plain.json. Prints a second
    line `<dir>/cards.json: cards export, <n> cards`.
    """
    _require_one_triage_selector(args)
    if not args.out.strip():
        print("✗ Error: --out is empty — name the page file (or an existing directory)",
              file=sys.stderr)
        sys.exit(1)
    try:
        template_path = _triage_template_path(args)
        template_text = template_path.read_text(encoding="utf-8")
        plain_path = Path(args.plain).expanduser() if args.plain else None
        plain_text = _read_plain_file(plain_path) if plain_path else None
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    from dct.workflows.triage import export_triage
    from dct.workflows.triage_page import build_triage_page, validate_plain_layer

    engine = _get_readonly_engine()
    plain_out = plain_cards = None
    try:
        cycle, project = _triage_cycle_ref(args, engine)
        export = export_triage(engine, cycle, project=project)
        out = _triage_page_target(args.out, export["cycle"]["code"], template_path, args.force)
        page = build_triage_page(export, template_text, topic=args.topic,
                                 context=args.context, lede=args.lede)
        cards_out = _triage_cards_target(out, args.force)
        if plain_path is not None:
            plain_cards = validate_plain_layer(plain_text, export)
            plain_out = _triage_plain_target(out, plain_path, args.force)
        _write_new_file(out, page, args.force)
        _write_new_file(cards_out, _triage_export_json(export), args.force)
        if plain_out is not None and plain_out.resolve() != plain_path.resolve():
            _write_new_file(plain_out, plain_text, args.force)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    # The digest of THIS build's export (K-32): the one the owner will see on
    # the published page, to be passed to `dct triage accept --expected-digest`.
    suggested = (f"{export['suggested']['by']}, digest={export['suggested']['digest']}"
                 if export["suggested"] else "none")
    print(f"{out}: {len(export['cards'])} cards, "
          f"closed={'yes' if export['closed'] else 'no'}, suggested={suggested}")
    print(f"{cards_out}: cards export, {len(export['cards'])} cards")
    if plain_out is not None:
        print(f"{plain_out}: plain layer, {plain_cards} cards")


def cmd_triage_decide(args):
    """`dct triage decide (<code> | --cycle-id N) --from-text <file|->` — the
    OWNER's pasted decision text (the page's "Tekst do wklejenia") recorded
    through decide_findings_from_text: parsed first (a parse error writes
    nothing), then one transaction; only the cards the text decides change.
    A card line left with the no-decision label and no note is skipped
    (K-31) and listed; a starting-point line is stored as the decisions'
    text origin (K-40). Every language of the string table is read (#991)."""
    from dct.workflows.triage import decide_findings_from_text

    _require_one_triage_selector(args)
    engine = _get_engine()
    try:
        text = _read_text_arg(None, args.from_text)
        cycle, project = _triage_cycle_ref(args, engine)
        out = decide_findings_from_text(engine, cycle, text, decided_by=args.decided_by,
                                        decision_source=args.source, project=project)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Parsed {out['parsed']}, decided {out['decided']} — "
          f"undecided: {_triage_undecided(out['undecided'])}")
    if out["skipped"]:
        print(f"  skipped (no decision on the page, unchanged): {', '.join(out['skipped'])}")


def cmd_triage_accept(args):
    """`dct triage accept (<code> | --cycle-id N) --expected-by <who>
    --expected-digest <hex>` — the OWNER accepts the proposals they saw as
    final (accept_suggestions). The digest is SUGGESTED.digest of the export
    the published page was built from (K-32) — never one read just now: a
    proposal changed since then (even by the same author) refuses the whole
    acceptance, nothing written."""
    from dct.workflows.triage import accept_suggestions

    _require_one_triage_selector(args)
    engine = _get_engine()
    try:
        cycle, project = _triage_cycle_ref(args, engine)
        out = accept_suggestions(engine, cycle, expected_by=args.expected_by,
                                 expected_digest=args.expected_digest,
                                 decided_by=args.decided_by, decision_source=args.source,
                                 overwrite=args.overwrite, project=project)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Accepted {out['decided']} proposal(s) by {args.expected_by} — "
          f"undecided: {_triage_undecided(out['undecided'])}")
    if out["without_suggestion"]:
        print(f"  without a proposal (untouched): {', '.join(out['without_suggestion'])}")
    if out["already_decided"]:
        print(f"  replaced earlier decisions: {', '.join(out['already_decided'])}")


def cmd_triage_close(args):
    """`dct triage close (<code> | --cycle-id N) --source <where> [--note …]…`
    — close_triage; refused while a finding is undecided."""
    from dct.workflows.triage import close_triage

    _require_one_triage_selector(args)
    engine = _get_engine()
    try:
        cycle, project = _triage_cycle_ref(args, engine)
        out = close_triage(engine, cycle, source=args.source, notes=args.note or None,
                           project=project)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ Closed triage cycle #{out['cycle_id']} — {len(out['closed']['answers'])} "
          f"answer(s), source: {out['closed']['source']}")


# ── MCP server (stdio) ───────────────────────────────────────────────────────

def cmd_server(args):
    from dct.core.provenance import from_environment, set_provenance

    set_provenance(from_environment("mcp"))
    try:
        from dct.mcp.server import mcp
    except ImportError as e:
        print(
            f"MCP server requires extras. Install with: pip install 'dct[mcp]'\n  (or reinstall: uv tool install --editable . --with 'mcp')\nOriginal error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    mcp.run()


# ── Upgrade ──────────────────────────────────────────────────────────────────

def cmd_upgrade(args):
    """Upgrade the installed package and refresh detected client integrations.

    Detects the install method (editable / uv tool / pipx / pip), runs the
    matching upgrade, re-links skills + hooks (idempotent) and prunes dangling
    links for skills/hooks dropped from the new package, then prints the effect
    matrix so the user knows what takes effect when.
    """
    import subprocess

    from dct.update import detect_install_method, dist_pkg, upgrade_command

    method = detect_install_method()

    if method == "editable":
        print("dct is installed as an editable / dev checkout — no package upgrade to run.")
        print("Pull the repo to update:")
        print("    git -C <dct-repo> pull")
        print("    uv pip install -e '<dct-repo>[mcp]'   # only if dependencies changed")
    else:
        cmd = upgrade_command(method)
        print(f"Detected install method: {method}")
        print(f"Upgrading {dist_pkg()} …  ({' '.join(cmd)})")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            print(f"✗ Upgrade command failed (exit {result.returncode})", file=sys.stderr)
            sys.exit(result.returncode)

    from dct.integrations.manager import refresh_integrations

    for result in refresh_integrations():
        if result["ok"]:
            print(f"  ✓ {result['client']}: Packaged integration assets refreshed")
            for path in result["removed"]:
                print(f"    Removed dangling link: {path}")
        else:
            print(f"  ⚠ {result['client']}: {result['error']}", file=sys.stderr)
            # K-66: each failing check's own remedy (a foreign reference must
            # be moved aside first); the generic repair only without one.
            for remedy in result.get("remedies") or [f"Run `dct integrate repair {result['client']}`."]:
                print(f"    → {remedy}", file=sys.stderr)

    print("\nWhat takes effect when:")
    print("  Now (this shell):            dct CLI, hooks, skill bodies")
    print("  Next session / /mcp reconnect: skill descriptions, MCP server code")
    print("  Next engine bootstrap:       database schema migrations")
    print("\nRecommend running `dct upgrade` at a session boundary — an old running")
    print("MCP server against a freshly migrated schema is an edge best avoided.")
    _maybe_print_legacy_nudge()


def _maybe_print_legacy_nudge() -> None:
    """One line after `dct upgrade`: a legacy ~/.claude install should learn
    about migrate-home at the upgrade moment, not only from the tty notice
    on some later command (ADR #52 follow-up)."""
    if paths.is_legacy(paths.default_config_path()):
        print(
            "\nnote: legacy installation in ~/.claude detected — run "
            "`dct migrate-home` to move it to the platform-neutral location "
            "(optional; `--dry-run` previews the plan)"
        )


# ── Project detect (for hooks) ───────────────────────────────────────────────

def cmd_project_detect(args):
    import os

    from dct.core.projects import get_project_by_path

    engine = _get_engine()
    cwd = os.getcwd()
    project = get_project_by_path(engine, cwd)
    if project:
        print(project["slug"])
    else:
        sys.exit(1)


# ── web daemon helpers ───────────────────────────────────────────────────────

def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True if `host:port` can be bound right now (no SO_REUSEADDR — we want a
    truthful 'is anyone holding this port' probe, not a tolerant one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return `preferred` if free, else the first free port scanning upward."""
    port = preferred
    while port < 65536:
        if _port_free(port, host):
            return port
        port += 1
    raise RuntimeError(f"no free port available at or above {preferred}")


def _write_pidfile(pid: int, url: str, path: Path | None = None) -> None:
    path = path if path is not None else _web_pidfile()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{url}\n")


def _detach_stdio(logfile: Path | None = None) -> None:
    """Detach a daemonized child from inherited stdio.

    Without this the serving child keeps the parent's stdout open, so a caller
    doing ``$(dct web start)`` (or any pipe / CI capture) blocks forever waiting
    for EOF. Point stdin at /dev/null and stdout+stderr at a log file, then drop
    the inherited descriptors.
    """
    logfile = logfile if logfile is not None else _web_logfile()
    sys.stdout.flush()
    sys.stderr.flush()
    null_fd = os.open(os.devnull, os.O_RDWR)
    out_fd = null_fd
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        out_fd = os.open(str(logfile), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        out_fd = null_fd  # log path unwritable → discard output but still detach
    os.dup2(null_fd, 0)
    os.dup2(out_fd, 1)
    os.dup2(out_fd, 2)
    for fd in {null_fd, out_fd}:
        if fd > 2:
            os.close(fd)


def _parse_pidfile(path: Path) -> "tuple[int, str] | None":
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError, ValueError):
        # Missing, unreadable, or undecodable — all mean "no usable pidfile";
        # raising here would crash web status/stop and mask a legacy daemon.
        return None
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    url = lines[1].strip() if len(lines) > 1 else ""
    return pid, url


def _read_pidfile(path: Path | None = None) -> "tuple[int, str] | None":
    if path is not None:
        return _parse_pidfile(path)
    # A daemon started by pre-1.2 dct wrote its pidfile under ~/.claude;
    # stop/status after the upgrade must still find that process — also when
    # the new-location file exists but is unparseable (a malformed file must
    # not mask a live legacy daemon).
    return _parse_pidfile(_web_pidfile()) or _parse_pidfile(
        paths.legacy_dir() / "dct-web.pid"
    )


def _clear_pidfile(path: Path | None = None) -> None:
    targets = [path] if path is not None else [
        _web_pidfile(),
        paths.legacy_dir() / "dct-web.pid",
    ]
    for target in targets:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def _pid_alive_windows(pid: int) -> bool:
    """Liveness via OpenProcess — os.kill(pid, 0) on Windows maps every
    non-CTRL signal to TerminateProcess, so the POSIX probe would KILL the
    daemon it is checking (W9)."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists (POSIX: signal 0 no-op probe)."""
    if _IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


# ── web daemon commands ──────────────────────────────────────────────────────

def cmd_web_start(args):
    from dct.core.engine import bootstrap_engine
    from dct.web.app import create_app

    cfg = load_config()
    web = cfg.web
    host = getattr(args, "host", None) or web.host
    preferred = getattr(args, "port", None) or web.port
    no_fallback = getattr(args, "no_fallback", False)
    open_browser = getattr(args, "open", False) or web.open_browser
    fallback = web.auto_fallback and not no_fallback

    existing = _read_pidfile()
    if existing and _pid_alive(existing[0]):
        print(f"✗ dct web already running (pid {existing[0]}) → {existing[1]}", file=sys.stderr)
        sys.exit(1)
    if existing:
        _clear_pidfile()  # stale pidfile from a dead process

    if fallback:
        port = _find_free_port(preferred, host)
    else:
        if not _port_free(preferred, host):
            print(f"✗ Port {preferred} is busy and --no-fallback is set", file=sys.stderr)
            sys.exit(1)
        port = preferred

    url = f"http://localhost:{port}"

    if _IS_WINDOWS:
        # W8: fork/setsid don't exist on Windows — detach a fresh interpreter
        # running the internal `_web-serve` foreground entry instead.
        pid = _spawn_windows_daemon(host, port)
        _write_pidfile(pid, url)
        print(f"✓ dct web started (pid {pid}) → {url}")
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        return

    # Daemonize: parent records the child pid + URL and returns; child detaches
    # from the controlling terminal and serves. No tty needed — safe under CC/CI.
    pid = os.fork()
    if pid > 0:  # parent
        _write_pidfile(pid, url)
        print(f"✓ dct web started (pid {pid}) → {url}")
        if open_browser:
            import webbrowser
            webbrowser.open(url)
        return

    # child: detach from the controlling terminal AND from inherited stdio so a
    # captured `$(dct web start)` does not block on the pipe the server keeps open.
    os.setsid()
    _detach_stdio()
    try:
        engine = bootstrap_engine(create=False)  # READ-ONLY: never create_tables()
        app = create_app(engine, read_only=True)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        os._exit(0)


def _spawn_windows_daemon(host: str, port: int) -> int:
    """Detached serve process for Windows (W8): DETACHED_PROCESS +
    CREATE_NEW_PROCESS_GROUP replace the fork/setsid daemonization; stdio
    goes to the web logfile like _detach_stdio does on POSIX."""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    logfile = _web_logfile()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dct.cli", "_web-serve",
             "--host", host, "--port", str(port)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
    return proc.pid


def cmd_internal_web_serve(args):
    """Foreground serve entry for the Windows daemon spawn (hidden)."""
    from dct.core.engine import bootstrap_engine
    from dct.web.app import create_app

    engine = bootstrap_engine(create=False)  # READ-ONLY: never create_tables()
    app = create_app(engine, read_only=True)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


def cmd_web_stop(args):
    info = _read_pidfile()
    if not info:
        print("dct web: not running (no pidfile)")
        return
    pid, _url = info
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"✓ Sent SIGTERM to dct web (pid {pid})")
    except ProcessLookupError:
        print(f"  (pid {pid} not alive — clearing stale pidfile)")
    except PermissionError:
        print(f"✗ No permission to signal pid {pid}", file=sys.stderr)
        sys.exit(1)
    except OSError:
        # W10: Windows raises plain OSError (e.g. WinError 87) for a missing
        # pid — same stale-pidfile treatment as ProcessLookupError on POSIX.
        print(f"  (pid {pid} not alive — clearing stale pidfile)")
    _clear_pidfile()


def cmd_web_status(args):
    info = _read_pidfile()
    if not info:
        print("dct web: stopped")
        return
    pid, url = info
    if _pid_alive(pid):
        print(f"dct web: running (pid {pid}) → {url}")
    else:
        print(f"dct web: stale pidfile (pid {pid} not alive). Run `dct web stop` to clear.")


# ── Argument parser ──────────────────────────────────────────────────────────

def _print_integration_result(result: dict, as_json: bool) -> None:
    import json

    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    if "client" in result:
        if result.get("action") == "uninstall":
            print(f"{result['client']}: integration removal processed")
            details = result.get("result", {})
            if details.get("message"):
                print(f"  {details['message']}")
            for path in details.get("preserved", []):
                print(f"  preserved: {path}")
            for path in details.get("retained", []):
                print(f"  retained: {path}")
        else:
            print(f"{result['client']}: {'ready' if result.get('ok') else 'attention required'}")
    for row in result.get("checks", []):
        print(f"  {row['status']}: {row['check']} — {row['message']}")
        if row.get("remedy") and row["status"] != "ok":
            print(f"    → {row['remedy']}")
    for integration in result.get("integrations", []):
        _print_integration_result(integration, False)
    if "error" in result:
        print(f"error: {result['error']}")


def cmd_integrate(args):
    """Manage one client without bootstrapping or changing the DCT database."""
    from contextlib import redirect_stdout

    from dct.integrations.manager import (
        get_integration,
        install_integration,
        integration_status,
    )

    try:
        adapter = get_integration(args.client)
        # Legacy installers print notes. Keep --json stdout machine-readable.
        with redirect_stdout(sys.stderr):
            if args.action in ("install", "repair"):
                result = install_integration(adapter)
            elif args.action == "uninstall":
                outcome = adapter.uninstall()
                result = {"client": args.client, "action": "uninstall",
                          "ok": outcome.get("status") != "error", "result": outcome}
            else:
                result = integration_status(adapter)
    except (OSError, ValueError) as exc:
        result = {"client": args.client, "ok": False, "error": str(exc)}
    _print_integration_result(result, args.json)
    if not result["ok"]:
        raise SystemExit(1)


def cmd_doctor(args):
    from dct.integrations.doctor import doctor

    result = doctor(args.client, args.python)
    _print_integration_result(result, args.json)
    if not result["ok"]:
        raise SystemExit(1)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dct", description="Deterministic Context Tracker — cross-project tracking")
    parser.add_argument("--version", action="version", version=f"dct {__version__}")

    sub = parser.add_subparsers(dest="command")

    p_integrate = sub.add_parser("integrate", help="Install, inspect or repair a client integration")
    p_integrate.add_argument("action", choices=("install", "status", "repair", "uninstall"))
    p_integrate.add_argument("client", choices=("claude", "codex"))
    p_integrate.add_argument("--json", action="store_true", help="Return machine-readable JSON")
    p_doctor = sub.add_parser("doctor", help="Read-only configuration and runtime diagnostics")
    p_doctor.add_argument("--client", choices=("claude", "codex"))
    p_doctor.add_argument("--python", action="append", default=[], help="Also check this Python environment (repeatable)")
    p_doctor.add_argument("--json", action="store_true", help="Return machine-readable JSON")

    # init
    p_init = sub.add_parser("init", help="Interactive setup")
    p_init.add_argument(
        "--wire-hooks", dest="wire_hooks", action="store_true", default=None,
        help="Wire dct hooks into ~/.claude/settings.json without prompting",
    )
    p_init.add_argument(
        "--no-wire-hooks", dest="wire_hooks", action="store_false",
        help="Skip settings.json hook wiring",
    )
    p_init.add_argument(
        "--attach-snippet", dest="attach_snippet", action="store_true", default=None,
        help="Append the short dct pointer (names the canonical shared reference) "
             "to ~/.claude/CLAUDE.md without prompting",
    )
    p_init.add_argument(
        "--no-attach-snippet", dest="attach_snippet", action="store_false",
        help="Skip the ~/.claude/CLAUDE.md pointer (the reference is installed anyway)",
    )

    # hooks
    p_hooks = sub.add_parser("hooks", help="Manage Claude Code hook wiring")
    p_hooks_sub = p_hooks.add_subparsers(dest="hooks_command")
    p_hooks_wire = p_hooks_sub.add_parser(
        "wire", help="Wire dct hooks into ~/.claude/settings.json (idempotent merge)"
    )
    p_hooks_wire.add_argument(
        "--settings", help="Path to settings.json (default: ~/.claude/settings.json)"
    )

    # server (MCP stdio entry point)
    sub.add_parser("server", help="Start the MCP server (stdio transport)")

    # upgrade (self-update + symlink refresh — #356)
    p_web_serve = sub.add_parser("_web-serve")  # hidden: Windows daemon entry (W8)
    p_web_serve.add_argument("--host", default="127.0.0.1")
    p_web_serve.add_argument("--port", type=int, required=True)

    sub.add_parser(
        "upgrade",
        help="Upgrade dct (auto-detects uv tool / pipx / pip; guides editable installs) "
             "and refresh skill/hook symlinks",
    )

    # migrate-home (legacy ~/.claude → platform-neutral locations — ADR #52)
    p_migrate = sub.add_parser(
        "migrate-home",
        help="Move a legacy ~/.claude dct installation (config, sqlite db, "
             "update cache, web state) to the platform-neutral locations",
    )
    p_migrate.add_argument(
        "--dry-run", action="store_true",
        help="Print the migration plan without changing anything",
    )

    # project
    p_project = sub.add_parser("project", help="Manage projects")
    p_proj_sub = p_project.add_subparsers(dest="project_command")

    p_proj_add = p_proj_sub.add_parser("add", help="Register a project")
    p_proj_add.add_argument("slug")
    p_proj_add.add_argument("path", nargs="?")
    p_proj_add.add_argument("--name")
    p_proj_add.add_argument("--version", "-v")

    p_proj_sub.add_parser("list", help="List projects")

    p_proj_rm = p_proj_sub.add_parser("remove", help="Remove a project (soft delete)")
    p_proj_rm.add_argument("slug")

    p_proj_sub.add_parser("detect", help="Detect project from CWD")

    p_proj_setv = p_proj_sub.add_parser("set-version", help="Set current_version for a project")
    p_proj_setv.add_argument("slug")
    p_proj_setv.add_argument("version")

    p_proj_rename = p_proj_sub.add_parser(
        "rename",
        help="Rename a project (any of slug/name/description/path) — project_id stable, FKs preserved",
    )
    p_proj_rename.add_argument("slug", help="Current slug of the project to rename")
    p_proj_rename.add_argument("--slug", dest="new_slug", help="New slug (must be unique among live projects)")
    p_proj_rename.add_argument("--name", help="New display name")
    p_proj_rename.add_argument("--description", help="New description")
    p_proj_rename.add_argument("--path", help="New filesystem path (does not move files on disk)")

    p_proj_clog = p_proj_sub.add_parser(
        "changelog",
        help="Opt a project in/out of dct changelog-hook enforcement (#222); off also means "
             "the release writes no changelog file (#876)",
    )
    p_proj_clog.add_argument("slug")
    p_proj_clog.add_argument("state", choices=["on", "off"])

    p_proj_rexec = p_proj_sub.add_parser(
        "release-executor",
        help="Get/set who performs the project's release effects: dct (default) or own "
             "(the project's own release tool)",
    )
    p_proj_rexec.add_argument("slug")
    p_proj_rexec.add_argument("executor", nargs="?", choices=["dct", "own"],
                              help="Omit to query the current value")

    p_proj_prrefs = p_proj_sub.add_parser(
        "changelog-pr-refs",
        help="Get/set rendering `(#N)` PR references in the changelog (opt-in)",
    )
    p_proj_prrefs.add_argument("slug")
    p_proj_prrefs.add_argument("state", nargs="?", choices=["on", "off"],
                               help="Omit to query the current value")

    p_proj_trailers = p_proj_sub.add_parser(
        "trailers",
        help="Get/store the project's commit-trailer preferences (asked once; /commit "
             "applies them without asking). Each is on, off or not answered yet",
    )
    p_proj_trailers.add_argument("slug")
    p_proj_trailers.add_argument(
        "--co-authored-by", choices=["on", "off"],
        help="Add a Co-Authored-By trailer to commits (omit to keep the stored answer)",
    )
    p_proj_trailers.add_argument(
        "--session-link", choices=["on", "off"],
        help="Add a session / artifact link to commits (omit to keep the stored answer; "
             "a project whose own rules forbid it keeps it out regardless)",
    )

    p_proj_vss = p_proj_sub.add_parser(
        "version-sync-script",
        help="Write scripts/dct-version-sync.sh from dct's template: every release then sets "
             "the new version in the project's version file (never overwrites a script)",
    )
    p_proj_vss.add_argument("slug")
    p_proj_vss.add_argument(
        "--kind", choices=list(VERSION_FILES),
        help="The version file the script edits (default: the one dct detects)",
    )

    p_proj_mc = p_proj_sub.add_parser(
        "manages-changelog",
        help="Exit 0 if the project opts into dct changelog enforcement, else 1 (hook query)",
    )
    p_proj_mc.add_argument("slug")

    p_proj_clogpath = p_proj_sub.add_parser(
        "changelog-path",
        help="Get/choose a project's changelog file relative to its root (#225); without a "
             "choice dct detects it (#876: the ONE tracked CHANGELOG / CHANGES / HISTORY / "
             "NEWS / RELEASES Markdown file of the project root)",
    )
    p_proj_clogpath.add_argument("slug")
    p_proj_clogpath.add_argument(
        "relpath", nargs="?",
        help="Relative Markdown changelog path (e.g. pkg/CHANGELOG.md, or exactly "
             "changelog.md) — an explicit choice; it chooses the file but waives no check (a "
             "non-Markdown name, a symlink anywhere on the path, or a file / submodule where a "
             "directory is expected is refused by the release). Omit to query the "
             "stored value and the file a release would write (detected / stored / default, "
             "or the problem with its next steps).",
    )
    p_proj_clogpath.add_argument(
        "--detect", action="store_true",
        help="Drop the explicit choice: dct detects the changelog file again",
    )

    # plan
    p_plan = sub.add_parser("plan", help="Plan operations (v0.2.0)")
    p_plan_sub = p_plan.add_subparsers(dest="plan_command")

    p_plan_rescan = p_plan_sub.add_parser(
        "rescan",
        help="Re-scan a plan markdown file (hook entrypoint). Idempotent + quiet-safe.",
    )
    p_plan_rescan.add_argument("file")
    p_plan_rescan.add_argument("--quiet", "-q", action="store_true",
                               help="Suppress all output on stdout/stderr")
    p_plan_rescan.add_argument("--resolve-uncertain", action="store_true",
                               help="Resolve uncertain sections with configured rules/command (disabled remains unresolved)")

    p_plan_type = p_plan_sub.add_parser("set-section-type", help="Apply/revoke a manual section type")
    p_plan_type.add_argument("section_ulid", help="Explicit section ULID (integer ids are MCP-only)")
    p_plan_type.add_argument("section_type")
    p_plan_type.add_argument("--project", required=True, help="Expected project slug")
    p_plan_type.add_argument("--json", action="store_true")
    p_plan_uncertain = p_plan_sub.add_parser("resolve-uncertain", help="Resolve indexed uncertainty with configured rules/command")
    p_plan_uncertain.add_argument("plan_ulid", help="Explicit plan ULID")
    p_plan_uncertain.add_argument("--project", required=True, help="Expected project slug")
    p_plan_uncertain.add_argument("--json", action="store_true")

    # add (item)
    p_add = sub.add_parser("add", help="Create a tracked item")
    p_add.add_argument("project")
    p_add.add_argument("type", choices=["issue", "todo", "feature", "idea", "improvement"])
    p_add.add_argument("title")
    p_add.add_argument("--priority", "-p", default="P2")
    p_add.add_argument("--component", "-c")
    p_add.add_argument("--short-title", "-S",
                       help="2-4 word label shown next to the item number "
                            "(≤ 50 chars, refused not truncated)")
    p_add.add_argument("--tags", "-t")
    p_add.add_argument("--description", "-d")
    p_add.add_argument("--source-file", "-f")

    # list (items)
    p_list = sub.add_parser("list", help="List items")
    p_list.add_argument("project", nargs="?")
    p_list.add_argument("--status", "-s")
    p_list.add_argument("--type", "-t")
    p_list.add_argument("--priority", "-p")
    p_list.add_argument("--search", "-q")
    p_list.add_argument("--tag", help="Only items carrying this tag "
                                      "(unknown tag → error listing known tags)")
    p_list.add_argument("--gate", help="Only items with a pending gate checkpoint: 'any' "
                                       "or a kind (dogfood, review, security, approval…), "
                                       "in a sprint or not")

    # show
    p_show = sub.add_parser("show", help="Show item details")
    p_show.add_argument("id", type=int)

    # update
    p_update = sub.add_parser("update", help="Update an item")
    p_update.add_argument("id", type=int)
    p_update.add_argument("--title")
    p_update.add_argument("--status", "-s")
    p_update.add_argument("--priority", "-p")
    p_update.add_argument("--component", "-c")
    p_update.add_argument("--resolution")
    p_update.add_argument("--short-title", default=None,
                          help="2-4 word label (≤ 50 chars). Omit to leave it "
                               "unchanged; --short-title \"\" clears it.")
    # No "-t" short alias here on purpose (K-25): "-t" already means --type on
    # `list` and --tags on `add`, so on `update` it would silently tag an item
    # with what the user meant as a type. Long flag only.
    p_update.add_argument("--tags", default=None,
                          help="Comma-separated tags — FULL REPLACE of the item's "
                               "tags. Omit to leave them unchanged; --tags \"\" "
                               "clears them all.")
    p_update.add_argument("--reason", default=None,
                          help="Why the item changes — added as a note in the same "
                               "transaction as the update (a refused update adds none).")

    # resolve
    p_resolve = sub.add_parser("resolve", help="Resolve/close an item")
    p_resolve.add_argument("id", type=int)
    p_resolve.add_argument(
        "--status", "-s", default="resolved", choices=list(TERMINAL_STATUSES)
    )
    p_resolve.add_argument("--resolution", "-r")

    # note
    p_note = sub.add_parser("note", help="Add a note to an item")
    p_note.add_argument("id", type=int)
    p_note.add_argument("note")

    # delete
    p_delete = sub.add_parser("delete", help="Soft-delete an item")
    p_delete.add_argument("id", type=int)

    # clog (changelog)
    p_clog = sub.add_parser("clog", help="Changelog management")
    p_clog_sub = p_clog.add_subparsers(dest="clog_command")

    p_clog_add = p_clog_sub.add_parser("add", help="Add changelog entry")
    p_clog_add.add_argument("category", choices=["added", "changed", "fixed", "removed"])
    p_clog_add.add_argument("entry")
    p_clog_add.add_argument("--project", "-P")
    p_clog_add.add_argument("--component", "-c")
    p_clog_add.add_argument("--source-file", "-f")
    p_clog_add.add_argument("--item-id", type=int)
    p_clog_add.add_argument(
        "--branch", help="Branch the change was made on (default: detected in CWD)")
    p_clog_add.add_argument(
        "--worktree", help="Checkout toplevel of the change (default: detected in CWD)")

    p_clog_import = p_clog_sub.add_parser("import-released", help="Import one historical row without creating a release")
    p_clog_import.add_argument("project")
    p_clog_import.add_argument("category", choices=["added", "changed", "fixed", "removed"])
    p_clog_import.add_argument("entry")
    p_clog_import.add_argument("--version", required=True)
    p_clog_import.add_argument("--released-at", default="", help="ISO date or timestamp with offset; omitted means import time")
    p_clog_import.add_argument("--component", default="")
    p_clog_import.add_argument("--source-file", default="")
    p_clog_import.add_argument("--source-lines", default="")
    p_clog_import.add_argument("--related-item", type=int, default=0)
    p_clog_import.add_argument("--json", action="store_true")

    def _add_branch_filters(p):
        p.add_argument(
            "--branch",
            help="Only entries whose stored branch is exactly this (any branch source)")
        p.add_argument(
            "--worktree",
            help="Only entries of this absolute worktree path (normalized as on write)")
        p.add_argument(
            "--include-unbranched", action="store_true",
            help="With --branch: also uncertain entries — no recorded branch, or "
                 "detected by the MCP server (branch_source=server, any branch)")

    p_clog_list = p_clog_sub.add_parser("list", help="List changelog entries")
    p_clog_list.add_argument("--project", "-P")
    p_clog_list.add_argument("--unreleased", "-u", action="store_true")
    _add_branch_filters(p_clog_list)

    p_clog_count = p_clog_sub.add_parser("count", help="Count unreleased entries")
    p_clog_count.add_argument("--project", "-P")
    _add_branch_filters(p_clog_count)

    p_clog_release = p_clog_sub.add_parser(
        "release",
        help="Release (alias of `release apply` without --plan-digest; needs the approved "
             "--proposal-digest): stamp, changelog, version files, commit, tag, push")
    p_clog_release.add_argument(
        "version", nargs="?", help="X.Y or X.Y.Z (a leading v is dropped)")
    p_clog_release.add_argument("--project", "-P")
    p_clog_bump = p_clog_release.add_mutually_exclusive_group()
    p_clog_bump.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_clog_bump.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_clog_bump.add_argument("--major", dest="bump", action="store_const", const="major")
    p_clog_sel = p_clog_release.add_mutually_exclusive_group()
    p_clog_sel.add_argument(
        "--entries", help="Comma-separated entry ids to stamp (default: all unreleased)")
    p_clog_sel.add_argument(
        "--no-entries", action="store_true",
        help="Version-only release: stamp no entries, only set the version")
    p_clog_release.add_argument(
        "--proposal-digest",
        help="Required for a new release: the proposal_sha256 of the approved `dct release "
             "prepare` — prepared for exactly the plan this command derives (same version "
             "and selection)")
    p_clog_release.add_argument(
        "--confirm-version-files", action="store_true", help=argparse.SUPPRESS)
    p_clog_release.add_argument(
        "--confirm", action="store_true",
        help="Required: it stamps, writes, commits, tags and pushes")
    _add_push_flags(p_clog_release)
    p_clog_release.add_argument("--json", action="store_true", help="Stable JSON document")
    p_clog_release.epilog = (
        "Alias of `dct release apply` (needs --confirm like apply) that derives the plan "
        "itself instead of taking --plan-digest: stamp, changelog file, the approved version "
        "files, release commit (through the repository's hooks) and tag, pushed to the push "
        "target, each verified. It never runs the version sync and never approves a "
        "proposal: run `dct release plan --version V --json` and `dct release prepare "
        "--version V (--entries … | --no-entries) --plan-digest D` first, review the "
        "proposed files and pass its proposal_sha256 as --proposal-digest — the proposal must "
        "be bound to exactly the plan this command derives. Refuses — instead of guessing — "
        "when the owner must decide: no approved proposal, entries without proof, a proof "
        "range not starting at v<current_version>, a push target that is not the complete "
        "approved one (--push-remote, --push-branch, --push-url). " + RELEASE_PUSH_EPILOG)

    p_clog_rstatus = p_clog_sub.add_parser(
        "release-status",
        help="Show a release record (default: the latest unfinished release)")
    p_clog_rstatus.add_argument("--version", help="The release to show (X.Y or X.Y.Z)")
    p_clog_rstatus.add_argument("--project", "-P")

    p_clog_radv = p_clog_sub.add_parser(
        "release-advance",
        help="Move a release record forward after a verified step "
             "(stamped → files_written → committed → tagged → pushed → completed)")
    p_clog_radv.add_argument("version", help="The release (X.Y or X.Y.Z)")
    p_clog_radv.add_argument("status", choices=RELEASE_FLOW)
    p_clog_radv.add_argument("--project", "-P")
    p_clog_radv.add_argument(
        "--release-commit-sha",
        help="The verified release commit — recorded once, from 'committed' on "
             "(the candidate commit is immutable)")
    p_clog_radv.add_argument(
        "--tag-proof", metavar="JSON",
        help="The tag as you verified it: {\"name\", \"object\" (git rev-parse "
             "refs/tags/<name>), \"commit\" (git rev-parse <name>^{commit}), "
             "\"annotated\": true|false} — required for tagged / pushed / completed of a "
             "release with git; recorded once")
    p_clog_radv.add_argument(
        "--push-proof", metavar="JSON",
        help="The push as you verified it: {\"target\": {\"remote\", \"url\", "
             "\"branch\", \"tag\"}, \"tag_commit\", \"branch_tip\"} — required for "
             "pushed (and for completed when the release's consent approved a push)")
    p_clog_radv.add_argument("--json", action="store_true", help="Stable JSON document")
    p_clog_radv.epilog = (
        "The ledger path for a release dct does NOT execute — a project's own release tool "
        "(release executor 'own') or a record stamped outside `dct release apply`: report "
        "only what you verified. A status is never proof: a release with git reaches tagged "
        "/ pushed / completed only with --tag-proof (full commit ids; its commit must be the "
        "release commit), and pushed only with --push-proof. A release dct executes is "
        "refused (`dct release resume` records its progress). " + RELEASE_LOCK_EPILOG)

    p_clog_rabandon = p_clog_sub.add_parser(
        "release-abandon",
        help="End an unfinished release as abandoned (keeps the record, stamps and "
             "current_version)")
    p_clog_rabandon.add_argument("version", help="The release (X.Y or X.Y.Z)")
    p_clog_rabandon.add_argument("--reason", required=True, help="Why (stored on the record)")
    p_clog_rabandon.add_argument(
        "--confirm", action="store_true",
        help="Required: the version stays taken by the abandoned record")
    p_clog_rabandon.add_argument("--project", "-P")
    p_clog_rabandon.add_argument("--json", action="store_true", help="Stable JSON document")
    p_clog_rabandon.epilog = (
        "Alias of `dct release abandon` (same rules, same operation lock). "
        + RELEASE_LOCK_EPILOG)

    p_clog_cand = p_clog_sub.add_parser(
        "candidates",
        help="Changelog candidates for the commit being prepared (read-only)")
    p_clog_cand.add_argument("--project", "-P")
    p_clog_cand.add_argument("--json", action="store_true", help="Stable JSON document")

    p_clog_update = p_clog_sub.add_parser(
        "update",
        help="Amend an unreleased changelog entry (\"\" clears a field, --item-id 0 unlinks)")
    p_clog_update.add_argument("id", type=int)
    p_clog_update.add_argument("--project", "-P")
    p_clog_update.add_argument("--entry")
    p_clog_update.add_argument(
        "--category", choices=["added", "changed", "fixed", "removed"])
    p_clog_update.add_argument("--component", "-c")
    p_clog_update.add_argument("--source-file", "-f")
    p_clog_update.add_argument("--source-lines")
    p_clog_update.add_argument("--item-id", type=int)
    p_clog_update.add_argument("--branch")
    p_clog_update.add_argument("--worktree")

    p_clog_delete = p_clog_sub.add_parser(
        "delete", help="Soft-delete an unreleased changelog entry")
    p_clog_delete.add_argument("id", type=int)
    p_clog_delete.add_argument("--project", "-P")

    p_clog_export = p_clog_sub.add_parser("export", help="Export to Keep a Changelog markdown")
    p_clog_export.add_argument("--project", "-P")
    p_clog_export.add_argument("--output", "-o")

    # release (the release workflow, K-72): plan = layer 1 (read-only proof),
    # attest / prepare / apply / resume / abandon / reconstruct = layer 3 (effects)
    p_release = sub.add_parser(
        "release", help="Release workflow: plan (read-only), attest, prepare, apply, resume, "
                        "abandon, reconstruct, takeover, version-files, status")
    p_release_sub = p_release.add_subparsers(dest="release_command")
    p_rel_plan = p_release_sub.add_parser(
        "plan", help="Show the release plan of the current checkout — no side effects")
    p_rel_plan.add_argument("--project", "-P")
    p_rel_plan_v = p_rel_plan.add_mutually_exclusive_group()
    p_rel_plan_v.add_argument("--version", help="Target version (X.Y or X.Y.Z)")
    p_rel_plan_v.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_rel_plan_v.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_rel_plan_v.add_argument("--major", dest="bump", action="store_const", const="major")
    p_rel_plan_sel = p_rel_plan.add_mutually_exclusive_group()
    p_rel_plan_sel.add_argument(
        "--entries", help="Comma-separated entry ids the snapshot binds (default: every "
                          "proven id)")
    p_rel_plan_sel.add_argument(
        "--no-entries", action="store_true", help="Bind a version-only release")
    p_rel_plan.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_status = p_release_sub.add_parser(
        "status", help="Show a release record (alias of `clog release-status`)")
    p_rel_status.add_argument("--version", help="The release to show (X.Y or X.Y.Z)")
    p_rel_status.add_argument("--project", "-P")
    p_rel_status.add_argument("--json", action="store_true", help="Stable JSON document")

    p_rel_attest = p_release_sub.add_parser(
        "attest", help="Create the empty attestation commit for unproven entries")
    p_rel_attest.epilog = (
        "The empty commit `chore: attest changelog entries` with the trailer `Changelog: "
        "<ids>` on the current HEAD (needs git >= 2.36). The repository's commit hooks run; "
        "the commit is created on the pinned HEAD and checked BEFORE it is published: it "
        "must prove EXACTLY the given ids (a hook that adds, drops or duplicates an id, or "
        "stages content, is refused with nothing published), then the branch HEAD names "
        "moves by compare-and-swap. " + RELEASE_LOCK_EPILOG)
    p_rel_attest.add_argument("--entries", required=True,
                              help="Comma-separated ids the plan lists as unproven")
    p_rel_attest.add_argument("--confirm", action="store_true",
                              help="Required: the owner confirms the changes are in HEAD")
    p_rel_attest.add_argument("--project", "-P")
    p_rel_attest.add_argument("--json", action="store_true", help="Stable JSON document")

    p_rel_apply = p_release_sub.add_parser(
        "apply", help="Release: stamp, changelog file, the approved version files, commit, "
                      "tag, push (verified)")
    p_rel_apply.add_argument("--project", "-P")
    p_rel_apply_v = p_rel_apply.add_mutually_exclusive_group()
    p_rel_apply_v.add_argument("--version", help="Target version (X.Y or X.Y.Z)")
    p_rel_apply_v.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_rel_apply_v.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_rel_apply_v.add_argument("--major", dest="bump", action="store_const", const="major")
    p_rel_apply_sel = p_rel_apply.add_mutually_exclusive_group()
    p_rel_apply_sel.add_argument(
        "--entries", help="Comma-separated entry ids — each proven by the plan (required "
                          "without --consent)")
    p_rel_apply_sel.add_argument(
        "--no-entries", action="store_true", help="Version-only release")
    p_rel_apply.add_argument("--confirm", action="store_true", help="Required")
    p_rel_apply.add_argument(
        "--consent", metavar="C",
        help="Release EXACTLY the screen the owner approved: the consent_digest of a variant "
             "of `dct release prepare --preview` (it commits your uncommitted changes and "
             "makes the attestation commit as that screen showed). Takes no --version / bump "
             "/ --entries / --no-entries / digest / push flag — the preview binds them all")
    p_rel_apply.add_argument(
        "--plan-digest",
        help="Required without --consent: the snapshot_digest of the approved `dct release "
             "plan --version V --entries … --json` (the release is refused when the "
             "re-derived plan differs)")
    p_rel_apply.add_argument(
        "--proposal-digest",
        help="Required for a new release: the proposal_sha256 of the approved `dct release "
             "prepare` (same version, selection and plan digest)")
    p_rel_apply.add_argument(
        "--confirm-version-files", action="store_true", help=argparse.SUPPRESS)
    _add_push_flags(p_rel_apply)
    p_rel_apply.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_apply.set_defaults(apply_parser=p_rel_apply)
    p_rel_apply.epilog = RELEASE_PUSH_EPILOG + (
        " --consent C: the one-dialog release — the work-in-progress commit and the "
        "attestation commit are made through the same hook lifecycle BEFORE the stamp, the "
        "content is re-derived and compared with C before and after them, and a re-run of "
        "the same command adopts what an interrupted one made.")

    p_rel_prepare = p_release_sub.add_parser(
        "prepare", help="Run the version sync isolated and store the version-file proposal "
                        "for approval (before the stamp; nothing stamped)")
    p_rel_prepare.add_argument("--project", "-P")
    p_rel_prepare_v = p_rel_prepare.add_mutually_exclusive_group()
    p_rel_prepare_v.add_argument("--version", help="Target version (X.Y or X.Y.Z)")
    p_rel_prepare_v.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_rel_prepare_v.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_rel_prepare_v.add_argument("--major", dest="bump", action="store_const", const="major")
    p_rel_prepare_sel = p_rel_prepare.add_mutually_exclusive_group()
    p_rel_prepare_sel.add_argument(
        "--entries", help="Comma-separated entry ids — each proven by the plan (required "
                          "without --preview, else default: every id proven at the "
                          "candidate)")
    p_rel_prepare_sel.add_argument(
        "--no-entries", action="store_true", help="Version-only release")
    p_rel_prepare_mode = p_rel_prepare.add_mutually_exclusive_group()
    p_rel_prepare_mode.add_argument(
        "--plan-digest",
        help="Required without --preview: the snapshot_digest of the approved `dct release "
             "plan` (same version and selection)")
    p_rel_prepare_mode.add_argument(
        "--preview", action="store_true",
        help="The one-screen release: compute what the release would be — the commit of "
             "your uncommitted changes, the entries, the changelog section, the version "
             "files, the push — and store it for `apply --consent`; nothing committed")
    p_rel_prepare_mode.add_argument(
        "--discard-preview", action="store_true",
        help="Drop the stored preview (the owner declined it); only with -P and --json")
    p_rel_prepare.add_argument(
        "--attest", help="--preview: comma-separated waiting entry ids the shown release "
                         "also marks as done (an empty attestation commit; not made now)")
    p_rel_prepare.add_argument(
        "--message-file",
        help="--preview: the commit message of your uncommitted changes — required when the "
             "work tree has any, refused when it is clean")
    p_rel_prepare.add_argument(
        "--include-untracked", action="append", metavar="PATH",
        help="--preview: an untracked (not ignored) file to commit with your changes; "
             "repeatable (untracked release artifacts are included automatically)")
    _add_push_flags(p_rel_prepare)
    p_rel_prepare.add_argument(
        "--no-variants", action="store_true",
        help='--preview: do not compute the "release with all waiting entries" variant')
    p_rel_prepare.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_prepare.set_defaults(prepare_parser=p_rel_prepare)
    p_rel_prepare.epilog = (
        "Effects: runs scripts/dct-version-sync.sh (or scripts/version-sync.sh) of the "
        "candidate ONCE with V in a temporary detached worktree (a project outside git: in a "
        "temporary copy of its directory) — never in your checkout, which is verified "
        "unchanged afterwards; writes the expected blobs into the repository's object "
        "database, pinned by the ref refs/dct/release-proposals/<project>/<proposal id> "
        "until the release is completed or abandoned or a new prepare replaces the proposal "
        "(outside git: dct's release blob store, same lifetime), and the pending proposal "
        "into the database. Nothing is stamped, committed or tagged. Environment of the "
        "script: only the files of the candidate (no untracked or ignored file of your "
        "checkout — virtualenvs, node_modules, build output — and no submodule content), "
        "stdin closed, no terminal, GIT_* repository variables dropped, a timeout; a script "
        "that needs anything else fails with that contract — dct never runs it in your "
        "checkout instead. --preview runs the same sync on a synthetic commit of your "
        "uncommitted changes (built in a temporary index — your index, work tree and "
        "branches are untouched), stores the preview for `dct release apply --consent` and "
        "pins its blobs by refs/dct/release-previews/<project>/<preview id> (a preview and "
        "a prepare replace each other). " + RELEASE_LOCK_EPILOG + RELEASE_EXECUTOR_EPILOG)

    p_rel_resume = p_release_sub.add_parser(
        "resume", help="Finish the unfinished release (verifies every effect first; executes "
                       "the consent stored at apply — no --confirm)")
    p_rel_resume.add_argument("--project", "-P")
    p_rel_resume.add_argument(
        "--confirm-version-files", action="store_true", help=argparse.SUPPRESS)
    p_rel_resume.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_resume.epilog = RELEASE_EXECUTOR_EPILOG + RELEASE_LOCK_EPILOG + (
        "The push goes to the URL approved at apply (it must still be a push URL of its "
        "remote; URLs added since receive nothing). "
        "A record stamped outside `dct release apply` (MCP release_changelog, an own tool, a "
        "partial reconstruction) is refused: hand it to dct with `dct release takeover` (its "
        "verified artifacts become the consent; resume then performs only the missing "
        "effects), finish it with `dct clog release-advance` after verifying its effects "
        "yourself, or abandon it.")

    p_rel_abandon = p_release_sub.add_parser(
        "abandon", help="End an unfinished release as abandoned (all effects stay)")
    p_rel_abandon.add_argument("--version", required=True, help="The release (X.Y or X.Y.Z)")
    p_rel_abandon.add_argument("--reason", required=True, help="Why (stored on the record)")
    p_rel_abandon.add_argument("--confirm", action="store_true", help="Required")
    p_rel_abandon.add_argument("--project", "-P")
    p_rel_abandon.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_abandon.epilog = (
        "The record ends 'abandoned'; every effect stays and nothing is reset. The ref "
        "pinning its proposal's blobs is removed. " + RELEASE_LOCK_EPILOG)

    p_rel_recon = p_release_sub.add_parser(
        "reconstruct", help="Record a legacy release made before release records existed")
    p_rel_recon.add_argument("--version", required=True, help="The release (X.Y or X.Y.Z)")
    p_rel_recon.add_argument("--confirm", action="store_true",
                             help="Write the record (without: show the evidence only)")
    p_rel_recon_how = p_rel_recon.add_mutually_exclusive_group()
    p_rel_recon_how.add_argument(
        "--release-commit",
        help="Choose ONE of several verified release commits (no tag; the refusal lists them)")
    p_rel_recon_how.add_argument(
        "--evidence",
        help="For a project whose release executor is 'own': a JSON file with the evidence "
             "its tool supplies — {release_commit_sha, candidate_sha, status (committed | "
             "tagged | completed), tag, artifacts: [{path, sha256}]}")
    p_rel_recon.add_argument("--project", "-P")
    p_rel_recon.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_recon.epilog = (
        "The version spelling is resolved first (the stored one: a request for 1.1.0 of a "
        "release stored as 1.1 records 1.1). Commits titled `release: v<V>` are candidates, "
        "each verified (one parent, the version-file contract, the changelog section, the "
        "version fields, and the WHOLE older changelog history against the parent: a release "
        "commit that removed or edited an older section — also a typo fix — is shown as a "
        "diff and never verified); a tag of V → completed, a single verified release commit "
        "without a tag → committed (the output names how to finish it: `dct release "
        "takeover`, or the ledger path), several → all listed, choose with --release-commit. "
        "The version-file contract is shown as UNKNOWN or confirmed; outside git an unknown "
        "one refuses — confirm it with `dct release version-files`. A project whose release "
        "executor is 'own' is not parsed by these rules: pass --evidence (its tool's own "
        "format; dct verifies the commit, the candidate, each artifact's hash at the commit "
        "and the tag). Any contradiction is refused; nothing is re-stamped and nothing in "
        "the repository is modified. The executor shown is assigned to the record now — it "
        "is not who made the release.")

    p_rel_take = p_release_sub.add_parser(
        "takeover", help="Hand an existing release record without a consent to dct, from its "
                         "verified artifacts (then `resume` performs only the missing effects)")
    p_rel_take.add_argument("--version", required=True, help="The release (X.Y or X.Y.Z)")
    _add_push_flags(p_rel_take)
    p_rel_take.add_argument(
        "--takeover-digest",
        help="Required with --confirm: the takeover_digest the preview printed")
    p_rel_take.add_argument("--confirm", action="store_true",
                            help="Record the takeover (without: show what would be recorded)")
    p_rel_take.add_argument("--project", "-P")
    p_rel_take.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_take.epilog = (
        "For a record dct did not stamp with the owner's consent — a reconstruction that "
        "ended at 'committed', a record stamped through MCP release_changelog, an older "
        "record — whose release commit EXISTS. dct verifies the artifacts again (the release "
        "commit, its single parent, the changelog section and the older history, the version "
        "files) and compares them with the record; then it shows the consent it would record "
        "(version, entries, candidate, release commit, tree, artifact hashes, the push target "
        "or --no-push) and its takeover_digest. With --takeover-digest D --confirm it records "
        "the consent and the proposal derived from those artifacts. No new bump, no re-stamp, "
        "no file, commit, tag or push, and nothing recorded is changed; afterwards `dct "
        "release resume` performs only what is missing (the tag, the push). Refused for an "
        "'own' record, a record that already has a consent, a terminal record, and when no "
        "release commit verifies or the artifacts contradict the record — the ledger path "
        "(`dct clog release-advance`) stays. " + RELEASE_LOCK_EPILOG)

    p_rel_vf = p_release_sub.add_parser(
        "version-files", help="Show / confirm the project's version-file contract (which "
                              "files every release changes)")
    p_rel_vf_set = p_rel_vf.add_mutually_exclusive_group()
    p_rel_vf_set.add_argument("--set", metavar="PATHS",
                              help="Comma-separated repo-relative paths to confirm")
    p_rel_vf_set.add_argument("--none", action="store_true",
                              help="Confirm that the project has NO version files")
    p_rel_vf.add_argument("--confirm", action="store_true",
                          help="Write the contract (without: show the proposal only)")
    p_rel_vf.add_argument("--project", "-P")
    p_rel_vf.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_vf.epilog = (
        "Without flags: shows the contract — UNKNOWN (never confirmed) is not the same as a "
        "confirmed empty one. --set / --none without --confirm show the proposal; with "
        "--confirm the contract is stored (the earlier one goes to its history). A release "
        "made with `dct release prepare` / `apply` learns the contract from its approved "
        "proposal — this command is for a project that needs one without such a release (a "
        "reconstruction outside git refuses an unknown contract). " + RELEASE_LOCK_EPILOG)

    # _task-mirror (internal: worker for hooks/dct-task-mirror.sh)
    p_tm = sub.add_parser(
        "_task-mirror",
        help="Internal — worker for dct-task-mirror.sh hook. Reads hook JSON on stdin.",
    )
    p_tm.add_argument("--client", choices=("claude", "codex"), default="claude")
    p_tm.add_argument("--turn-id", default="")
    p_tm.add_argument("--session-id", default="")
    p_tm.add_argument("--project", default="")

    # db (K-84: the explicit bootstrap read-only commands point to)
    p_db = sub.add_parser("db", help="Database schema maintenance")
    p_db_sub = p_db.add_subparsers(dest="db_command")
    p_db_sub.add_parser(
        "upgrade",
        help="Create the database or upgrade its schema (what every write command "
             "runs implicitly; read-only commands never do)",
    )

    # config
    p_config = sub.add_parser("config", help="Inspect dct configuration")
    p_config_sub = p_config.add_subparsers(dest="config_command")
    p_config_sub.add_parser(
        "path",
        help="Show resolved config/data/cache/state locations and their sources",
    )
    p_config_get = p_config_sub.add_parser("get", help="Read a dotted config key")
    p_config_get.add_argument(
        "key",
        help="e.g. integrations.claude.task_mirror, classification.model, plans.auto_resolve_uncertain",
    )

    # checkpoint
    p_cp = sub.add_parser("checkpoint", aliases=["cp"], help="Manage item checkpoints (acceptance criteria)")
    p_cp_sub = p_cp.add_subparsers(dest="checkpoint_command")

    p_cp_add = p_cp_sub.add_parser("add", help="Add a checkpoint to an item")
    p_cp_add.add_argument("item_id", type=int)
    p_cp_add.add_argument("text")
    p_cp_add.add_argument(
        "--kind",
        default="task",
        help="Checkpoint kind: task (default, non-blocking) or a gate kind "
             "(dogfood/security/approval/review). Gates block resolve_item.",
    )

    p_cp_list = p_cp_sub.add_parser("list", help="List checkpoints for an item with progress")
    p_cp_list.add_argument("item_id", type=int)

    p_cp_done = p_cp_sub.add_parser("done", help="Mark checkpoint as done")
    p_cp_done.add_argument("checkpoint_id", type=int)

    p_cp_reject = p_cp_sub.add_parser("reject", help="Mark checkpoint as rejected (scope change, N/A)")
    p_cp_reject.add_argument("checkpoint_id", type=int)

    p_cp_reopen = p_cp_sub.add_parser("reopen", help="Revert checkpoint to pending")
    p_cp_reopen.add_argument("checkpoint_id", type=int)

    p_cp_delete = p_cp_sub.add_parser("delete", help="Soft-delete a checkpoint (prefer reject for ledger)")
    p_cp_delete.add_argument("checkpoint_id", type=int)

    # sprint (lifecycle: planned → active → [deferred] → completed/abandoned)
    p_sprint = sub.add_parser("sprint", help="Manage sprints")
    p_sprint_sub = p_sprint.add_subparsers(dest="sprint_command")

    p_sp_list = p_sprint_sub.add_parser("list", help="List sprints")
    p_sp_list.add_argument("--project", help="Project slug (default: auto-detect from CWD)")
    p_sp_list.add_argument("--status", help="Filter by status "
                                            "(planned/active/deferred/completed/abandoned)")
    p_sp_list.add_argument("--kind", help="Filter by kind (sprint/phase/milestone/block)")

    p_sp_show = p_sprint_sub.add_parser("show", help="Show sprint details")
    p_sp_show.add_argument("id", type=int)

    p_sp_create = p_sprint_sub.add_parser("create", help="Create a sprint")
    p_sp_create.add_argument("name")
    p_sp_create.add_argument("--project", help="Project slug (default: auto-detect from CWD)")
    p_sp_create.add_argument("--code", help="Short handle, unique per project")
    p_sp_create.add_argument("--kind", help="sprint (default) / phase / milestone / block")
    p_sp_create.add_argument("--status", help="planned (default) / active / completed / abandoned "
                                              "('deferred' is not a valid creation status)")
    p_sp_create.add_argument("--goal", help="Free-text objective")

    p_sp_update = p_sprint_sub.add_parser(
        "update", help="Update a sprint (status/name/goal/code/kind)"
    )
    p_sp_update.add_argument("id", type=int)
    p_sp_update.add_argument("--status", help="New status (transition whitelist enforced)")
    p_sp_update.add_argument("--name", help="New name")
    p_sp_update.add_argument("--goal", help="New goal")
    p_sp_update.add_argument("--code", help="New short handle (unique per project)")
    p_sp_update.add_argument("--kind", help="New kind")

    _VERB_STATUS = {"defer": "deferred", "activate": "active", "complete": "completed"}
    for _verb, _st in _VERB_STATUS.items():
        p_verb = p_sprint_sub.add_parser(
            _verb, help=f"Shortcut for `sprint update <id> --status {_st}`"
        )
        p_verb.add_argument("id", type=int)
        p_verb.set_defaults(_forced_status=_st)

    # tags (global, user-scoped dictionary — #611)
    p_tags = sub.add_parser("tags", help="Manage the tag dictionary")
    p_tags_sub = p_tags.add_subparsers(dest="tags_command")

    p_tags_sub.add_parser("list", help="List all tags with usage counts (global)")

    p_tag_add = p_tags_sub.add_parser("add", help="Register a tag")
    p_tag_add.add_argument("name")
    p_tag_add.add_argument("--description", "-d")
    p_tag_add.add_argument("--force", action="store_true",
                           help="Create even when the name is similar to an "
                                "existing tag (the similarity guard is advisory)")

    p_tag_merge = p_tags_sub.add_parser(
        "merge", help="Move every active link of <from> onto <into>, retire <from>"
    )
    p_tag_merge.add_argument("from_name", metavar="from")
    p_tag_merge.add_argument("into_name", metavar="into")

    # triage findings ledger (#909, #930) — record / suggest / promote go
    # through MCP; the page build and the owner's decisions also run here
    p_triage = sub.add_parser("triage", help="Triage findings ledger (review findings)")
    p_triage_sub = p_triage.add_subparsers(dest="triage_command")

    def _triage_cycle_args(p):
        p.add_argument("cycle", nargs="?",
                       help="Cycle code within the project (never read as an id)")
        p.add_argument("--cycle-id", type=int, metavar="N",
                       help="The cycle's global id instead of its code")
        p.add_argument("--project",
                       help="Project slug (default for a code: auto-detect from CWD; "
                            "with --cycle-id: checked when given)")

    p_tr_export = p_triage_sub.add_parser(
        "export",
        help="Print a cycle's triage-page constants CLOSED / SUGGESTED / CARDS (read-only)",
    )
    _triage_cycle_args(p_tr_export)
    p_tr_export.add_argument("--json", action="store_true",
                             help="Print the whole export {cycle, cards, closed, suggested} "
                                  "as JSON instead of the constant declarations")

    p_tr_page = p_triage_sub.add_parser(
        "page", help="Build a cycle's triage page from its export (read-only on the database)")
    _triage_cycle_args(p_tr_page)
    p_tr_page.add_argument("--out", required=True, metavar="FILE",
                           help="The page file to write (its directory must exist; a "
                                "directory = <dir>/<safe code>.html)")
    p_tr_page.add_argument("--template", metavar="PATH",
                           help="Page template (default: [triage] template in dct.toml, else the template dct ships)")
    p_tr_page.add_argument("--topic", help="Page title (default: the cycle's topic)")
    p_tr_page.add_argument("--context",
                           help="Eyebrow text (default: the cycle's scope, else its code)")
    p_tr_page.add_argument("--lede", help="Lede text (default: card count, rubric, cycle "
                                          "and the legend)")
    p_tr_page.add_argument("--plain", metavar="FILE",
                           help="Plain-language layer (JSON {by, date, cards: {K-n: …}}): "
                                "validated against the cycle's cards, then copied as "
                                "plain.json beside the page")
    p_tr_page.add_argument("--force", action="store_true",
                           help="Replace an existing --out file (and plain.json)")

    p_tr_decide = p_triage_sub.add_parser(
        "decide", help="Record the owner's pasted decision text (the page's generated text)")
    _triage_cycle_args(p_tr_decide)
    p_tr_decide.add_argument("--from-text", required=True, metavar="FILE",
                             help="File with the owner's decision text ('-' = stdin)")
    p_tr_decide.add_argument("--decided-by", required=True,
                             help="The PERSON who decided (e.g. owner)")
    p_tr_decide.add_argument("--source", required=True,
                             help="Where the owner's words are recorded (e.g. 'chat 2026-10-14')")

    p_tr_accept = p_triage_sub.add_parser(
        "accept", help="The owner accepts the agent's proposals as final decisions")
    _triage_cycle_args(p_tr_accept)
    p_tr_accept.add_argument("--expected-by", required=True,
                             help="Who made the proposals the owner saw (refused when any "
                                  "proposal is by someone else)")
    p_tr_accept.add_argument("--expected-digest", required=True, metavar="HEX",
                             help="SUGGESTED.digest of the export the published page was "
                                  "built from (refused when the cards or proposals changed "
                                  "since)")
    p_tr_accept.add_argument("--decided-by", required=True,
                             help="The PERSON who accepted (e.g. owner)")
    p_tr_accept.add_argument("--source", required=True,
                             help="Where the owner's acceptance is recorded")
    p_tr_accept.add_argument("--overwrite", action="store_true",
                             help="Replace decisions already recorded on proposed cards")

    p_tr_close = p_triage_sub.add_parser(
        "close", help="Close a cycle with the owner's recorded answers")
    _triage_cycle_args(p_tr_close)
    p_tr_close.add_argument("--source", required=True,
                            help="Where the owner's answers are recorded")
    p_tr_close.add_argument("--note", action="append", metavar="TEXT",
                            help="A paragraph of the closed panel (repeatable)")

    # pickup (session-start op — snapshot + handoff consume, #413)
    p_pickup = sub.add_parser(
        "pickup",
        help="Session-start op: project snapshot + consume the pending handoff",
    )
    p_pickup.add_argument("--project", help="Project slug (default: auto-detect from CWD)")
    p_pickup.add_argument("--since", default="last_release",
                          help="Activity window anchor for recent_changes: "
                               "last_release (default) | all | <N>h (e.g. 24h) "
                               "| ISO date")
    p_pickup.add_argument("--include", default=None,
                          help="Comma-separated snapshot sections (sprint,handoff,gates,"
                               "decisions,recent_changes). 'none' = summary only.")
    p_pickup.add_argument("--consume", choices=["auto", "always", "never"], default="never",
                          help="never (default): pure read — pickup does not write; "
                               "auto: consume an unambiguous single candidate; "
                               "always: consume or fail (autonomous flows)")
    p_pickup.add_argument("--handoff-id", type=int, default=None,
                          help="Consume this specific handoff (wins over --scope)")
    p_pickup.add_argument("--scope", default=None,
                          help="Only consider handoffs carrying this scope tag")
    p_pickup.add_argument("--consumed-by", default=None,
                          help="Session identity stamped on consume "
                               "(default: '<client> @ <UTC minute>', echoed)")
    p_pickup.add_argument("--json", action="store_true", help="Print the raw snapshot as JSON")

    # handoff (session-end ledger)
    p_handoff = sub.add_parser("handoff", help="Manage session handoffs")
    p_ho_sub = p_handoff.add_subparsers(dest="handoff_command")

    p_ho_create = p_ho_sub.add_parser(
        "create",
        help="Session-end op: set sprint membership + leave a handoff prompt (one transaction)",
    )
    p_ho_create.add_argument("--prompt", default=None, help="Handoff prompt text")
    p_ho_create.add_argument("--prompt-file", default=None,
                             help="Read the prompt from a file ('-' = stdin)")
    p_ho_create.add_argument("--created-by", default=None,
                             help="Session identity (default: '<client> @ <UTC minute>', echoed)")
    p_ho_create.add_argument("--items", default=None,
                             help="REPLACE the sprint's OPEN membership with these ids "
                                  "(e.g. 22,21,6) — a conscious replan; resolved members "
                                  "stay as history. Mutually exclusive with --add-items/"
                                  "--remove-items. Omit all item flags to keep the sprint.")
    p_ho_create.add_argument("--add-items", default=None,
                             help="ADD these item ids to the target sprint (additive edge)")
    p_ho_create.add_argument("--remove-items", default=None,
                             help="CUT these OPEN items from the sprint's scope "
                                  "(terminal members refuse — they are history)")
    p_ho_create.add_argument("--sprint", default=None,
                             help="Target sprint id or code (required when several are active)")
    p_ho_create.add_argument("--scope", default=None, help="Scope tag")
    p_ho_create.add_argument("--context-summary", default=None,
                             help="Context summary snapshot, stored verbatim")
    p_ho_create.add_argument("--context-summary-file", default=None,
                             help="Read the context summary from a file ('-' = stdin); "
                                  "wins over --context-summary")
    p_ho_create.add_argument("--project", help="Project slug (default: auto-detect from CWD)")
    p_ho_create.add_argument("--json", action="store_true", help="Print the raw result as JSON")

    p_ho_update = p_ho_sub.add_parser("update", help="Amend an unconsumed handoff in place")
    p_ho_update.add_argument("id", type=int, help="Handoff id")
    p_ho_update.add_argument("--prompt", default=None, help="New prompt text")
    p_ho_update.add_argument("--prompt-file", default=None, help="Read new prompt from file ('-' = stdin)")
    p_ho_update.add_argument("--scope", default=None, help="New scope tag")
    p_ho_update.add_argument("--context-summary", default=None, help="New context summary snapshot")
    p_ho_update.add_argument("--context-summary-file", default=None,
                             help="Read the new context summary from a file ('-' = stdin); "
                                  "wins over --context-summary")

    # web (read-only browser dashboard daemon — v0.4.1)
    p_web = sub.add_parser("web", help="Read-only browser dashboard daemon")
    p_web_sub = p_web.add_subparsers(dest="web_command")

    p_web_start = p_web_sub.add_parser("start", help="Start the dashboard daemon")
    p_web_start.add_argument("--port", type=int, help="Preferred port (overrides config)")
    p_web_start.add_argument("--host", help="Bind address (default 127.0.0.1)")
    p_web_start.add_argument("--open", action="store_true", help="Open the browser after start")
    p_web_start.add_argument("--no-fallback", action="store_true",
                             help="Fail if the preferred port is busy instead of scanning upward")

    p_web_sub.add_parser("stop", help="Stop the daemon (SIGTERM via pidfile)")
    p_web_sub.add_parser("status", help="Show daemon pid + URL")

    return parser


def cmd_migrate_home(args):
    """Move a legacy ~/.claude installation to the platform-neutral home
    (dct/migrate.py planner/executor; idempotent, never overwrites)."""
    steps = migrate.plan_migration()
    if not steps:
        if (paths.config_dir() / "dct.toml").exists():
            print("Nothing to migrate — already using the platform-neutral location.")
        else:
            print("Nothing to migrate — no legacy dct installation found in ~/.claude.")
        return
    if args.dry_run:
        for step in steps:
            print(f"would move {step.src} -> {step.dst}")
        print(f"{len(steps)} step(s); run `dct migrate-home` without --dry-run to apply.")
        return
    for message in migrate.execute_migration(steps):
        print(message)


DB_COMMAND_MAP = {
    "upgrade": cmd_db_upgrade,
}

COMMAND_MAP = {
    "integrate": cmd_integrate,
    "doctor": cmd_doctor,
    "init": cmd_init,
    "server": cmd_server,
    "upgrade": cmd_upgrade,
    "migrate-home": cmd_migrate_home,
    "add": cmd_add,
    "list": cmd_list,
    "show": cmd_show,
    "update": cmd_update,
    "resolve": cmd_resolve,
    "note": cmd_note,
    "delete": cmd_delete,
    "pickup": cmd_pickup,
    "_task-mirror": cmd_internal_task_mirror,
    "_web-serve": cmd_internal_web_serve,
}

HOOKS_COMMAND_MAP = {
    "wire": cmd_hooks_wire,
}

PROJECT_COMMAND_MAP = {
    "add": cmd_project_add,
    "list": cmd_project_list,
    "remove": cmd_project_remove,
    "detect": cmd_project_detect,
    "set-version": cmd_project_set_version,
    "rename": cmd_project_rename,
    "changelog": cmd_project_changelog,
    "manages-changelog": cmd_project_manages_changelog,
    "changelog-path": cmd_project_changelog_path,
    "release-executor": cmd_project_release_executor,
    "changelog-pr-refs": cmd_project_changelog_pr_refs,
    "trailers": cmd_project_trailers,
    "version-sync-script": cmd_project_version_sync_script,
}

PLAN_COMMAND_MAP = {
    "set-section-type": cmd_plan_set_section_type,
    "resolve-uncertain": cmd_plan_resolve_uncertain,
    "rescan": cmd_plan_rescan,
}

CLOG_COMMAND_MAP = {
    "import-released": cmd_clog_import_released,
    "add": cmd_clog_add,
    "list": cmd_clog_list,
    "count": cmd_clog_count,
    "release": cmd_clog_release,
    "release-status": cmd_clog_release_status,
    "release-advance": cmd_clog_release_advance,
    "release-abandon": cmd_clog_release_abandon,
    "candidates": cmd_clog_candidates,
    "update": cmd_clog_update,
    "delete": cmd_clog_delete,
    "export": cmd_clog_export,
}

RELEASE_COMMAND_MAP = {
    "plan": cmd_release_plan,
    "status": cmd_clog_release_status,
    "attest": cmd_release_attest,
    "apply": cmd_release_apply,
    "prepare": cmd_release_prepare,
    "resume": cmd_release_resume,
    "abandon": cmd_release_abandon,
    "reconstruct": cmd_release_reconstruct,
    "takeover": cmd_release_takeover,
    "version-files": cmd_release_version_files,
}

CONFIG_COMMAND_MAP = {
    "get": cmd_config_get,
    "path": cmd_config_path,
}

WEB_COMMAND_MAP = {
    "start": cmd_web_start,
    "stop": cmd_web_stop,
    "status": cmd_web_status,
}

CHECKPOINT_COMMAND_MAP = {
    "add": cmd_checkpoint_add,
    "list": cmd_checkpoint_list,
    "done": cmd_checkpoint_done,
    "reject": cmd_checkpoint_reject,
    "reopen": cmd_checkpoint_reopen,
    "delete": cmd_checkpoint_delete,
}

HANDOFF_COMMAND_MAP = {
    "create": cmd_handoff_create,
    "update": cmd_handoff_update,
}

SPRINT_COMMAND_MAP = {
    "list": cmd_sprint_list,
    "show": cmd_sprint_show,
    "create": cmd_sprint_create,
    "update": cmd_sprint_update,
    # verb-sugar aliases route onto the same update path
    "defer": cmd_sprint_update,
    "activate": cmd_sprint_update,
    "complete": cmd_sprint_update,
}

TAGS_COMMAND_MAP = {
    "list": cmd_tag_list,
    "add": cmd_tag_add,
    "merge": cmd_tag_merge,
}

TRIAGE_COMMAND_MAP = {
    "export": cmd_triage_export,
    "page": cmd_triage_page,
    "decide": cmd_triage_decide,
    "accept": cmd_triage_accept,
    "close": cmd_triage_close,
}


# ── Passive update nudge (#356) ──────────────────────────────────────────────

# (command, subcommand) pairs that hooks / tooling call non-interactively — a
# nudge on stderr would corrupt their machine-read output, so never surface it.
_NUDGE_HOOK_SUBCOMMANDS = frozenset({
    ("project", "detect"),
    ("project", "manages-changelog"),
    ("clog", "count"),
    ("config", "get"),
})


def _nudge_suppressed(args) -> bool:
    """True when the passive update nudge must NOT be printed for this command.

    Suppressed for: any underscore-prefixed command (internal, e.g. _task-mirror),
    any underscore-prefixed subcommand, and the hook-driven machine-output pairs
    in ``_NUDGE_HOOK_SUBCOMMANDS`` (project detect / manages-changelog, clog
    count, config get). The caller additionally gates on ``stderr.isatty()``.
    """
    cmd = getattr(args, "command", None) or ""
    if not cmd or cmd.startswith("_") or cmd in ("doctor", "integrate"):
        return True
    sub = (
        getattr(args, "project_command", None)
        or getattr(args, "clog_command", None)
        or getattr(args, "config_command", None)
    )
    if isinstance(sub, str) and sub.startswith("_"):
        return True
    return (cmd, sub) in _NUDGE_HOOK_SUBCOMMANDS


NUDGE_JOIN_TIMEOUT = 1.5  # bounded wait for the background PyPI probe at exit


def _register_probe_join(thread) -> None:
    """Join the background update probe (bounded) at interpreter exit.

    The probe runs in a daemon thread, so a short-lived CLI command would
    otherwise exit before it finishes and the cache would never populate. Joining
    at ``atexit`` — AFTER the command's own work is done — lets quick commands
    still warm the cache without the probe blocking the command. Offline worst
    case is bounded by the probe's ``NETWORK_TIMEOUT`` (2s) racing this 1.5s join;
    the daemon flag guarantees the process still exits either way. No-op when the
    probe was not started this invocation (``thread is None``).
    """
    if thread is None:
        return
    atexit.register(thread.join, NUDGE_JOIN_TIMEOUT)


def _maybe_nudge(args) -> None:
    """Print the cached update notice to stderr for interactive human commands.

    Also kicks off the non-blocking background cache refresh and registers a
    bounded ``atexit`` join so a short command still populates the cache before
    the interpreter exits. Entirely best-effort: any failure (or a non-tty,
    suppressed, or opted-out command) is silent and never affects the command's
    own output or exit code.
    """
    # A passive nicety must never break a command.
    with contextlib.suppress(Exception):
        if not sys.stderr.isatty() or _nudge_suppressed(args):
            return
        from dct.config import load_config
        from dct.update import check_for_update, get_cached_notice

        cfg = load_config()  # single load; both halves honor [update] check
        thread = check_for_update(cfg)  # background refresh; returns immediately
        _register_probe_join(thread)    # bounded join at exit, after command work
        notice = get_cached_notice(config=cfg)
        if notice:
            print(notice, file=sys.stderr)


def main():
    # Creation-provenance context for every write this process makes
    # (CP #1236). `dct server` re-stamps with source="mcp" in cmd_server.
    from dct.core.provenance import from_environment, set_provenance

    set_provenance(from_environment("cli"))

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "init":
        _require_init_terminal()
    _maybe_nudge(args)

    if args.command == "project":
        handler = PROJECT_COMMAND_MAP.get(args.project_command)
        if not handler:
            parser.parse_args(["project", "--help"])
            return
        handler(args)
    elif args.command == "clog":
        handler = CLOG_COMMAND_MAP.get(args.clog_command)
        if not handler:
            parser.parse_args(["clog", "--help"])
            return
        handler(args)
    elif args.command == "release":
        handler = RELEASE_COMMAND_MAP.get(args.release_command)
        if not handler:
            parser.parse_args(["release", "--help"])
            return
        handler(args)
    elif args.command in ("checkpoint", "cp"):
        handler = CHECKPOINT_COMMAND_MAP.get(args.checkpoint_command)
        if not handler:
            parser.parse_args(["checkpoint", "--help"])
            return
        handler(args)
    elif args.command == "handoff":
        handler = HANDOFF_COMMAND_MAP.get(args.handoff_command)
        if not handler:
            parser.parse_args(["handoff", "--help"])
            return
        handler(args)
    elif args.command == "sprint":
        handler = SPRINT_COMMAND_MAP.get(args.sprint_command)
        if not handler:
            parser.parse_args(["sprint", "--help"])
            return
        handler(args)
    elif args.command == "tags":
        handler = TAGS_COMMAND_MAP.get(args.tags_command)
        if not handler:
            parser.parse_args(["tags", "--help"])
            return
        handler(args)
    elif args.command == "triage":
        handler = TRIAGE_COMMAND_MAP.get(args.triage_command)
        if not handler:
            parser.parse_args(["triage", "--help"])
            return
        handler(args)
    elif args.command == "plan":
        handler = PLAN_COMMAND_MAP.get(args.plan_command)
        if not handler:
            parser.parse_args(["plan", "--help"])
            return
        handler(args)
    elif args.command == "db":
        handler = DB_COMMAND_MAP.get(args.db_command)
        if not handler:
            parser.parse_args(["db", "--help"])
            return
        handler(args)
    elif args.command == "config":
        handler = CONFIG_COMMAND_MAP.get(args.config_command)
        if not handler:
            parser.parse_args(["config", "--help"])
            return
        handler(args)
    elif args.command == "web":
        handler = WEB_COMMAND_MAP.get(args.web_command or "start")
        if not handler:
            parser.parse_args(["web", "--help"])
            return
        handler(args)
    elif args.command == "hooks":
        handler = HOOKS_COMMAND_MAP.get(args.hooks_command)
        if not handler:
            parser.parse_args(["hooks", "--help"])
            return
        handler(args)
    else:
        handler = COMMAND_MAP.get(args.command)
        if handler:
            handler(args)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
