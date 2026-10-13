"""The one-dialog release — what the owner's consent binds up to the stamp
and after it, driven through the REAL CLI (and the core stamp where the
transaction itself is the subject) on throwaway git repositories with a
bare push remote.

What these tests pin:

- the last window before the stamp: an entry edited (or added) or the PR
  render switched right before the stamp is refused INSIDE the stamp
  transaction with `consent_mismatch` — no release, no stamped entry — and
  a work-in-progress / attestation commit made before it is reported
  (`made`, "commit made, release not started"), never hidden behind
  "nothing released"; the PR fixture really changes the render;
- the changelog settings (managed, the explicit path) are read again on
  every re-derivation and checked inside the stamp transaction: a change
  after the work-in-progress commit, after the attestation commit or right
  before the stamp needs a new consent;
- the approved changelog composition is bound to the durable record
  (`consent.changelog_sha256`) and enforced on the file write after the
  stamp AND on resume — until resume, not for milliseconds; a consent
  without it (older records) stays valid;
- the date, the PR-reference render and the section the screen shows are
  bound also when dct writes no changelog file (`changelog.managed`
  false) — the screen never shows an unbound date.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime

import pytest
from conftest import git
from test_release_apply_consent import (
    HISTORY,
    NO_MADE,
    Interrupted,
    Tomorrow,
    applied,
    apply_consent,
    at_point,  # noqa: F401 — the fixture
    cli,  # noqa: F401 — the fixture
    do_preview,
    interrupt,
    merged_pr,
    plain,  # noqa: F401 — the fixture
    record_count,
    refused_apply,
    rel,  # noqa: F401 — the fixture
    remote_refs,
    variant,
)
from test_release_consent_hardening import both  # noqa: F401 — the fixture
from test_release_effects import SLUG, proven
from test_release_preview import commit_all, head, message_file, write

from dct.core import releases as ledger
from dct.core.changelog import (
    StampRefused,
    add_entry,
    import_released_entry,
    list_entries,
    release_entries,
    update_entry,
)
from dct.core.projects import (
    create_project,
    get_project,
    set_changelog_path,
    set_changelog_pr_refs,
    set_manages_changelog,
)
from dct.core.releases import get_release
from dct.export import export_changelog_md
from dct.workflows.release.effects import ReleaseRefused, resume_release

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """dct makes real commits (W, A, the release commit) in these tests."""


def sha256(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def unstamped(engine, ids) -> bool:
    versions = {e["id"]: e["version"] for e in list_entries(engine, SLUG)}
    return all(versions[i] is None for i in ids)


def assert_nothing_released(engine, repo, bare, remote_before, ids) -> None:
    assert record_count(engine) == 0, "a release record was written"
    assert unstamped(engine, ids), "an entry was stamped"
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert git(repo, "tag", "--list", "v1.1*") == ""
    assert remote_refs(bare) == remote_before, "something was pushed"


# ═════════════════════════════════════════════════════════════════════════════
# The last window before the stamp: entries and the PR render
# ═════════════════════════════════════════════════════════════════════════════


def _clean(engine, rel, tmp_path):  # noqa: F811
    a = proven(engine, rel["repo"], "feature a")
    return a, ()


def _dirty(engine, rel, tmp_path):  # noqa: F811
    repo = rel["repo"]
    commit_all(repo, {"f.txt": "committed\n"})
    a = add_entry(engine, SLUG, "added", "work in progress")["id"]
    write(repo, "f.txt", "uncommitted\n")
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {a}\n")
    return a, ("--message-file", msg)


def _reworded(engine, a):
    update_entry(engine, SLUG, a, entry="reworded after the screen")


def _added(engine, a):
    add_entry(engine, SLUG, "fixed", "a late fix")


@pytest.mark.parametrize("edit", [_reworded, _added], ids=["reworded", "added"])
@pytest.mark.parametrize("tree", [_clean, _dirty], ids=["clean", "dirty"])
def test_an_entry_changed_right_before_the_stamp_is_a_consent_mismatch(
        engine, rel, cli, at_point, tmp_path, tree, edit):  # noqa: F811
    repo, bare = rel["repo"], rel["bare"]
    a, argv = tree(engine, rel, tmp_path)
    c = variant(do_preview(cli, repo, *argv))["consent_digest"]
    remote_before = remote_refs(bare)
    at_point["before_stamp"] = lambda: edit(engine, a)
    doc = refused_apply(cli, repo, c, key="consent_mismatch")
    mismatch = doc["details"]["consent_mismatch"]
    assert mismatch["changed"] == ["entries"], mismatch
    assert set(mismatch["diff"]) == {"entries"}
    assert set(mismatch["diff"]["entries"]) == {"approved", "now"}
    assert_nothing_released(engine, repo, bare, remote_before, [a])
    made = doc["details"]["made"]
    if argv:  # the work-in-progress commit was made before the stamp refused
        assert made["work_commit"] == head(repo)
        assert "commit made, release not started" in doc["details"]["error"]
    else:
        assert made == NO_MADE
        assert "commit made" not in doc["details"]["error"]


@pytest.mark.parametrize("tree", ["clean", "dirty"])
def test_the_pr_render_switched_right_before_the_stamp_is_a_consent_mismatch(
        engine, rel, cli, at_point, tmp_path, tree):  # noqa: F811
    repo, bare = rel["repo"], rel["bare"]
    a = merged_pr(engine, repo)
    argv: tuple = ()
    ids = [a]
    if tree == "dirty":
        commit_all(repo, {"f.txt": "committed\n"})
        more = add_entry(engine, SLUG, "added", "more work")["id"]
        write(repo, "f.txt", "more work\n")
        argv = ("--message-file",
                message_file(tmp_path, f"feat: more work\n\nChangelog: {more}\n"))
        ids.append(more)
    # The fixture really changes the render: with PR references the section
    # names the pull request, without them it does not.
    set_changelog_pr_refs(engine, SLUG, True)
    with_refs = variant(do_preview(cli, repo, *argv))
    assert "- feature a (#7)" in with_refs["display"]["section"]
    set_changelog_pr_refs(engine, SLUG, False)
    shown = variant(do_preview(cli, repo, *argv))
    assert "(#7)" not in shown["display"]["section"]
    assert shown["consent"]["changelog"]["pr_refs"] is False
    remote_before = remote_refs(bare)
    at_point["before_stamp"] = lambda: set_changelog_pr_refs(engine, SLUG, True)
    doc = refused_apply(cli, repo, shown["consent_digest"], key="consent_mismatch")
    assert doc["details"]["consent_mismatch"]["changed"] == ["changelog"]
    assert_nothing_released(engine, repo, bare, remote_before, ids)
    assert (repo / "changelog.md").read_text() == HISTORY
    if tree == "dirty":
        assert doc["details"]["made"]["work_commit"] == head(repo)
        assert "commit made, release not started" in doc["details"]["error"]
    else:
        assert doc["details"]["made"] == NO_MADE


def test_the_core_stamp_names_an_entry_change_as_a_consent_mismatch_only_for_a_preview(engine):
    """The D/P path keeps its plain refusal; the one-dialog stamp routes it."""
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    stale = {str(a): "0" * 64}
    with pytest.raises(ValueError, match="changed since the approved plan") as refusal:
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a], expected_entries=stale)
    assert not isinstance(refusal.value, StampRefused)
    assert record_count(engine) == 0


# ═════════════════════════════════════════════════════════════════════════════
# The changelog settings: fresh on every derivation, checked in the stamp
# ═════════════════════════════════════════════════════════════════════════════


def _changelog_off(engine):
    set_manages_changelog(engine, SLUG, False)


def _changelog_elsewhere(engine):
    set_changelog_path(engine, SLUG, "docs/CHANGES.md")


@pytest.mark.parametrize("change", [_changelog_off, _changelog_elsewhere],
                         ids=["turned_off", "another_path"])
@pytest.mark.parametrize("point", ["after_work_commit", "after_attest_commit", "before_stamp"])
def test_a_changelog_setting_changed_after_a_commit_needs_a_new_consent(
        engine, both, cli, at_point, point, change):  # noqa: F811
    repo, bare = both["repo"], both["bare"]
    doc = do_preview(cli, repo, "--message-file", both["msg"])
    c = variant(doc, "all")["consent_digest"]
    remote_before = remote_refs(bare)
    at_point[point] = lambda: change(engine)
    out = refused_apply(cli, repo, c, key="consent_mismatch")
    assert "changelog" in out["details"]["consent_mismatch"]["changed"], out["details"]
    made = out["details"]["made"]
    assert made["work_commit"] and made["attest_commit"] == head(repo), made
    assert "commit made, release not started" in out["details"]["error"]
    assert_nothing_released(engine, repo, bare, remote_before,
                            [both["a"], both["waiting"], both["more"]])
    assert (repo / "changelog.md").read_text() == HISTORY
    assert not (repo / "docs" / "CHANGES.md").exists()


@pytest.mark.parametrize("change", [_changelog_off, _changelog_elsewhere],
                         ids=["turned_off", "another_path"])
def test_outside_git_a_changelog_setting_changed_during_apply_needs_a_new_consent(
        engine, plain, cli, tmp_path, monkeypatch, change):  # noqa: F811
    """Outside git the refresh before each derivation re-resolves the
    changelog file too: a setting changed after apply read the project (a
    concurrent `dct project changelog …`) is a consent_mismatch — never a
    release that stamps the NEW settings and writes the OLD file."""
    from dct.workflows.release import effects

    root = plain["root"]
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    c = variant(do_preview(cli, tmp_path, "--entries", str(a)))["consent_digest"]
    real = effects._new_release_run

    def changed_after_the_read(*args, **kwargs):
        run = real(*args, **kwargs)
        change(engine)
        return run

    monkeypatch.setattr(effects, "_new_release_run", changed_after_the_read)
    out = refused_apply(cli, tmp_path, c, key="consent_mismatch")
    assert "changelog" in out["details"]["consent_mismatch"]["changed"], out["details"]
    assert out["details"]["made"] == NO_MADE
    assert record_count(engine) == 0 and unstamped(engine, [a])
    assert get_project(engine, SLUG)["current_version"] == "1.0.0"
    assert (root / "changelog.md").read_text() == HISTORY
    assert not (root / "docs" / "CHANGES.md").exists()
    assert (root / "VERSION").read_text() == "1.0.0\n"


def test_the_core_stamp_checks_the_changelog_settings_in_its_transaction(engine):
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    approved = {"managed": True, "explicit": False, "path": None}
    set_manages_changelog(engine, SLUG, False)
    with pytest.raises(StampRefused) as refused:
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                        expected_changelog_settings=approved)
    mismatch = refused.value.details["consent_mismatch"]
    assert mismatch["changed"] == ["changelog"]
    assert mismatch["diff"]["changelog"] == {
        "approved": {"settings": approved},
        "now": {"settings": {"managed": False, "explicit": False, "path": None}}}
    assert record_count(engine) == 0 and unstamped(engine, [a])
    set_manages_changelog(engine, SLUG, True)
    set_changelog_path(engine, SLUG, "docs/CHANGES.md")
    with pytest.raises(StampRefused):
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                        expected_changelog_settings=approved)
    assert record_count(engine) == 0
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], expected_changelog_settings={
        "managed": True, "explicit": True, "path": "docs/CHANGES.md"})
    assert get_project(engine, SLUG)["current_version"] == "1.1.0"


@pytest.mark.parametrize("bad", [
    {"managed": True}, {"managed": "yes", "explicit": False, "path": None},
    {"managed": True, "explicit": False, "path": None, "extra": 1}, ["managed"]])
def test_the_core_stamp_validates_the_expected_changelog_settings(engine, bad):
    """The refusal is the VALIDATOR's — not the in-transaction
    consent_mismatch (StampRefused is a ValueError too, so a bare
    raises((ValueError, TypeError)) would pass with the validator gone)."""
    create_project(engine, slug=SLUG, name="R", path="/tmp/proj-r", current_version="1.0.0")
    a = add_entry(engine, SLUG, "added", "feature a")["id"]
    with pytest.raises((ValueError, TypeError),
                       match=r"^expected_changelog_settings (must|is not)") as refused:
        release_entries(engine, SLUG, "1.1.0", entry_ids=[a], expected_changelog_settings=bad)
    assert not isinstance(refused.value, StampRefused)
    assert record_count(engine) == 0 and unstamped(engine, [a])


# ═════════════════════════════════════════════════════════════════════════════
# The approved composition is bound to the record (write and resume)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def bootstrap(engine, rel, cli):  # noqa: F811
    """The first changelog file ever: the candidate holds none, so the
    composition is the WHOLE released history exported from the database
    plus the new section."""
    repo = rel["repo"]
    git(repo, "rm", "-q", "changelog.md")
    git(repo, "commit", "-q", "-m", "chore: no changelog yet")
    a = proven(engine, repo, "feature a")
    shown = variant(do_preview(cli, repo))
    assert shown["consent"]["changelog"]["base_blob"] is None  # bootstrap
    return {**rel, "a": a, "shown": shown, "c": shown["consent_digest"]}


def _older_history(engine):
    import_released_entry(engine, SLUG, "added", "older history", "0.9.0")


def test_the_record_carries_the_approved_composition(engine, bootstrap, cli):  # noqa: F811
    applied(cli, bootstrap["repo"], bootstrap["c"])
    record = get_release(engine, SLUG, "1.1.0")
    bound = bootstrap["shown"]["consent"]["changelog"]["composed_sha256"]
    assert record["consent"]["changelog_sha256"] == bound
    assert sha256((bootstrap["repo"] / "changelog.md").read_bytes()) == bound


def test_history_changed_after_the_stamp_refuses_the_file_write(
        engine, bootstrap, cli, at_point):  # noqa: F811
    repo = bootstrap["repo"]
    at_point["after_stamp"] = lambda: _older_history(engine)
    code, doc = apply_consent(cli, repo, bootstrap["c"])
    assert code == 1 and doc["outcome"] == "refused", doc
    changed = doc["details"]["changelog_changed"]
    assert changed["approved"] == bootstrap["shown"]["consent"]["changelog"]["composed_sha256"]
    assert changed["now"] != changed["approved"] and changed["path"] == "changelog.md"
    assert not (repo / "changelog.md").exists(), "an unapproved changelog was written"
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"


def test_history_changed_before_a_resume_refuses_it_until_restored(
        engine, bootstrap, cli, at_point):  # noqa: F811
    """The window lasts until the resume — however long the interruption."""
    repo = bootstrap["repo"]
    at_point["after_stamp"] = interrupt("after_stamp")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, bootstrap["c"])
    _older_history(engine)
    with pytest.raises(ReleaseRefused) as refused:
        resume_release(engine, SLUG, cwd=str(repo))
    assert "changelog_changed" in refused.value.details
    assert "abandon" in str(refused.value)
    assert not (repo / "changelog.md").exists()
    assert get_release(engine, SLUG, "1.1.0")["status"] == "stamped"
    # A second resume refuses the same way: nothing was half-written.
    with pytest.raises(ReleaseRefused):
        resume_release(engine, SLUG, cwd=str(repo))
    assert not (repo / "changelog.md").exists()


def test_a_consent_without_the_composition_stays_valid():
    """Older records (and the plan/prepare path) carry no changelog_sha256 —
    their consent validates unchanged; a given one is kept, checked."""
    old = ledger.validate_consent({"version": "1.1.0", "entry_ids": [1]})
    assert "changelog_sha256" not in old
    kept = ledger.validate_consent({"version": "1.1.0", "entry_ids": [1],
                                    "changelog_sha256": "A" * 64})
    assert kept["changelog_sha256"] == "a" * 64
    with pytest.raises(ValueError):
        ledger.validate_consent({"version": "1.1.0", "entry_ids": [1],
                                 "changelog_sha256": "not a digest"})


# ═════════════════════════════════════════════════════════════════════════════
# Date, render and section are bound when dct writes no changelog file
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def unmanaged(engine, rel, cli):  # noqa: F811
    repo = rel["repo"]
    a = merged_pr(engine, repo)
    set_manages_changelog(engine, SLUG, False)
    shown = variant(do_preview(cli, repo))
    return {**rel, "a": a, "shown": shown, "c": shown["consent_digest"]}


def test_without_a_changelog_file_the_view_binds_date_render_and_section(engine, unmanaged):
    shown = unmanaged["shown"]
    assert shown["consent"]["changelog"] == {
        "path": None, "managed": False, "base_blob": None, "composed_sha256": None,
        "section_sha256": sha256(shown["display"]["section"]), "date": today(),
        "pr_refs": False}
    assert f"## [1.1.0] - {today()}" in shown["display"]["section"]


def test_without_a_changelog_file_a_render_change_after_the_screen_refuses(
        engine, unmanaged, cli):  # noqa: F811
    repo, bare = unmanaged["repo"], unmanaged["bare"]
    remote_before = remote_refs(bare)
    set_changelog_pr_refs(engine, SLUG, True)
    doc = refused_apply(cli, repo, unmanaged["c"], key="consent_mismatch")
    assert doc["details"]["consent_mismatch"]["changed"] == ["changelog"]
    assert doc["details"]["made"] == NO_MADE
    assert_nothing_released(engine, repo, bare, remote_before, [unmanaged["a"]])


def test_without_a_changelog_file_the_date_is_bound_at_the_stamp(
        engine, unmanaged, cli, at_point, monkeypatch):  # noqa: F811
    from dct.core import changelog as core_changelog

    repo, bare = unmanaged["repo"], unmanaged["bare"]
    remote_before = remote_refs(bare)
    at_point["before_stamp"] = lambda: monkeypatch.setattr(core_changelog, "datetime",
                                                           Tomorrow)
    doc = refused_apply(cli, repo, unmanaged["c"], key="date_changed")
    assert doc["details"]["date_changed"]["approved"] == today()
    assert_nothing_released(engine, repo, bare, remote_before, [unmanaged["a"]])


def test_without_a_changelog_file_the_release_is_what_the_screen_showed(
        engine, unmanaged, cli):  # noqa: F811
    repo = unmanaged["repo"]
    applied(cli, repo, unmanaged["c"])
    record = get_release(engine, SLUG, "1.1.0")
    assert record["render"] == {"pr_refs": False}
    assert "changelog_sha256" not in record["consent"]  # no file, no composition
    assert (repo / "changelog.md").read_text() == HISTORY  # dct wrote no file
    # The section the screen showed is the release's section in the export.
    assert unmanaged["shown"]["display"]["section"] in export_changelog_md(engine, SLUG)


@pytest.mark.parametrize("changelog", [
    None,
    {"path": "changelog.md", "managed": False, "base_blob": None, "composed_sha256": None,
     "section_sha256": "a" * 64, "date": "2026-01-01", "pr_refs": False},
    {"path": None, "managed": False, "base_blob": None, "composed_sha256": "a" * 64,
     "section_sha256": "a" * 64, "date": "2026-01-01", "pr_refs": False},
    {"path": None, "managed": True, "base_blob": None, "composed_sha256": "a" * 64,
     "section_sha256": "a" * 64, "date": "2026-01-01", "pr_refs": False},
    {"path": "changelog.md", "managed": True, "base_blob": None, "composed_sha256": None,
     "section_sha256": "a" * 64, "date": "2026-01-01", "pr_refs": False},
    {"path": None, "managed": False, "base_blob": None, "composed_sha256": None,
     "section_sha256": "a" * 64, "date": None, "pr_refs": False},
], ids=["null", "unmanaged_with_path", "unmanaged_with_composition", "managed_without_path",
        "managed_without_composition", "no_date"])
def test_the_view_validator_requires_a_bound_changelog_block(unmanaged, changelog):
    view = {**unmanaged["shown"]["consent"], "changelog": changelog}
    with pytest.raises((ValueError, TypeError)):
        ledger.validate_consent_view(view)
