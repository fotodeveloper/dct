"""Tag dictionary (`tags`) + item linkage (`item_tags`) — #611.

Tags are a GLOBAL, user-scoped dictionary: deliberately no `project_id`, so one
name means the same thing in every project (item LISTS stay project-scoped —
the accepted asymmetry, plan decision 5). Two spellings of one name never make
two rows: `canonicalize_tag_name` (whitespace collapsed, lowercased, C0
controls stripped) is what keeps `test` / `Test` / ` test ` a single row.

Canonicalize vs normalize (K-28): CREATION paths (`add_tag`,
`resolve_tag_names_conn`) call `normalize_tag_name`, which adds the API rules —
no comma, no empty, at most MAX_TAG_LENGTH. LOOKUP and ADMIN paths
(`tag_id_for_name_conn`, both ends of `merge_tags`) only canonicalize, because
names backfilled from the frozen `items.tags` JSON may legally break those
rules (K-7) and must stay filterable and mergeable.

Fragmentation guard: registering a name CONFUSABLE with an existing one is
refused with an actionable message; `force=True` is the documented escape
hatch. The guard is advisory (a UX rule for humans typing names), never an
integrity constraint — the only hard constraint is the partial UNIQUE index on
`name` where `deleted_at IS NULL`.

Naming hazard: this module imports the SQLAlchemy Table as `tags_table`, so
every list-of-names parameter is called `names` — never `tags`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import SequenceMatcher

import sqlalchemy as sa

from dct.db import _parse_legacy_tags_json, item_tags, items
from dct.db import tags as tags_table

# Length limit for NEW tag names, enforced at the API boundary only. Legacy
# names migrated from the frozen `items.tags` JSON column are exempt (K-7) —
# the DB column itself is sa.Text with no limit.
MAX_TAG_LENGTH = 50

# SequenceMatcher ratio at (or above) which two names are considered
# confusable. Calibrated against the vector table in tests/test_tags.py:
# test/tests (.889) and security/securty (.933) are caught,
# refactor/refactoring (.842) is not.
SIMILARITY_THRESHOLD = 0.85

# One SELECT per this many item ids in tags_for_items_conn — keeps the IN list
# well under every driver's bind-parameter ceiling (K-19).
_ID_CHUNK = 500

# C0 control characters are never meaningful inside a tag name (K-36). The
# classic whitespace controls are NOT stripped here — they are separators, and
# `str.split()` below folds them into a single space ("multi\tword" stays two
# words). Everything else in 0x00-0x1f plus DEL (0x7f) is deleted outright, so
# an ESC sequence can never reach the stored name.
_SEPARATOR_CONTROLS = "\t\n\v\f\r"
_CONTROL_DELETIONS = str.maketrans(
    "",
    "",
    "".join(chr(c) for c in range(0x20) if chr(c) not in _SEPARATOR_CONTROLS) + "\x7f",
)


# ── pure functions ────────────────────────────────────────────────────────────


def canonicalize_tag_name(raw: str) -> str:
    """Canonical FORM of a tag name — no validation (K-28).

    Strips C0 control characters (K-36), collapses whitespace and lowercases.
    Never raises for content: an empty result is a legal answer here. This is
    the function every LOOKUP path uses, because the dictionary may legally
    hold legacy names that violate the CREATION rules (a comma, or more than
    MAX_TAG_LENGTH characters — K-7): such a name must stay filterable,
    mergeable and listable even though it could not be registered today.

    A non-str argument is an AssertionError naming the list contract (#31): it
    means the caller sent something other than list[str].
    """
    assert isinstance(raw, str), f"tags must be list[str], got {type(raw).__name__}"
    return " ".join(raw.translate(_CONTROL_DELETIONS).split()).lower()


def normalize_tag_name(raw: str) -> str:
    """`canonicalize_tag_name` plus the rules for CREATING a name.

    Raises ValueError for an empty/whitespace-only name, a name containing a
    comma (the CSV separator at the CLI boundary — such a tag could never
    round-trip through `--tags a,b`) or one longer than MAX_TAG_LENGTH AFTER
    normalization. A non-str argument is an AssertionError naming the list
    contract (#31): it means the caller sent something other than list[str].

    Creation paths only (`add_tag`, `resolve_tag_names_conn`); reads use
    `canonicalize_tag_name`.
    """
    name = canonicalize_tag_name(raw)
    if not name:
        raise ValueError("Tag name cannot be empty")
    if "," in name:
        raise ValueError(f"Tag name cannot contain a comma: '{name}'")
    if len(name) > MAX_TAG_LENGTH:
        raise ValueError(
            f"Tag name is too long ({len(name)} > {MAX_TAG_LENGTH}): '{name}'"
        )
    return name


def _similar(candidate: str, names) -> list[str]:
    """Similarity guard over ALREADY-normalized names (no validation).

    Used directly by the backfill, whose legacy names may legally violate the
    comma/length API rules and must never be pushed back through
    normalize_tag_name. `sorted()` inside the ratio call makes the relation
    symmetric — SequenceMatcher is order-sensitive otherwise, and the guard's
    verdict must not depend on which name was registered first.
    """
    scored: list[tuple[float, str]] = []
    for other in names:
        if other == candidate:
            continue
        ratio = SequenceMatcher(None, *sorted((candidate, other)), autojunk=False).ratio()
        plural = candidate == other + "s" or other == candidate + "s"
        if plural or ratio >= SIMILARITY_THRESHOLD:
            scored.append((ratio, other))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:3]]


def similar_tag_names(name: str, names) -> list[str]:
    """Existing `names` confusable with `name` — top 3, ranked by -ratio then
    name. Empty list means the name is free to register.

    Two rules, either one is enough: the plural rule (`a == b + "s"`, which
    catches ui/uis at .800) and the SequenceMatcher ratio >= SIMILARITY_THRESHOLD.

    A read-side helper: the candidate is only CANONICALIZED, so a legacy name
    that breaks the creation rules can still be ranked (K-28).
    """
    return _similar(canonicalize_tag_name(name), names)


# ── dictionary ops ────────────────────────────────────────────────────────────


def _active_names_conn(conn: sa.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            sa.select(tags_table.c.name)
            .where(tags_table.c.deleted_at.is_(None))
            .order_by(tags_table.c.name)
        )
    ]


def _add_tag_conn(
    conn: sa.Connection,
    name: str,
    description: str | None = None,
    force: bool = False,
) -> dict:
    """Get-or-create one ALREADY-NORMALIZED name inside an open transaction.

    Callers pass a name that went through normalize_tag_name (or, for the
    legacy backfill, through the tolerant parser — K-7 names may exceed the API
    rules and must not be re-validated here).
    """
    row = conn.execute(
        sa.select(tags_table.c.id, tags_table.c.deleted_at)
        .where(tags_table.c.name == name)
    ).first()
    if row is not None and row.deleted_at is None:
        # K-33: a non-empty description upserts the prose on the ACTIVE row
        # too, exactly as on the resurrection path below — one rule, so
        # `add_tag(name, description=...)` is never a silent no-op. The row is
        # reused, so `created` stays False.
        if description:
            conn.execute(
                tags_table.update()
                .where(tags_table.c.id == row.id)
                .values(description=description)
            )
        return {"id": row.id, "name": name, "created": False}
    if row is not None:
        # Resurrection (K-35): the SAME row comes back, id included, so links
        # that still point at this id become visible again. Links that
        # `merge_tags` moved onto another tag are NOT pulled back — they belong
        # to the merge target now (the documented one-way property).
        values: dict = {"deleted_at": None}
        if description:
            values["description"] = description
        conn.execute(
            tags_table.update().where(tags_table.c.id == row.id).values(**values)
        )
        return {"id": row.id, "name": name, "created": True}

    if not force:
        similar = _similar(name, _active_names_conn(conn))
        if similar:
            existing = ", ".join(f"'{s}'" for s in similar)
            raise ValueError(
                f"Tag '{name}' is similar to existing {existing} — reuse it, or "
                f"call add_tag(name='{name}', force=true) to create anyway"
            )

    try:
        # Retry-upsert (K-10): a concurrent writer may win the race on the
        # partial UNIQUE index. SAVEPOINT so the IntegrityError does not poison
        # the surrounding transaction (PostgreSQL aborts it otherwise).
        with conn.begin_nested():
            new = conn.execute(
                tags_table.insert()
                .values(name=name, description=description or None)
                .returning(tags_table.c.id)
            ).first()
        assert new is not None, "INSERT RETURNING must return a row"
        return {"id": new.id, "name": name, "created": True}
    except sa.exc.IntegrityError:
        existing_row = conn.execute(
            sa.select(tags_table.c.id).where(
                tags_table.c.name == name, tags_table.c.deleted_at.is_(None)
            )
        ).first()
        if existing_row is None:
            raise
        return {"id": existing_row.id, "name": name, "created": False}


def add_tag(
    engine: sa.Engine,
    name: str,
    description: str | None = None,
    force: bool = False,
) -> dict:
    """Register a tag (or reuse/resurrect the row that already owns the name).

    Returns {"id", "name", "created"} — `created` is False when an active row
    was reused. `force=True` overrides the SIMILARITY guard only; an exact
    existing name is still reused, never duplicated.

    A non-empty `description` is UPSERTED onto the row in every case — new,
    reused or resurrected (K-33); `None`/empty leaves the stored prose alone.
    """
    normalized = normalize_tag_name(name)
    with engine.begin() as conn:
        return _add_tag_conn(conn, normalized, description=description, force=force)


def list_tags(engine: sa.Engine) -> list[dict]:
    """The whole dictionary, name-ASC, each row with an `item_count`.

    Counts are GLOBAL (the dictionary is user-scoped — plan decision 5) and
    exclude soft-removed links and soft-deleted items. Those exclusions live in
    the JOIN ON clauses, never in WHERE: a LEFT JOIN with a WHERE condition on
    the right-hand table degenerates into an INNER JOIN and an unused tag would
    silently vanish from the dictionary (K-14).
    """
    q = (
        sa.select(
            tags_table.c.id,
            tags_table.c.name,
            sa.func.count(sa.distinct(items.c.id)).label("item_count"),
            tags_table.c.created_at,
            tags_table.c.description,
        )
        .select_from(
            tags_table.outerjoin(
                item_tags,
                sa.and_(
                    item_tags.c.tag_id == tags_table.c.id,
                    item_tags.c.removed_at.is_(None),
                ),
            ).outerjoin(
                items,
                sa.and_(
                    items.c.id == item_tags.c.item_id,
                    items.c.deleted_at.is_(None),
                ),
            )
        )
        .where(tags_table.c.deleted_at.is_(None))
        .group_by(
            tags_table.c.id,
            tags_table.c.name,
            tags_table.c.created_at,
            tags_table.c.description,
        )
        .order_by(tags_table.c.name)
    )
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def tag_id_for_name_conn(conn: sa.Connection, name: str) -> int:
    """Resolve an EXISTING active tag name to its id.

    A LOOKUP path: the name is only CANONICALIZED (K-28), so a legacy
    dictionary entry that violates today's creation rules stays filterable.

    Closed dictionary (the `resolve_project_id` pattern): an unknown name is a
    ValueError naming the unknown value and the CLOSEST known names — never a
    silently empty result set, and never the whole dictionary (K-38): up to 3
    ranked suggestions, then "… and N more; call list_tags".
    """
    canonical = canonicalize_tag_name(name)
    row = conn.execute(
        sa.select(tags_table.c.id).where(
            tags_table.c.name == canonical, tags_table.c.deleted_at.is_(None)
        )
    ).first()
    if row is None:
        raise ValueError(_unknown_tag_message(canonical, _active_names_conn(conn)))
    return row.id


def _unknown_tag_message(canonical: str, known: list[str]) -> str:
    """"Unknown tag" error text: bounded, ranked, actionable (K-38).

    Suggestions are the `_similar` ranking when it fires, otherwise the first
    names in the (name-ASC) dictionary — at most 3 either way, followed by the
    count of everything not shown. The offending name is truncated to
    MAX_TAG_LENGTH so a pasted blob cannot flood the message.
    """
    shown = _similar(canonical, known) or known[:3]
    listed = ", ".join(f"'{s}'" for s in shown) if shown else "(none)"
    remaining = len(known) - len(shown)
    if remaining > 0:
        listed += f" … and {remaining} more; call list_tags"
    if len(canonical) > MAX_TAG_LENGTH:
        canonical = canonical[:MAX_TAG_LENGTH] + "…"
    return f"Unknown tag '{canonical}'. Known tags: {listed}"


def merge_tags(engine: sa.Engine, from_name: str, into_name: str) -> dict:
    """Move every ACTIVE link of `from_name` onto `into_name`, then retire the
    source row (soft delete). Returns {"from", "into", "items_moved"}.

    Both names must resolve to ACTIVE dictionary rows (unknown / soft-deleted →
    ValueError with the closest known names; from == into after canonicalization
    → ValueError). Ledger-preserving: historical (soft-removed) links of the
    source keep their own removed_at, and an item already carrying both tags
    ends with exactly ONE active link (full-PK probe, lesson #215).

    Both endpoints are LOOKUPS, so names are only CANONICALIZED (K-28) — this
    is the admin path that retires a legacy name no one could register today.

    One-way by design: `add_tag` can resurrect the source NAME afterwards, but
    the links that moved stay on the target.
    """
    source = canonicalize_tag_name(from_name)
    target = canonicalize_tag_name(into_name)
    if source == target:
        raise ValueError(f"Cannot merge tag '{source}' into itself")

    now = datetime.now(UTC)
    with engine.begin() as conn:
        from_id = tag_id_for_name_conn(conn, source)
        into_id = tag_id_for_name_conn(conn, target)

        moving = [
            r.item_id
            for r in conn.execute(
                sa.select(item_tags.c.item_id).where(
                    item_tags.c.tag_id == from_id, item_tags.c.removed_at.is_(None)
                )
            )
        ]
        for item_id in moving:
            _ensure_link_conn(conn, item_id, into_id, now)
            conn.execute(
                item_tags.update()
                .where(item_tags.c.item_id == item_id, item_tags.c.tag_id == from_id)
                .values(removed_at=now)
            )
        conn.execute(
            tags_table.update()
            .where(tags_table.c.id == from_id)
            .values(deleted_at=now)
        )
    return {"from": source, "into": target, "items_moved": len(moving)}


# ── item linkage ──────────────────────────────────────────────────────────────


def _link_row_conn(conn: sa.Connection, item_id: int, tag_id: int):
    """The FULL-composite-PK probe of one link row (removed_at deliberately not
    filtered — a soft-removed row still occupies the PK slot, lesson #215)."""
    return conn.execute(
        sa.select(item_tags.c.removed_at).where(
            item_tags.c.item_id == item_id, item_tags.c.tag_id == tag_id
        )
    ).first()


def _ensure_link_conn(conn: sa.Connection, item_id: int, tag_id: int, now) -> None:
    """Make (item_id, tag_id) an ACTIVE link — reactivate or insert.

    Retry-upsert (K-29, mirroring `_add_tag_conn`): a concurrent writer may
    take the PK slot between our probe and our INSERT, so the INSERT runs in a
    SAVEPOINT and an IntegrityError falls back to the reactivate branch. The
    re-probe is what makes the fallback honest — an IntegrityError that no row
    explains is re-raised instead of being reported as a link that exists.
    """
    row = _link_row_conn(conn, item_id, tag_id)
    if row is None:
        try:
            with conn.begin_nested():
                conn.execute(
                    item_tags.insert().values(
                        item_id=item_id, tag_id=tag_id, assigned_at=now
                    )
                )
            return
        except sa.exc.IntegrityError:
            row = _link_row_conn(conn, item_id, tag_id)
            if row is None:
                raise
    if row.removed_at is not None:
        conn.execute(
            item_tags.update()
            .where(item_tags.c.item_id == item_id, item_tags.c.tag_id == tag_id)
            .values(removed_at=None, assigned_at=now)
        )


def resolve_tag_names_conn(
    conn: sa.Connection, names, *, force: bool = False
) -> list[int]:
    """Normalize, dedupe and get-or-create `names`; returns tag ids.

    Processing is SEQUENTIAL in input order after a `dict.fromkeys` dedupe
    (K-12), so ["test", "tests"] deterministically registers 'test' and refuses
    'tests' — never the other way round depending on hash order.
    """
    assert isinstance(names, list), f"tags must be list[str], got {type(names).__name__}"
    normalized = [normalize_tag_name(n) for n in names]
    return [
        _add_tag_conn(conn, n, force=force)["id"] for n in dict.fromkeys(normalized)
    ]


def active_tag_names_conn(conn: sa.Connection, item_id: int) -> list[str]:
    """Active tag names of one item, name-ASC. Soft-deleted dictionary rows are
    invisible (a retired name hides its links until it is resurrected)."""
    return [
        r[0]
        for r in conn.execute(
            sa.select(tags_table.c.name)
            .select_from(
                item_tags.join(tags_table, tags_table.c.id == item_tags.c.tag_id)
            )
            .where(
                item_tags.c.item_id == item_id,
                item_tags.c.removed_at.is_(None),
                tags_table.c.deleted_at.is_(None),
            )
            .order_by(tags_table.c.name)
        )
    ]


def _link_state_conn(conn: sa.Connection, item_id: int) -> dict[int, datetime | None]:
    """{tag_id: removed_at} for EVERY link row of one item, soft-removed ones
    included — the ledger state the diff in `set_item_tags_conn` works from."""
    return {
        r.tag_id: r.removed_at
        for r in conn.execute(
            sa.select(item_tags.c.tag_id, item_tags.c.removed_at).where(
                item_tags.c.item_id == item_id
            )
        )
    }


def _materialize_legacy_links_conn(conn: sa.Connection, item_id: int) -> bool:
    """Seed one item's links from the frozen `items.tags` JSON (K-39).

    Same tolerant parser and same force=True as the bootstrap backfill —
    legacy names are HISTORY, never input to the API (K-7), so neither the
    creation rules nor the similarity guard apply. `assigned_at` copies the
    item's `updated_at`, exactly as the backfill does. Returns True when at
    least one link was written.

    Callers MUST have checked that the item owns no link rows at all: that is
    the backfill's own guard, and re-running this over a live link set would
    resurrect names the user removed. Shares the frozen column's lifetime —
    delete it together with `items.tags` at 2.0.0.
    """
    row = conn.execute(
        sa.select(items.c.tags, items.c.updated_at).where(items.c.id == item_id)
    ).first()
    if row is None:
        return False
    names, _dropped = _parse_legacy_tags_json(row.tags)
    for name in names:
        tag_id = _add_tag_conn(conn, name, force=True)["id"]
        _ensure_link_conn(conn, item_id, tag_id, row.updated_at)
    return bool(names)


def set_item_tags_conn(
    conn: sa.Connection, item_id: int, names, *, force: bool = False, now=None
) -> list[str]:
    """FULL REPLACE of one item's tags inside an open transaction.

    Diff-and-sync (the `_add_members_conn` / `_remove_members_conn` pattern):
    links that stay are untouched, links that leave are soft-removed, links
    that come back are REACTIVATED rather than re-inserted. Returns the final
    active name-ASC list, which is exactly what the item dict carries — no
    read-back needed (K-15).

    Migrate-on-write (K-39): an item that has NO link rows at all but still
    carries a non-empty legacy `items.tags` JSON is materialized FIRST, so the
    diff below can soft-remove those legacy names. Otherwise an explicit clear
    would leave zero rows behind and the next bootstrap backfill — whose guard
    is "no link rows at all" — would happily re-seed the tags the user just
    removed.
    """
    now = now or datetime.now(UTC)
    tag_ids = resolve_tag_names_conn(conn, names, force=force)
    desired = set(tag_ids)

    existing = _link_state_conn(conn, item_id)
    if not existing and _materialize_legacy_links_conn(conn, item_id):
        existing = _link_state_conn(conn, item_id)
    for tag_id in tag_ids:
        if tag_id not in existing or existing[tag_id] is not None:
            _ensure_link_conn(conn, item_id, tag_id, now)
    stale = [t for t, removed_at in existing.items()
             if t not in desired and removed_at is None]
    if stale:
        conn.execute(
            item_tags.update()
            .where(item_tags.c.item_id == item_id, item_tags.c.tag_id.in_(stale))
            .values(removed_at=now)
        )
    return active_tag_names_conn(conn, item_id)


def tags_for_items_conn(conn: sa.Connection, item_ids) -> dict[int, list[str]]:
    """{item_id: [name, ...]} for a materialized page of items, name-ASC.

    A SEPARATE query on purpose: joining item_tags into the list query would
    fan out the checkpoint aggregates (3 checkpoints × 3 tags → cp == [6, 9]).
    Cost is 1 + ceil(n / 500) queries for the whole page — never N+1.
    """
    ids = list(dict.fromkeys(int(i) for i in item_ids))
    out: dict[int, list[str]] = {i: [] for i in ids}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        rows = conn.execute(
            sa.select(item_tags.c.item_id, tags_table.c.name)
            .select_from(
                item_tags.join(tags_table, tags_table.c.id == item_tags.c.tag_id)
            )
            .where(
                item_tags.c.item_id.in_(chunk),
                item_tags.c.removed_at.is_(None),
                tags_table.c.deleted_at.is_(None),
            )
            .order_by(tags_table.c.name)
        )
        for row in rows:
            out[row.item_id].append(row.name)
    return out
