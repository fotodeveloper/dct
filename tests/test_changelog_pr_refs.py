"""K-90 — the opt-in public PR reference `(#N)` in the changelog: the project
setting `changelog_pr_refs` (default off) is persisted on each release
(`render`), and the ONE renderer (export_changelog_md — the full export and
the single section the effects layer inserts) appends `(#N)` only from the
release's stored proof map, only when that PR is unambiguous. Never a dct id,
never a branch name, never a rewrite of an earlier release."""

from __future__ import annotations

from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project, set_changelog_pr_refs
from dct.core.releases import get_release
from dct.export import export_changelog_md

SHA = "a" * 40
MERGE = "b" * 40


def _proof(entry_id: int, pr: int | None, **extra) -> dict:
    return {str(entry_id): {"commits": [SHA], "merge_commit": MERGE if pr else None,
                            "pr": pr, **extra}}


def _project(engine, slug="proj-k", **kw):
    create_project(engine, slug=slug, name="K", current_version="1.0.0", **kw)


def _block(engine, slug, version):
    return export_changelog_md(engine, slug, version=version)


def test_default_off_renders_no_reference(engine):
    _project(engine)
    a = add_entry(engine, "proj-k", "added", "feature a", branch="feat/a",
                  branch_source="client")["id"]
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[a], proof=_proof(a, 7))
    assert get_release(engine, "proj-k", "1.1.0")["render"] == {"pr_refs": False}
    block = _block(engine, "proj-k", "1.1.0")
    assert "- feature a\n" in block + "\n" and "(#" not in block
    assert "feat/a" not in export_changelog_md(engine, "proj-k")


def test_opt_in_renders_the_proof_maps_pr_in_both_renders(engine):
    _project(engine)
    set_changelog_pr_refs(engine, "proj-k", True)
    a = add_entry(engine, "proj-k", "added", "feature a", component="api",
                  branch="feat/a", branch_source="client")["id"]
    b = add_entry(engine, "proj-k", "fixed", "bug b")["id"]
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[a, b],
                    proof={**_proof(a, 7), **_proof(b, None)})
    block = _block(engine, "proj-k", "1.1.0")
    assert "- feature a (api) (#7)" in block
    assert "- bug b\n" in block + "\n" and f"#{b}" not in block and f"#{a})" not in block
    full = export_changelog_md(engine, "proj-k")
    # ONE function: the full export holds exactly the single-section render.
    assert block in full
    assert "feat/a" not in full


def test_ambiguous_or_heuristic_numbers_are_never_rendered(engine):
    _project(engine)
    set_changelog_pr_refs(engine, "proj-k", True)
    a = add_entry(engine, "proj-k", "added", "twice merged")["id"]
    b = add_entry(engine, "proj-k", "added", "squashed (#12)")["id"]
    proof = {
        **_proof(a, None, ambiguous=True, associations=[
            {"merge_commit": MERGE, "pr": 3, "source": "introducing_merge", "certain": True},
            {"merge_commit": "c" * 40, "pr": 4, "source": "introducing_merge",
             "certain": True}]),
        **_proof(b, None, associations=[
            {"merge_commit": None, "pr": 12, "source": "subject_suffix", "certain": False}]),
    }
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[a, b], proof=proof)
    block = _block(engine, "proj-k", "1.1.0")
    assert "(#3)" not in block and "(#4)" not in block
    assert "- squashed (#12)\n" in block + "\n"  # the entry's own text only
    assert block.count("(#12)") == 1


def test_the_setting_is_persisted_per_release(engine):
    """A preference change between apply and resume (or later) never changes
    a release's render — the expected changelog hashes stay valid."""
    _project(engine)
    set_changelog_pr_refs(engine, "proj-k", True)
    a = add_entry(engine, "proj-k", "added", "a")["id"]
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[a], proof=_proof(a, 7))
    before = _block(engine, "proj-k", "1.1.0")
    set_changelog_pr_refs(engine, "proj-k", False)
    assert _block(engine, "proj-k", "1.1.0") == before and "(#7)" in before


def test_turning_it_on_never_rewrites_earlier_releases(engine):
    _project(engine)
    a = add_entry(engine, "proj-k", "added", "old")["id"]
    release_entries(engine, "proj-k", "1.1.0", entry_ids=[a], proof=_proof(a, 5))
    old = _block(engine, "proj-k", "1.1.0")
    set_changelog_pr_refs(engine, "proj-k", True)
    b = add_entry(engine, "proj-k", "added", "new")["id"]
    release_entries(engine, "proj-k", "1.2.0", entry_ids=[b], proof=_proof(b, 6))
    assert _block(engine, "proj-k", "1.1.0") == old and "(#5)" not in old
    assert "- new (#6)" in _block(engine, "proj-k", "1.2.0")
    assert "(#5)" not in export_changelog_md(engine, "proj-k")


def test_unreleased_and_unrecorded_versions_get_no_reference(engine):
    _project(engine)
    set_changelog_pr_refs(engine, "proj-k", True)
    add_entry(engine, "proj-k", "added", "pending")
    full = export_changelog_md(engine, "proj-k")
    assert "- pending\n" in full + "\n" and "(#" not in full
