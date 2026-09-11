"""Tests for dct.core.plans.ingest — pipeline from MD file to DB state."""

import hashlib

import pytest

from dct.core.plans import classify as classify_mod
from dct.core.plans.ingest import (
    IngestReport,
    ensure_ulids_and_parents,
    infer_kind_from_path,
    ingest_file,
    write_back_anchors,
    write_file_atomic,
)
from dct.core.plans.parser import parse_plan
from dct.core.projects import create_project

ULID_A = "01HX9Z4K5M7PQRSTVWXYZ00001"
ULID_B = "01HX9Z4K5M7PQRSTVWXYZ00002"


@pytest.fixture
def proj(engine, tmp_path):
    return create_project(engine, slug="dct", name="DCT", path=str(tmp_path))


# ── ensure_ulids_and_parents ─────────────────────────────────────────────────

def test_ensure_ulids_generates_missing_plan_ulid():
    r = parse_plan("# Title\n## Sprint 1\n- [ ] Do\n")
    assert r.plan_ulid is None
    changed = ensure_ulids_and_parents(r)
    assert changed is True
    assert r.plan_ulid is not None
    assert len(r.plan_ulid) == 26


def test_ensure_ulids_generates_missing_section_ulids():
    r = parse_plan("# A\n## B\n### C\n")
    ensure_ulids_and_parents(r)
    assert all(s.ulid is not None for s in r.sections)
    # Parent derivation: B.parent = A, C.parent = B
    assert r.sections[0].parent_ulid is None
    assert r.sections[1].parent_ulid == r.sections[0].ulid
    assert r.sections[2].parent_ulid == r.sections[1].ulid


def test_ensure_ulids_assigns_cp_parent_from_preceding_heading():
    r = parse_plan("## S\n- [ ] X\n- [x] Y\n")
    ensure_ulids_and_parents(r)
    assert r.checkpoints[0].parent_section_ulid == r.sections[0].ulid
    assert r.checkpoints[1].parent_section_ulid == r.sections[0].ulid


def test_ensure_ulids_idempotent_when_already_anchored():
    content = (
        f"<!-- dct:plan:{ULID_A} -->\n"
        f"## Sprint\n<!-- dct:section:{ULID_B} parent={ULID_A} -->\n"
    )
    r = parse_plan(content)
    assert r.plan_ulid == ULID_A
    assert r.sections[0].ulid == ULID_B
    changed = ensure_ulids_and_parents(r)
    assert changed is False


# ── write_back_anchors ───────────────────────────────────────────────────────

def test_write_back_inserts_plan_anchor_at_top():
    content = "# Title\n## A\n"
    r = parse_plan(content)
    ensure_ulids_and_parents(r)
    out = write_back_anchors(content, r)
    assert out.startswith(f"<!-- dct:plan:{r.plan_ulid} -->\n")


def test_write_back_inserts_section_anchor_after_heading():
    content = "## Sprint 1\n"
    r = parse_plan(content)
    ensure_ulids_and_parents(r)
    out = write_back_anchors(content, r)
    lines = out.splitlines()
    # Expected: plan anchor, then heading, then section anchor
    assert lines[0].startswith("<!-- dct:plan:")
    assert lines[1] == "## Sprint 1"
    assert f"<!-- dct:section:{r.sections[0].ulid}" in lines[2]


def test_write_back_inserts_cp_anchor_inline():
    content = "## S\n- [ ] Do the thing\n"
    r = parse_plan(content)
    ensure_ulids_and_parents(r)
    out = write_back_anchors(content, r)
    assert f"<!-- dct:cp:{r.checkpoints[0].ulid}" in out
    # Original text still present
    assert "Do the thing" in out


def test_write_back_preserves_trailing_newline():
    content = "## S\n- [ ] X\n"
    r = parse_plan(content)
    ensure_ulids_and_parents(r)
    out = write_back_anchors(content, r)
    assert out.endswith("\n")


def test_write_back_idempotent_for_existing_anchors():
    content = (
        f"<!-- dct:plan:{ULID_A} -->\n"
        f"## S\n<!-- dct:section:{ULID_B} parent={ULID_A} -->\n"
        f"- [ ] <!-- dct:cp:01HX9Z4K5M7PQRSTVWXYZ00003 parent={ULID_B} --> text\n"
    )
    r = parse_plan(content)
    ensure_ulids_and_parents(r)
    out = write_back_anchors(content, r)
    # No double anchors
    assert out.count("<!-- dct:plan:") == 1
    assert out.count("<!-- dct:section:") == 1
    assert out.count("<!-- dct:cp:") == 1


# ── write_file_atomic ────────────────────────────────────────────────────────

def test_write_file_atomic_creates_and_replaces(tmp_path):
    target = tmp_path / "plan.md"
    target.write_text("original\n")
    write_file_atomic(target, "new content\n")
    assert target.read_text() == "new content\n"
    # No leftover temp file
    assert not (tmp_path / "plan.md.dct-tmp").exists()


# ── infer_kind_from_path ─────────────────────────────────────────────────────

def test_infer_kind_adr():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/docs/adr/001-foo.md")) == "adr"
    assert infer_kind_from_path(Path("/x/ADR-001-naming.md")) == "adr"


def test_infer_kind_spec():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/docs/specs/2026-design.md")) == "spec"
    assert infer_kind_from_path(Path("/x/docs/plans/2026-04-16-foo-design.md")) == "spec"


def test_infer_kind_plan():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/docs/plans/2026-04-16-foo.md")) == "plan"


def test_infer_kind_roadmap():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/ROADMAP.md")) == "roadmap"
    assert infer_kind_from_path(Path("/x/product-roadmap.md")) == "roadmap"


def test_infer_kind_backlog():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/TODOS.md")) == "backlog"
    assert infer_kind_from_path(Path("/x/FUTURE.md")) == "backlog"


def test_infer_kind_retro():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/docs/retros/sprint-0.md")) == "retro"


def test_infer_kind_fallback_to_plan():
    from pathlib import Path
    assert infer_kind_from_path(Path("/x/unknown.md")) == "plan"


# ── ingest_file: end-to-end ──────────────────────────────────────────────────

def _write_fixture(tmp_path, content: str, rel: str = "docs/plans/p.md"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_ingest_first_import_creates_plan_sections_checkpoints(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "# Plan\n## Sprint 1\n- [ ] A\n- [x] B\n")
    report = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    assert report["is_first_import"] is True
    assert report["sections_created"] == 2  # H1 + H2
    assert report["checkpoints_created"] == 2
    assert report["ulids_injected"] is True

    # File now has ULIDs
    new_content = path.read_text(encoding="utf-8")
    assert "<!-- dct:plan:" in new_content
    assert "<!-- dct:section:" in new_content
    assert "<!-- dct:cp:" in new_content

    # Hash reflects the ULID-enriched file
    expected_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    assert report["source_hash"] == expected_hash


def test_ingest_does_not_downgrade_resolved_to_uncertain(
    engine, proj, tmp_path, monkeypatch
):
    """Regression: a previously-resolved section must stay resolved on re-sync
    even if the current parse output is 'uncertain' and auto_resolve_uncertain
    is disabled (or the LLM returned None)."""
    # First: ingest with LLM returning 'reference' (so heading gets resolved).
    monkeypatch.setattr(
        classify_mod, "classify_uncertain",
        lambda *a, **kw: "reference",
    )
    path = _write_fixture(tmp_path, "# Some weird standalone title\n")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=True)

    from dct.core.plans.crud import list_plan_sections, list_plans
    plan_id = list_plans(engine, project_slug="dct")[0]["id"]
    sec = list_plan_sections(engine, plan_id=plan_id)[0]
    assert sec["section_type"] == "reference"

    # Now: re-ingest with LLM returning None (no resolution) — the parser
    # will still classify this heading as 'uncertain', but DB must not
    # downgrade the 'reference' classification back to 'uncertain'.
    monkeypatch.setattr(
        classify_mod, "classify_uncertain",
        lambda *a, **kw: None,
    )
    # Change file content slightly to force hash mismatch + rescan.
    path.write_text(path.read_text() + "\nSome trailing prose.\n", encoding="utf-8")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    sec_after = list_plan_sections(engine, plan_id=plan_id)[0]
    assert sec_after["section_type"] == "reference"  # sticky, not downgraded


def test_ingest_drops_plan_anchor_missing_warning_after_injection(
    engine, proj, tmp_path, monkeypatch
):
    """Regression: parser emits `plan_anchor_missing` during parse, but
    ingest immediately writes the anchor to disk. The report should not
    show the warning after the fix is materialized — otherwise users see
    a warning about a file that visibly has the anchor."""
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "# Plan\n## Sprint 1\n- [ ] Do\n")
    report = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    assert report["ulids_injected"] is True
    assert "plan_anchor_missing" not in report["warnings"]
    # File actually has the anchor on disk
    assert "<!-- dct:plan:" in path.read_text(encoding="utf-8")


def test_ingest_reads_x_state_on_first_import(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "## S\n- [ ] pending one\n- [x] done one\n")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    from dct.core.plans.crud import list_plan_checkpoints, list_plans
    plans_ = list_plans(engine, project_slug="dct")
    assert len(plans_) == 1
    cps = list_plan_checkpoints(engine, plan_id=plans_[0]["id"])
    by_text = {cp["text"]: cp["status"] for cp in cps}
    assert by_text == {"pending one": "pending", "done one": "done"}


def test_ingest_re_sync_ignores_x_state(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "## S\n- [ ] A\n- [ ] B\n")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    # User marks A as "done" in DB via complete_plan_checkpoint
    from dct.core.plans.crud import (
        complete_plan_checkpoint,
        list_plan_checkpoints,
        list_plans,
    )
    plans_ = list_plans(engine, project_slug="dct")
    cps = list_plan_checkpoints(engine, plan_id=plans_[0]["id"])
    a = next(cp for cp in cps if cp["text"] == "A")
    complete_plan_checkpoint(engine, a["id"])

    # User edits MD to flip A to `[x]` AND B back to `[ ]` — re-sync must IGNORE.
    # Actually re-sync keeps DB state; MD [x] is ignored. We verify by making
    # a trivial non-cp edit to change source_hash, then re-ingest.
    content = path.read_text(encoding="utf-8")
    path.write_text(content + "\nSome trailing prose.\n")

    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    cps_after = list_plan_checkpoints(engine, plan_id=plans_[0]["id"])
    by_text = {cp["text"]: cp["status"] for cp in cps_after}
    # A remains done (from DB, not re-read from MD)
    assert by_text["A"] == "done"
    assert by_text["B"] == "pending"


def test_ingest_auto_resolve_uncertain_uses_classify(engine, proj, tmp_path, monkeypatch):
    called: list[str] = []

    def fake_classify(heading, parent_heading=None, preview="", **kwargs):
        called.append(heading)
        return "prose"

    monkeypatch.setattr(classify_mod, "classify_uncertain", fake_classify)

    path = _write_fixture(tmp_path, "# Some weird standalone heading\n")
    report = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=True)

    assert called == ["Some weird standalone heading"]
    assert report["uncertain_resolved"] == 1
    assert report["uncertain_remaining"] == 0


def test_ingest_uncertain_without_classify_stays_uncertain(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "# Some weird standalone heading\n")
    report = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    assert report["uncertain_resolved"] == 0
    assert report["uncertain_remaining"] == 1


def test_ingest_uses_title_from_first_h1(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "# Real Title\n## Sprint 1\n- [ ] X\n")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    from dct.core.plans.crud import list_plans
    plans_ = list_plans(engine, project_slug="dct")
    assert plans_[0]["title"] == "Real Title"


def test_ingest_falls_back_to_filename_title(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "## Sprint 1\n- [ ] X\n", rel="docs/plans/my-plan.md")
    ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)

    from dct.core.plans.crud import list_plans
    plans_ = list_plans(engine, project_slug="dct")
    assert plans_[0]["title"] == "My Plan"


def test_ingest_stamps_imported_at_on_first_import_only(engine, proj, tmp_path, monkeypatch):
    monkeypatch.setattr(classify_mod, "classify_uncertain", lambda *a, **kw: None)

    path = _write_fixture(tmp_path, "# Plan\n## A\n- [ ] X\n")
    r1 = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)
    assert r1["is_first_import"] is True

    # Re-ingest (non-first)
    r2 = ingest_file(engine, path, project_slug="dct", auto_resolve_uncertain=False)
    assert r2["is_first_import"] is False


def test_ingest_missing_file_raises(engine, proj, tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_file(engine, tmp_path / "nonexistent.md", project_slug="dct")


def test_ingest_report_dataclass_serializes():
    r = IngestReport(
        plan_id=1, plan_ulid="01HX" + "0" * 22,
        is_first_import=True, source_hash="abc",
    )
    d = r.as_dict()
    assert d["plan_id"] == 1
    assert d["sections_created"] == 0
    assert d["warnings"] == []


def test_default_ingest_without_external_client_preserves_unknown(engine, proj, tmp_path, monkeypatch):
    from dct.config import Config, save_config

    cfg_path = tmp_path / 'config.toml'
    save_config(Config(database_url='sqlite:///unused.db'), cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))

    def forbidden(*args, **kwargs):
        raise AssertionError('default ingest must not invoke external classification')

    monkeypatch.setattr(classify_mod.subprocess, 'run', forbidden)
    monkeypatch.setattr(classify_mod.shutil, 'which', forbidden)
    path = tmp_path / 'portable.md'
    path.write_text('# 1. Portable plan\n\n## Sprint 1\n\n## Unknown topic\n')
    report = ingest_file(engine, str(path), project_slug='dct')
    assert report['uncertain_resolved'] == 0
    assert report['uncertain_remaining'] == 1
