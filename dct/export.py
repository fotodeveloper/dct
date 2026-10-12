"""Export changelog entries to Keep a Changelog markdown format."""

import json
from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa

from dct.core.version import version_key
from dct.db import changelog_entries, projects, releases, row_to_dict


def export_changelog_md(
    engine: sa.Engine,
    project_slug: str,
    include_unreleased: bool = True,
    version: str | None = None,
    provisional: dict | None = None,
) -> str:
    """Generate Keep a Changelog format markdown from DB entries.

    When `version` is given, render ONLY that version's `## [version] - DATE`
    block — WITHOUT the top-level `# Changelog` header and WITHOUT the
    [Unreleased] section. This single-version unit is what the append-only
    /release flow prepends into an existing changelog.md (see item #214); an
    unknown version renders to an empty string. When `version` is None
    (default) the full file is rendered: header + [Unreleased] + every version.

    Versions are selected and grouped SEMANTICALLY (K-55): every entry whose
    stored version equals the requested one under the version contract
    (1.1 == 1.1.0 == v1.1 — changelog.stored_spellings) lands in ONE block,
    in the full render too. The block is headed by the AUTHORITATIVE
    spelling (changelog.spelling_rule — the rule the reconstruction uses
    too): the live release record's version when one exists, else the
    stored spelling of the version's first-stamped entry (earliest
    released_at, NULL last, ties → lowest id; a legacy release keeps its own
    spelling — nothing is invented, nothing is rewritten).

    PR references (K-90, opt-in): an entry of a version whose release
    record was stamped with `render.pr_refs` true (the project's
    `changelog_pr_refs` at stamp time, persisted on the record) ends with
    ` (#N)` — N from THAT record's stored proof map, only when unambiguous
    (core.releases.proof_pr). Never a dct id, never a branch name; a
    version without a record (legacy) or stamped with the setting off is
    rendered as it always was, so turning the setting on never rewrites
    released history. The full render and the single-version render share
    this ONE rule (_render_version_block).

    `provisional` (#876 K-9 — the release effects' composition BEFORE the
    stamp, never a trial stamp): {version, entry_ids, render, proof} of the
    release about to be stamped — rendered as if those entries already
    carried `version` (as the newest release) and its record existed with
    that render / proof map. Read-only like every render; nothing is
    written, no other entry or record is read differently.
    """
    from dct.core.changelog import stored_spellings

    with engine.begin() as conn:
        row = conn.execute(
            sa.select(projects.c.id).where(projects.c.slug == project_slug, projects.c.deleted_at.is_(None))
        ).first()
        if not row:
            raise ValueError(f"Project '{project_slug}' not found")
        pid = row._mapping["id"]
        q = (
            sa.select(changelog_entries)
            .where(changelog_entries.c.project_id == pid, changelog_entries.c.deleted_at.is_(None))
        )
        chosen = set(provisional["entry_ids"]) if provisional else set()
        if version is not None:
            selected = changelog_entries.c.version.in_(stored_spellings(conn, pid, version))
            if chosen:
                selected = sa.or_(selected, changelog_entries.c.id.in_(sorted(chosen)))
            q = q.where(selected)
        q = q.order_by(changelog_entries.c.released_at.desc().nulls_first(), changelog_entries.c.created_at)
        rows = [row_to_dict(r) for r in conn.execute(q)]
        record_rows = conn.execute(
            sa.select(releases.c.version, releases.c.render, releases.c.proof).where(
                releases.c.project_id == pid, releases.c.deleted_at.is_(None)
            ).order_by(releases.c.id)
        ).all()
    recorded = [r.version for r in record_rows]
    refs_by_version = {_identity(r.version): _pr_refs(r.render, r.proof) for r in record_rows}
    newest = None
    if provisional:
        # The entries about to be stamped carry the version (unreleased rows
        # come first in the query's order, as the stamp's newest released_at
        # will); the record about to be written names its spelling and refs.
        newest = _identity(provisional["version"])
        for row in rows:
            if row["id"] in chosen:
                row["version"] = provisional["version"]
        recorded.append(provisional["version"])
        refs_by_version[newest] = _pr_refs(provisional.get("render"), provisional.get("proof"))

    category_order = ["added", "changed", "fixed", "removed"]

    # Group by SEMANTIC version; unreleased entries under None.
    versions: dict[object, list[dict]] = defaultdict(list)
    for row in rows:
        versions[_identity(row["version"])].append(row)

    # Single-version render: emit just the one block, no file-level header, so
    # the append-only release flow can prepend it into the existing file.
    if version is not None:
        released = [entries for key, entries in versions.items() if key is not None]
        if not released:
            return ""
        block: list[str] = []
        entries = released[0]
        _render_version_block(_spelling(entries, recorded), entries, category_order, block,
                              refs_by_version.get(_identity(entries[0]["version"])))
        return "\n".join(block)

    lines = ["# Changelog", ""]

    # Unreleased first
    if include_unreleased and None in versions:
        lines.append("## [Unreleased]")
        lines.append("")
        _render_version_entries(versions[None], category_order, lines)

    # Released versions (sorted by released_at of each version's first entry, desc)
    released_groups = [entries for key, entries in versions.items() if key is not None]
    for entries in sorted(released_groups, reverse=True,
                          key=lambda e: (_identity(e[0]["version"]) == newest,
                                         e[0].get("released_at") or "")):
        _render_version_block(_spelling(entries, recorded), entries, category_order, lines,
                              refs_by_version.get(_identity(entries[0]["version"])))

    return "\n".join(lines)


def _identity(version: str | None) -> object:
    """The grouping key of a stored version: None (unreleased), the
    contract's comparable key, or — for a legacy unparseable value — the raw
    string itself (it equals only itself, as in same_version)."""
    if version is None:
        return None
    try:
        return ("version", version_key(version))
    except ValueError:
        return ("raw", version)


def _spelling(entries: list[dict], recorded: list[str]) -> str:
    """The authoritative spelling of one version block — the ONE legacy
    spelling rule shared with the reconstruction (changelog.spelling_rule,
    K-86 / K-93 A3): the release record's version, else the stored spelling
    of the first-stamped entry (earliest released_at, NULL last, then the
    lowest id)."""
    from dct.core.changelog import spelling_rule

    return spelling_rule(recorded, entries)


def _pr_refs(render, proof) -> dict[int, int]:
    """entry id → PR number of ONE release record (K-90): empty unless the
    record's persisted `render.pr_refs` is true; only unambiguous PRs of
    its stored proof map (core.releases.proof_pr)."""
    from dct.core.releases import proof_pr

    def load(value):
        return json.loads(value) if isinstance(value, str) else value

    render, proof = load(render), load(proof)
    if not isinstance(render, dict) or render.get("pr_refs") is not True:
        return {}
    refs = {}
    for key in proof or {}:
        if key.isdigit() and (pr := proof_pr(proof, int(key))) is not None:
            refs[int(key)] = pr
    return refs


def _render_version_block(
    version: str, entries: list[dict], category_order: list[str], lines: list[str],
    refs: dict[int, int] | None = None,
) -> None:
    """Append a `## [version] - DATE` header (date from the first entry's
    released_at, or today when unreleased) followed by its category sections
    (`refs`: entry id → PR number, K-90). Shared by the full-file render and
    the single-version render so the two paths can never drift apart."""
    released_at = entries[0].get("released_at")
    if released_at:
        date_str = released_at.strftime("%Y-%m-%d") if hasattr(released_at, "strftime") else str(released_at)[:10]
    else:
        date_str = datetime.now(UTC).date().isoformat()
    lines.append(f"## [{version}] - {date_str}")
    lines.append("")
    _render_version_entries(entries, category_order, lines, refs)


def _render_version_entries(
    entries: list[dict], category_order: list[str], lines: list[str],
    refs: dict[int, int] | None = None,
) -> None:
    by_category: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_category[e["category"]].append(e)

    for cat in category_order:
        if cat not in by_category:
            continue
        lines.append(f"### {cat.capitalize()}")
        lines.append("")
        for e in by_category[cat]:
            entry_text = e["entry"]
            if e.get("component"):
                entry_text = f"{entry_text} ({e['component']})"
            if refs and e["id"] in refs:
                entry_text = f"{entry_text} (#{refs[e['id']]})"
            lines.append(f"- {entry_text}")
        lines.append("")
