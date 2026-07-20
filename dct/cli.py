"""CLI entry point for dct (Deterministic Context Tracker)."""

import argparse
import atexit
import os
import signal
import socket
import sys
from pathlib import Path

from dct import __version__, migrate, paths
from dct.config import load_config, save_config, Config
from dct.db import get_engine, create_tables
from dct.core.engine import bootstrap_engine

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
            version = project.get("current_version")
            source = project.get("_detected_source")
            if version and source:
                print(f"    ↪ Detected version {version} from {source}")
            elif not version:
                picked = _prompt_version()
                if picked:
                    set_version(engine, slug, picked)
                    print(f"    ✓ Version set to {picked}")
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

    print("\nTo register more projects:  dct project add <slug> <path>")
    print("To import existing data:    use /dct-import in a project session")
    print("\nSetup complete!")


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
        set_version(engine, args.slug, picked)
        print(f"  ✓ Version set to {picked}")
    else:
        print(
            f"  ⚠ No version detected. Run `dct project set-version {args.slug} <version>` "
            f"before using `/release`.",
            file=sys.stderr,
        )


def cmd_project_set_version(args):
    from dct.core.projects import set_version

    engine = _get_engine()
    project = set_version(engine, args.slug, args.version)
    if project:
        print(f"✓ {args.slug} → {args.version}")
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
    from dct.core.projects import get_project, resolve_changelog_path, set_changelog_path

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

    items = list_items(engine, **kwargs)
    headers = ["#", "Project", "Type", "Pri", "Status", "Title"]

    # Build project_id → slug cache
    from dct.core.projects import list_projects
    slug_map = {p["id"]: p["slug"] for p in list_projects(engine)}

    rows = []
    for i in items:
        slug = slug_map.get(i.get("project_id"), "?")
        rows.append([str(i["id"]), slug, i["item_type"], i["priority"], i["status"], i["title"][:60]])

    _print_table(headers, rows, max_widths={5: 60})


def cmd_show(args):
    from dct.core.items import get_item

    engine = _get_engine()
    item = get_item(engine, args.id)
    if not item:
        print(f"✗ Item #{args.id} not found", file=sys.stderr)
        sys.exit(1)

    print(f"#{item['id']}  {item['item_type']}/{item['priority']}  [{item['status']}]")
    print(f"Title:       {item['title']}")
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


# ── Handoff commands ──────────────────────────────────────────────────────────

def cmd_handoff_update(args):
    from dct.core.handoff import update_handoff

    engine = _get_engine()

    def _read(text, path):
        if path:
            return sys.stdin.read() if path == "-" else Path(path).read_text()
        return text

    prompt = _read(args.prompt, args.prompt_file)

    try:
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


# ── Changelog commands ────────────────────────────────────────────────────────

def cmd_clog_add(args):
    from dct.core.changelog import add_entry

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
        e = add_entry(engine, project_slug, args.category, args.entry, **kwargs)
        print(f"✓ Changelog #{e['id']} [{e['category']}] — {e['entry'][:60]}")
    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_clog_list(args):
    from dct.core.changelog import list_entries

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    entries = list_entries(engine, project_slug, unreleased_only=args.unreleased)
    headers = ["#", "Category", "Entry", "Version", "Created"]
    rows = []
    for e in entries:
        rows.append([
            str(e["id"]),
            e["category"],
            e["entry"][:50],
            e["version"] or "(unreleased)",
            _fmt_date(e["created_at"]),
        ])
    _print_table(headers, rows, max_widths={2: 50})


def cmd_clog_count(args):
    from dct.core.changelog import count_unreleased

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)
    count = count_unreleased(engine, project_slug)
    print(count)


def cmd_clog_release(args):
    import subprocess
    from dct.core.changelog import release_entries, count_unreleased
    from dct.core.projects import get_project
    from dct.core.version import bump

    engine = _get_engine()
    project_slug = args.project or _detect_project(engine)

    version = args.version
    if not version:
        bump_type = args.bump
        if not bump_type:
            print("✗ Provide a version or --patch/--minor/--major", file=sys.stderr)
            sys.exit(1)
        project = get_project(engine, project_slug)
        if not project:
            print(f"✗ Project '{project_slug}' not found", file=sys.stderr)
            sys.exit(1)
        current = project.get("current_version")
        if not current:
            print(f"✗ No current version for '{project_slug}'. Use explicit version: dct clog release {project_slug} 0.1.0", file=sys.stderr)
            sys.exit(1)
        version = bump(current, bump_type)
        print(f"  {current} → {version} ({bump_type})")

    unreleased = count_unreleased(engine, project_slug)
    if unreleased == 0:
        print(f"✗ No unreleased entries for '{project_slug}'", file=sys.stderr)
        sys.exit(1)

    count = release_entries(engine, project_slug, version)
    print(f"✓ Released {count} entries as {version}")

    project = get_project(engine, project_slug)
    if project and project.get("path"):
        import os
        for sync_path in ["scripts/dct-version-sync.sh", "scripts/version-sync.sh"]:
            full_path = os.path.join(project["path"], sync_path)
            if os.path.isfile(full_path):
                print(f"  Running {sync_path}...")
                subprocess.run(["bash", full_path, version], check=False)
                break


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
    """Internal: worker for hooks/dct-task-mirror.sh.

    Reads a Claude Code hook JSON payload on stdin, extracts TaskCreate /
    TodoWrite todos, find-or-creates a session-scoped item in dct, and
    appends each todo as a `kind='cc-task'` checkpoint. Non-blocking —
    safe to fail silently (hook is fire-and-forget).

    Owns the config gate and project autodetect (the shell hook is a lean
    dispatcher that only drains stdin and backgrounds this worker — #354):
      - reads [mirror].task_to_dct; anything but 'auto' is a silent no-op;
      - detects the project from CWD, silent no-op when the CWD is not a
        registered dct project.

    Intentionally named with an underscore prefix — not user-facing, just
    the target of the task-mirror hook. Re-expose via the `dct` entry point
    so the venv-installed Python is used (hooks cannot reliably find it).

    Args:
        session-id: Claude Code session_id. Empty → read payload .session_id.
        project: dct project slug. Empty → autodetect from CWD.
    """
    import json
    from dct.config import load_config

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return  # fire-and-forget: swallow, exit clean

    # Config gate (moved off the shell fast-path). auto → mirror; ask/off/
    # anything else → silent no-op.
    mode = getattr(load_config().mirror, "task_to_dct", "auto") or "auto"
    if mode != "auto":
        return

    tool_input = payload.get("tool_input") or {}
    todos: list[str] = []
    if isinstance(tool_input.get("todos"), list):
        todos = [
            (t.get("content") or t.get("subject") or "")
            for t in tool_input["todos"]
            if isinstance(t, dict)
        ]
    else:
        single = (
            tool_input.get("subject")
            or tool_input.get("description")
            or tool_input.get("prompt")
            or ""
        )
        if single:
            todos = [single]

    todos = [t.strip() for t in todos if t and t.strip()]
    if not todos:
        return

    from dct.core.items import list_items, create_item
    from dct.core.checkpoints import add_checkpoint
    from dct.core.projects import get_project_by_path

    engine = _get_engine()

    # Silent project autodetect — a fire-and-forget worker must never raise
    # or exit non-zero when the CWD is not a registered project.
    project_slug = args.project
    if not project_slug:
        import os

        detected = get_project_by_path(engine, os.getcwd())
        if not detected:
            return
        project_slug = detected["slug"]

    session_id = args.session_id or payload.get("session_id") or "unknown"
    title = f"CC task list — session {session_id}"

    existing = [
        row
        for row in list_items(
            engine, project_slug=project_slug, search="CC task list",
            status=None, limit=100,
        )
        if row.get("title") == title
    ]
    if existing:
        item_id = existing[0]["id"]
    else:
        item = create_item(
            engine, project_slug, "todo", title,
            tags=["cc-task-list", "auto"],
        )
        item_id = item["id"]

    for t in todos:
        add_checkpoint(engine, item_id, f"[cc] {t}", kind="cc-task")

    print(f"mirror ok: item #{item_id}, {len(todos)} checkpoint(s) added")


def cmd_config_get(args):
    """Read a dotted config key — e.g. `dct config get mirror.task_to_dct`.

    Supports: database.url, dct_root, plans.<field>, mirror.<field>.
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

    # Nested dataclass access
    section, _, field_name = key.partition(".")
    if not field_name:
        print(f"✗ Unknown config key: '{key}'", file=sys.stderr)
        sys.exit(1)

    obj = getattr(cfg, section, None)
    if obj is None or not hasattr(obj, field_name):
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
    from dct.core.checkpoints import list_checkpoints, get_progress

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


# ── MCP server (stdio) ───────────────────────────────────────────────────────

def cmd_server(args):
    from dct.core.provenance import from_environment, set_provenance

    set_provenance(from_environment("mcp"))
    try:
        from dct.mcp.server import mcp
    except ImportError as e:
        print(
            f"MCP server requires extras. Install with: pip install 'dct[mcp]'\n  (or reinstall: uv tool install --editable . --with 'mcp[cli]')\nOriginal error: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    mcp.run()


# ── Upgrade ──────────────────────────────────────────────────────────────────

def cmd_upgrade(args):
    """Upgrade the installed dct package and refresh its Claude Code symlinks.

    Detects the install method (editable / uv tool / pipx / pip), runs the
    matching upgrade, re-links skills + hooks (idempotent) and prunes dangling
    links for skills/hooks dropped from the new package, then prints the effect
    matrix so the user knows what takes effect when.
    """
    import subprocess
    from dct.update import detect_install_method, upgrade_command, dist_pkg

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
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"✗ Upgrade command failed (exit {result.returncode})", file=sys.stderr)
            sys.exit(result.returncode)

    # Refresh Claude Code integration: re-link current skills/hooks (idempotent),
    # then remove links to skills/hooks that the new package no longer ships.
    from dct.integrations.claude import install_hook, install_skills, prune_dangling_symlinks

    linked_ok = True
    removed: list[str] = []
    try:
        install_skills()
        install_hook()
        removed = prune_dangling_symlinks()
    except Exception as e:  # never let link maintenance abort the command
        linked_ok = False
        print(f"  ⚠ Skills/hooks re-link failed: {e}", file=sys.stderr)
        print("    Run `dct init` to repair the Claude Code integration.", file=sys.stderr)

    if linked_ok:
        print("  ✓ Skills and hooks re-linked")
        if removed:
            print(f"  ✓ Removed {len(removed)} dangling link(s):")
            for path in removed:
                print(f"      - {path}")

    print("\nWhat takes effect when:")
    print("  Now (this shell):            dct CLI, hooks, skill bodies")
    print("  Next session / /mcp reconnect: skill descriptions, MCP server code")
    print("  Next engine bootstrap:       database schema migrations")
    print("\nRecommend running `dct upgrade` at a session boundary — an old running")
    print("MCP server against a freshly migrated schema is an edge best avoided.")


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


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` exists (signal 0 is a no-op probe)."""
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

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dct", description="Deterministic Context Tracker — cross-project tracking")
    parser.add_argument("--version", action="version", version=f"dct {__version__}")

    sub = parser.add_subparsers(dest="command")

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

    # resolve
    p_resolve = sub.add_parser("resolve", help="Resolve/close an item")
    p_resolve.add_argument("id", type=int)
    p_resolve.add_argument("--status", "-s", default="resolved", choices=["resolved", "wont_fix"])
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

    p_clog_list = p_clog_sub.add_parser("list", help="List changelog entries")
    p_clog_list.add_argument("--project", "-P")
    p_clog_list.add_argument("--unreleased", "-u", action="store_true")

    p_clog_count = p_clog_sub.add_parser("count", help="Count unreleased entries")
    p_clog_count.add_argument("--project", "-P")

    p_clog_release = p_clog_sub.add_parser("release", help="Release: bump version and stamp changelog")
    p_clog_release.add_argument("version", nargs="?")
    p_clog_release.add_argument("--project", "-P")
    p_clog_bump = p_clog_release.add_mutually_exclusive_group()
    p_clog_bump.add_argument("--patch", dest="bump", action="store_const", const="patch")
    p_clog_bump.add_argument("--minor", dest="bump", action="store_const", const="minor")
    p_clog_bump.add_argument("--major", dest="bump", action="store_const", const="major")

    p_clog_export = p_clog_sub.add_parser("export", help="Export to Keep a Changelog markdown")
    p_clog_export.add_argument("--project", "-P")
    p_clog_export.add_argument("--output", "-o")

    # _task-mirror (internal: worker for hooks/dct-task-mirror.sh)
    p_tm = sub.add_parser(
        "_task-mirror",
        help="Internal — worker for dct-task-mirror.sh hook. Reads hook JSON on stdin.",
    )
    p_tm.add_argument("--session-id", default="")
    p_tm.add_argument("--project", default="")

    # config
    p_config = sub.add_parser("config", help="Inspect dct configuration")
    p_config_sub = p_config.add_subparsers(dest="config_command")
    p_config_get = p_config_sub.add_parser("get", help="Read a dotted config key")
    p_config_get.add_argument("key", help="e.g. mirror.task_to_dct, plans.auto_resolve_uncertain")

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

    # web (read-only browser dashboard daemon — v0.4.1)
    p_handoff = sub.add_parser("handoff", help="Manage session handoffs")
    p_ho_sub = p_handoff.add_subparsers(dest="handoff_command")

    p_ho_update = p_ho_sub.add_parser("update", help="Amend an unconsumed handoff in place")
    p_ho_update.add_argument("id", type=int, help="Handoff id")
    p_ho_update.add_argument("--prompt", default=None, help="New prompt text")
    p_ho_update.add_argument("--prompt-file", default=None, help="Read new prompt from file ('-' = stdin)")
    p_ho_update.add_argument("--scope", default=None, help="New scope tag")
    p_ho_update.add_argument("--context-summary", default=None, help="New context summary snapshot")

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
    "_task-mirror": cmd_internal_task_mirror,
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
    "export": cmd_clog_export,
}

CONFIG_COMMAND_MAP = {
    "get": cmd_config_get,
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
    if not cmd or cmd.startswith("_"):
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
    try:
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
    except Exception:
        pass  # a passive nicety must never break a command


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
