"""CLI entry point for dct (Deterministic Context Tracker)."""

import argparse
import atexit
import contextlib
import os
import signal
import socket
import subprocess
import sys
from datetime import UTC
from pathlib import Path

from dct import __version__, migrate, paths
from dct.config import Config, load_config, save_config
from dct.core.engine import bootstrap_engine
from dct.core.items import TERMINAL_STATUSES
from dct.core.releases import RELEASE_FLOW
from dct.db import create_tables, get_engine

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

def cmd_init(args):
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
    from dct.integrations.claude import install_hook, install_skills, register_mcp

    ok, msg = register_mcp(config)
    if ok:
        print(f"\n  ✓ MCP server — {msg}")
    if install_hook(config):
        print("  ✓ Global hook installed")
    if install_skills(config):
        print("  ✓ Skills installed")

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

    # Attach the CLAUDE.md snippet (idempotent; consent-gated — it teaches the
    # assistant when to reach for dct, so plain "track this" works reliably).
    # args.attach_snippet: True (--attach-snippet) / False (--no-attach-snippet)
    # / None (ask on a tty, skip when non-interactive).
    attach = getattr(args, "attach_snippet", None)
    if attach is None:
        if sys.stdin.isatty():
            answer = input(
                "Add dct usage instructions to ~/.claude/CLAUDE.md? [Y/n]: "
            ).strip().lower()
            attach = answer != "n"
        else:
            attach = False
    if attach:
        from dct.integrations.claude import attach_claude_md_snippet
        try:
            result = attach_claude_md_snippet()
        except ValueError as e:
            print(f"  ⚠ CLAUDE.md snippet skipped: {e}")
        else:
            verb = {
                "added": "added to",
                "refreshed": "refreshed in",
                "unchanged": "already present in",
            }[result["status"]]
            print(f"  ✓ dct snippet {verb} {result['path']}")
            if result["backup"]:
                print(f"    (backup: {result['backup']})")
    else:
        print(
            "  ⚠ Snippet not attached — re-run with --attach-snippet, or paste "
            "dct/assets/claude-md-snippet.md yourself."
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


def cmd_project_manages_changelog(args):
    """Hook query — exit 0 if the project opts into dct changelog enforcement,
    else exit 1. Silent by design (the changelog hook reads only the exit code).
    """
    from dct.core.projects import project_manages_changelog

    engine = _get_engine()
    sys.exit(0 if project_manages_changelog(engine, args.slug) else 1)


def cmd_project_changelog_path(args):
    """Get or set a project's changelog file location, relative to its root (#225).

    With a relpath → set it (validated to stay within the project root). Without
    → print the current relpath and its resolved absolute path.
    """
    from dct.core.projects import (
        get_project,
        resolve_changelog_path,
        set_changelog_path,
    )

    engine = _get_engine()

    if args.relpath is None:
        project = get_project(engine, args.slug)
        if not project:
            print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
            sys.exit(1)
        rel = project.get("changelog_path") or "changelog.md"
        print(f"{args.slug}: {rel}")
        resolved = resolve_changelog_path(project)
        if resolved:
            print(f"  → {resolved}")
        return

    try:
        project = set_changelog_path(engine, args.slug, args.relpath)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    if not project:
        print(f"✗ Project '{args.slug}' not found", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {args.slug}: changelog-path → {project['changelog_path']}")
    resolved = resolve_changelog_path(project)
    if resolved:
        print(f"  → {resolved}")


# ── Plan rescan (v0.2.0) ─────────────────────────────────────────────────────

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

    try:
        items = list_items(engine, **kwargs)
    except ValueError as e:
        # Unknown tag — the core error carries the known-names suggestion.
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
        prompt = _read_text_arg(args.prompt, args.prompt_file)
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
            context_summary=args.context_summary,
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
        prompt = _read_text_arg(args.prompt, args.prompt_file)
        result = update_handoff(
            engine, args.id,
            prompt=prompt or None,
            scope=args.scope,
            context_summary=args.context_summary,
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
            ["#", "cp", "kind", "prio", "title", "text"],
            [
                [
                    str(g["item_id"]), str(g["checkpoint_id"]), g.get("kind") or "",
                    g.get("priority") or "", _strip_controls(g.get("item_title")),
                    _strip_controls(g.get("text")),
                ]
                for g in gates
            ],
            max_widths={4: 30, 5: 50},
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

    engine = _get_engine()
    consume = {"auto": None, "always": True, "never": False}[args.consume]
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


_VERSION_SYNC_SCRIPTS = ("scripts/dct-version-sync.sh", "scripts/version-sync.sh")


def _release_project_root(project: dict) -> tuple[str | None, str | None, str | None]:
    """(project root, checkout root, HEAD commit) of the release (K-34, K-41, K-52).

    The project root is the directory the version-sync script is looked up
    in and RUN FROM; the checkout root (git toplevel of the current checkout)
    and its HEAD (a full commit id) are pinned in the release record.
    Resolved STRICTLY (gitinfo.verify_release_checkout) — the best-effort
    readers' "None" never means "no git" here:
    - Registered path inside git: the caller's cwd must be a checkout of the
      SAME repository (main checkout or any linked worktree, never a nested
      foreign repo or a submodule) with a first commit; the result is that
      checkout's `project_root` (a monorepo subproject maps to the same
      subdirectory of the current worktree), `repo_root` and HEAD.
    - Registered path CONFIRMED outside git: (registered path, None, None) —
      the documented path of a project without git.
    - Anything not confirmed — not a checkout, a deleted cwd or project
      directory, git missing / timing out / failing, a broken repository, a
      repository without commits — SystemExit(1) BEFORE anything is
      stamped: a mutating release never silently switches to the main
      checkout.
    - No registered path: (None, None, None) — no version-sync, nothing pinned.
    """
    from dct.core.gitinfo import (
        CheckoutVerificationError,
        NotACheckoutError,
        verify_release_checkout,
    )

    path = project.get("path")
    if not path:
        return None, None, None
    try:
        checkout = verify_release_checkout(path)
    except NotACheckoutError:
        print(
            f"✗ The current directory is not a checkout of project '{project['slug']}' "
            f"({path}) — run `dct clog release` from the project's main checkout or "
            f"one of its linked worktrees; nothing released.",
            file=sys.stderr,
        )
        sys.exit(1)
    except CheckoutVerificationError as e:
        print(
            f"✗ Cannot verify the release checkout of project '{project['slug']}' "
            f"({path}): {e} — nothing released.",
            file=sys.stderr,
        )
        sys.exit(1)
    if checkout is None:
        return path, None, None
    return checkout["project_root"], checkout["repo_root"], checkout["head"]


def _run_version_sync(root: str, version: str) -> None:
    """Run the first version-sync script found under `root`, from `root`.

    A non-zero exit stops the CLI with that exit code (the DB release is
    already recorded — re-running the same version resumes the sync).
    """
    import os
    import subprocess

    for sync_path in _VERSION_SYNC_SCRIPTS:
        full_path = os.path.join(root, sync_path)
        if not os.path.isfile(full_path):
            continue
        print(f"  Running {sync_path}...")
        code = subprocess.run(["bash", full_path, version], cwd=root, check=False).returncode
        if code != 0:
            print(
                f"✗ version-sync script failed (exit {code}): {sync_path} in {root}. "
                f"The database release of {version} is recorded — fix the script and "
                f"re-run `dct clog release {version}` to finish the sync.",
                file=sys.stderr,
            )
            sys.exit(code)
        return


def cmd_clog_release(args):
    """`dct clog release` — stamp entries (core release_entries), then sync files.

    Checks that conflict with idempotency are left to core (K-31): a retry of
    a released version is a no-op there, and mode 'all' with nothing
    unreleased refuses unless the call is a no-op (core rule for every
    caller, K-62) — a version-only release needs `--no-entries`. The
    checkout is verified STRICTLY BEFORE stamping (_release_project_root,
    K-52); its HEAD and root are pinned in the release record (K-41) as
    candidate_sha / checkout_root. Version-sync runs only when the project's
    current_version equals the requested version after the call — a real
    release, a version-only release, or an explicit resume of the current
    version (released=0) — never for a historical no-op (requested version
    != current_version). The record is NOT advanced here: the changelog
    file, commit and tag steps belong to the /release workflow
    (`dct clog release-advance`).
    """
    from dct.core.changelog import release_entries
    from dct.core.projects import get_project
    from dct.core.version import same_version

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)

    if not args.version and not args.bump:
        print("✗ Provide a version or --patch/--minor/--major", file=sys.stderr)
        sys.exit(1)
    try:
        entry_ids = [] if args.no_entries else _ids(args.entries, "--entries", "entry")
    except ValueError as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    project = get_project(engine, project_slug)
    if not project:
        print(f"✗ Project '{project_slug}' not found", file=sys.stderr)
        sys.exit(1)
    root, checkout_root, head = _release_project_root(project)

    try:
        result = release_entries(
            engine, project_slug, args.version or None,
            bump_type=None if args.version else args.bump, entry_ids=entry_ids,
            candidate_sha=head,
            checkout_root=checkout_root,
        )
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)

    version, current = result["version"], result["current_version"]
    previous = project.get("current_version")
    if args.bump and not args.version:
        print(f"  {previous} → {version} ({args.bump})")
    if result["released"] or not same_version(previous, current):
        print(f"✓ Released {result['released']} entries as {version} "
              f"(mode: {result['mode']}) — current version: {current}")
    else:
        print(f"✓ {version} already released — nothing to stamp "
              f"(mode: {result['mode']}); current version: {current}")
    if result["already_released"]:
        print(f"  already in {version}: {len(result['already_released'])} "
              f"({', '.join(f'#{i}' for i in result['already_released'])})")
    if result.get("warning"):
        print(f"  ⚠ {result['warning']}", file=sys.stderr)
    record = result.get("release")
    if record:
        print(f"  release record {record['version']}: {record['status']} — not advanced "
              f"by this command (finish with /release or `dct clog release-advance`)")

    if not same_version(current, version):
        print(f"  version-sync skipped: {version} is not the current version ({current})")
        return
    if root:
        _run_version_sync(root, version)


def _print_release_record(record: dict) -> None:
    ids = ", ".join(f"#{i}" for i in record["entry_ids"]) or "(none — version only)"
    print(f"Release {record['version']}: {record['status']} (mode: {record['mode']})")
    print(f"  entries:       {ids}")
    print(f"  candidate sha: {record['candidate_sha'] or '-'}")
    print(f"  release commit: {record['release_commit_sha'] or '-'}")
    print(f"  checkout root: {record['checkout_root'] or '-'}")
    manifest = record["manifest"] or {}
    print(f"  manifest:      {', '.join(sorted(manifest)) or '-'}")
    proof = record["proof"] or {}
    print(f"  proof:         {len(proof)} of {len(record['entry_ids'])} entries"
          if proof else "  proof:         -")
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

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        record = get_release(engine, project_slug, args.version or None)
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    if record is None:
        print(f"No release record for {args.version}" if args.version
              else "No unfinished release")
        return
    _print_release_record(record)


def cmd_clog_release_advance(args):
    """`dct clog release-advance V STATUS` — move a release record forward
    and/or record the verified release commit (K-41, K-61); mirrors the
    advance_release MCP tool (candidate_sha is immutable — not accepted;
    the manifest is written by the release workflow). Errors exit 1."""
    from dct.core.releases import advance_release, get_release

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        before = get_release(engine, project_slug, args.version)
        out = advance_release(
            engine, project_slug, args.version, args.status,
            release_commit_sha=args.release_commit_sha or None,
        )
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    record = out["release"]
    if not out["updated"]:
        print(f"✓ Release {record['version']} unchanged: {record['status']}")
        return
    previous = before["status"] if before else "?"
    print(f"✓ Release {record['version']}: {previous} → {record['status']}")


def cmd_clog_release_abandon(args):
    """`dct clog release-abandon V --reason "…" --confirm` — end an unfinished
    release as abandoned (K-60, ledger only); mirrors the abandon_release MCP
    tool. Errors exit 1."""
    from dct.core.releases import abandon_release

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        out = abandon_release(
            engine, project_slug, args.version, args.reason or "", confirm=args.confirm,
        )
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)
    record = out["release"]
    if not out["updated"]:
        print(f"✓ Release {record['version']} was already abandoned "
              f"({record['abandon_reason']})")
        return
    print(f"✓ Release {record['version']} abandoned — the record, the stamped entries "
          f"and current_version stay; release the work under a new version")


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

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    try:
        plan = build_plan(engine, project_slug, version=args.version or None,
                          bump_type=None if args.version else args.bump)
    except Exception as e:
        _proof_layer_error(e)
    if args.json:
        print(json.dumps(plan, indent=2, default=_json_default))
        return
    version = plan["version"]
    print(f"Release plan for {plan['project']} ({plan['kind']}): "
          f"{version['current'] or '-'} → {version['target'] or '(no target version given)'}")
    if plan["unfinished_release"]:
        rec = plan["unfinished_release"]
        print(f"  unfinished release {rec['version']}: {rec['status']} — resume or abandon it")
    checkout = plan["checkout"]
    if not checkout["git"]:
        print(f"  checkout: none ({checkout['reason']})")
    else:
        print(f"  candidate: {checkout['candidate_sha']} on "
              f"{checkout['branch'] or '(detached HEAD)'} in {checkout['repo_root']}")
        rng = plan["range"]
        print(f"  range: {rng['display'] or '(empty)'} — {rng['commit_count']} commits"
              f"{' [confirm the base]' if rng['confirm_required'] else ''}"
              f"{' [approximate]' if rng['approximate'] else ''}")
    proven, unproven = plan["entries"]["proven"], plan["entries"]["unproven"]
    print(f"Proven ({len(proven)}):")
    for entry in proven:
        merge = f" via {entry['merge_commit'][:12]}" if entry["merge_commit"] else ""
        pr = f" (#{entry['pr']})" if entry["pr"] else ""
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
    for warning in plan["warnings"]:
        print(f"  ⚠ {warning}", file=sys.stderr)


def cmd_clog_candidates(args):
    """`dct clog candidates` — /commit's changelog candidates (K-53), read-only.

    confirmed (client provenance of THIS checkout and branch) vs uncertain
    (server / no provenance, or any entry on a detached HEAD — never
    attached automatically), minus the ids the range already proves, plus
    the staged file list. --json prints the stable document
    (dct.workflows.release.CANDIDATE_KEYS).
    """
    import json

    from dct.workflows.release import commit_candidates

    engine = _get_engine()
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
    update.<field>.
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
            print(f"    Run `dct integrate repair {result['client']}` (legacy Claude: `dct init`).", file=sys.stderr)

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
        else:
            print(f"{result['client']}: {'ready' if result.get('ok') else 'attention required'}")
    for row in result.get("checks", []):
        print(f"  {row['status']}: {row['check']} — {row['message']}")
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
        help="Append the dct snippet to ~/.claude/CLAUDE.md without prompting",
    )
    p_init.add_argument(
        "--no-attach-snippet", dest="attach_snippet", action="store_false",
        help="Skip the ~/.claude/CLAUDE.md snippet attach",
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
        "changelog", help="Opt a project in/out of dct changelog-hook enforcement (#222)",
    )
    p_proj_clog.add_argument("slug")
    p_proj_clog.add_argument("state", choices=["on", "off"])

    p_proj_mc = p_proj_sub.add_parser(
        "manages-changelog",
        help="Exit 0 if the project opts into dct changelog enforcement, else 1 (hook query)",
    )
    p_proj_mc.add_argument("slug")

    p_proj_clogpath = p_proj_sub.add_parser(
        "changelog-path",
        help="Get/set a project's changelog file location relative to its root (#225)",
    )
    p_proj_clogpath.add_argument("slug")
    p_proj_clogpath.add_argument(
        "relpath", nargs="?",
        help="Relative changelog path (e.g. pkg/changelog.md). Omit to query the current value.",
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
                               help="Call claude --print for any uncertain sections")

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
        help="Release: bump version and stamp changelog (run from a checkout of the project)")
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
    p_clog_release.epilog = (
        "Writes the durable release record (status 'stamped', pinned HEAD and "
        "checkout root) with the stamp. The changelog file, commit and tag steps "
        "are NOT done here and do not advance the record: /release does them "
        "(or `dct clog release-advance`).")

    p_clog_rstatus = p_clog_sub.add_parser(
        "release-status",
        help="Show a release record (default: the latest unfinished release)")
    p_clog_rstatus.add_argument("--version", help="The release to show (X.Y or X.Y.Z)")
    p_clog_rstatus.add_argument("--project", "-P")

    p_clog_radv = p_clog_sub.add_parser(
        "release-advance",
        help="Move a release record forward after a verified step "
             "(stamped → files_written → committed → tagged → completed)")
    p_clog_radv.add_argument("version", help="The release (X.Y or X.Y.Z)")
    p_clog_radv.add_argument("status", choices=RELEASE_FLOW)
    p_clog_radv.add_argument("--project", "-P")
    p_clog_radv.add_argument(
        "--release-commit-sha",
        help="The verified release commit — recorded once, from 'committed' on "
             "(the candidate commit is immutable)")

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

    p_clog_cand = p_clog_sub.add_parser(
        "candidates",
        help="Changelog candidates for the commit being prepared (read-only, K-53)")
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

    # release (the release workflow, K-72) — layer 1: the read-only plan
    p_release = sub.add_parser("release", help="Release workflow (plan: read-only proof layer)")
    p_release_sub = p_release.add_subparsers(dest="release_command")
    p_rel_plan = p_release_sub.add_parser(
        "plan", help="Show the release plan of the current checkout — no side effects")
    p_rel_plan.add_argument("--project", "-P")
    p_rel_plan_v = p_rel_plan.add_mutually_exclusive_group()
    p_rel_plan_v.add_argument("--version", help="Target version (X.Y or X.Y.Z)")
    p_rel_plan_v.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_rel_plan_v.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_rel_plan_v.add_argument("--major", dest="bump", action="store_const", const="major")
    p_rel_plan.add_argument("--json", action="store_true", help="Stable JSON document")
    p_rel_status = p_release_sub.add_parser(
        "status", help="Show a release record (alias of `clog release-status`)")
    p_rel_status.add_argument("--version", help="The release to show (X.Y or X.Y.Z)")
    p_rel_status.add_argument("--project", "-P")

    # _task-mirror (internal: worker for hooks/dct-task-mirror.sh)
    p_tm = sub.add_parser(
        "_task-mirror",
        help="Internal — worker for dct-task-mirror.sh hook. Reads hook JSON on stdin.",
    )
    p_tm.add_argument("--client", choices=("claude", "codex"), default="claude")
    p_tm.add_argument("--turn-id", default="")
    p_tm.add_argument("--session-id", default="")
    p_tm.add_argument("--project", default="")

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
    p_ho_create.add_argument("--context-summary", default=None, help="Context summary snapshot")
    p_ho_create.add_argument("--project", help="Project slug (default: auto-detect from CWD)")
    p_ho_create.add_argument("--json", action="store_true", help="Print the raw result as JSON")

    p_ho_update = p_ho_sub.add_parser("update", help="Amend an unconsumed handoff in place")
    p_ho_update.add_argument("id", type=int, help="Handoff id")
    p_ho_update.add_argument("--prompt", default=None, help="New prompt text")
    p_ho_update.add_argument("--prompt-file", default=None, help="Read new prompt from file ('-' = stdin)")
    p_ho_update.add_argument("--scope", default=None, help="New scope tag")
    p_ho_update.add_argument("--context-summary", default=None, help="New context summary snapshot")

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
}

PLAN_COMMAND_MAP = {
    "rescan": cmd_plan_rescan,
}

CLOG_COMMAND_MAP = {
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
    elif args.command == "plan":
        handler = PLAN_COMMAND_MAP.get(args.plan_command)
        if not handler:
            parser.parse_args(["plan", "--help"])
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
