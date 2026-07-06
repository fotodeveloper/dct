"""End-to-end dogfood test: ingest dct's own spec file into an in-memory DB.

Copies the real spec into a tmp dir to avoid mutating repo state. Asserts:
- parser produces a non-trivial section count,
- ingest creates the plan row + sections + (for a spec, few to no checkpoints),
- idempotency: re-running ingest makes no further DB writes.
"""

import shutil
from pathlib import Path

from dct.core.plans import classify as classify_mod
from dct.core.plans.ingest import ingest_file
from dct.core.projects import create_project

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = REPO_ROOT / "docs" / "superpowers" / "specs" / "2026-04-17-plans-roadmaps-decisions-design.md"


def test_ingest_our_own_spec(engine, tmp_path, monkeypatch):
    """Run the v0.2 pipeline on our v0.2 design spec — one round-trip."""
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    assert SPEC_FILE.is_file(), f"Spec not found: {SPEC_FILE}"
    target_project = tmp_path / "dct-clone"
    target_project.mkdir()
    create_project(
        engine, slug="dct-clone", name="DCT (dogfood clone)",
        path=str(target_project),
    )

    # Copy spec into the cloned project layout so kind_from_path picks 'spec'.
    spec_dir = target_project / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    dest = spec_dir / SPEC_FILE.name
    shutil.copy2(SPEC_FILE, dest)

    report = ingest_file(
        engine, dest, project_slug="dct-clone",
        auto_resolve_uncertain=False,
    )

    assert report["is_first_import"] is True
    assert report["sections_created"] >= 15  # spec has 16 top-level H2 sections
    assert isinstance(report["source_hash"], str)
    assert len(report["source_hash"]) == 64  # sha256 hex

    # The file must end up with ULID anchors regardless of whether they
    # were injected in this test run (the live spec may have been dogfooded
    # into production DB earlier — ULIDs already on disk in git working tree)
    # or are being injected now (fresh clone without prior ingest).
    content = dest.read_text(encoding="utf-8")
    assert "<!-- dct:plan:" in content
    assert "<!-- dct:section:" in content

    # Second ingest: hash unchanged → no new sections/checkpoints.
    report2 = ingest_file(
        engine, dest, project_slug="dct-clone",
        auto_resolve_uncertain=False,
    )
    assert report2["is_first_import"] is False
    assert report2["sections_created"] == 0
    assert report2["sections_updated"] == 0
    assert report2["checkpoints_created"] == 0


def test_ingest_produces_queryable_plan(engine, tmp_path, monkeypatch):
    """After ingesting our spec, list_plans + get_plan return sane shapes."""
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)
    target = tmp_path / "proj2"
    target.mkdir()
    create_project(engine, slug="proj2", name="P2", path=str(target))

    spec_dir = target / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    dest = spec_dir / SPEC_FILE.name
    shutil.copy2(SPEC_FILE, dest)

    ingest_file(engine, dest, project_slug="proj2", auto_resolve_uncertain=False)

    from dct.core.plans.crud import get_plan, list_plans
    plans_ = list_plans(engine, project_slug="proj2", kind="spec")
    assert len(plans_) == 1
    full = get_plan(engine, plans_[0]["id"])
    assert full is not None
    assert full["kind"] == "spec"
    assert len(full["sections"]) >= 15
    # Spec has zero checkboxes — its progress is 0/0 → percent 0 by definition.
    assert full["progress"]["total"] == 0
    assert full["progress"]["percent"] == 0
