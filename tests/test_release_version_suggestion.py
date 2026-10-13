"""`version.suggested` of the release plan (plan schema 5): the bump dct
proposes for the snapshot's selection, the next free patch when the target is
taken, and the rule that a 0.x project is never moved to 1.0.0 by a
suggestion.

Three layers, each against the production code:
- `plan.suggest_version(current, categories, base_tag, taken)` — the pure
  rules table (no git, no database);
- `gitinfo.version_tags` — the ONE reader of a version's local tags (moved
  from the effects layer, which delegates to it);
- `build_plan` on real throwaway repositories — what the plan document shows,
  that the suggestion lives OUTSIDE the snapshot (the plan digest is
  unchanged), and the plans that carry no suggestion.
"""

import inspect
import itertools
import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import git, make_git_repo

from dct.core import gitinfo
from dct.core.changelog import add_entry, import_released_entry, release_entries
from dct.core.projects import create_project, set_release_executor, set_version
from dct.core.releases import abandon_release
from dct.core.version import bump, compare_versions, parse_version, same_version
from dct.db import projects
from dct.workflows.release import build_plan
from dct.workflows.release import plan as plan_module

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

DOC = Path(__file__).resolve().parent.parent / "docs" / "release-workflow.md"
SUGGESTION_KEYS = {"bump", "target", "reason", "safe", "taken", "alternatives"}
CATEGORIES = ("added", "changed", "fixed", "removed")
REASONS = ("first_version", "after_tag", "current_unparseable", "version_only", "fixes_only",
           "features", "breaking", "breaking_0x", "tag_taken")


# ── helpers ──────────────────────────────────────────────────────────────────


def suggest(current, categories, base_tag=None, taken=()):
    """plan.suggest_version with `taken` given as versions (any spelling) —
    the callable the rules receive answers under the version contract."""
    taken = tuple(taken)
    return plan_module.suggest_version(
        current, set(categories), base_tag,
        lambda version: any(same_version(version, t) for t in taken))


def expected(bump_type, target, reason, safe, alternatives=(), taken=()):
    return {"bump": bump_type, "target": target, "reason": reason, "safe": safe,
            "taken": list(taken),
            "alternatives": [{"bump": b, "target": t} for b, t in alternatives]}


def commit(repo, message: str, path: str = "f.txt") -> str:
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(message.splitlines()[0] + "\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-q", "--cleanup=verbatim", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def proven(engine, slug, repo, category: str, text: str) -> int:
    """An unreleased entry of `category` proven by a new commit on HEAD."""
    entry_id = add_entry(engine, slug, category, text)["id"]
    commit(repo, f"{category}: {text}\n\nChangelog: {entry_id}\n", f"{text}.txt")
    return entry_id


def side_tag(repo, name: str, *, annotated: bool = False) -> None:
    """Tag `name` on a commit of a side branch — NOT an ancestor of HEAD, so
    the proof range and the snapshot do not see it; only `taken` does."""
    branch = git(repo, "symbolic-ref", "--short", "HEAD")
    git(repo, "checkout", "-q", "-b", f"side-{len(git(repo, 'tag').split())}")
    commit(repo, f"chore: side work for {name}\n", "side.txt")
    if annotated:
        git(repo, "tag", "-a", "-m", f"release {name}", name)
    else:
        git(repo, "tag", name)
    git(repo, "checkout", "-q", branch)


def project_repo(engine, tmp_path, slug="proj-s", *, current="1.0.0", tag="v1.0.0"):
    """A fresh repo on main, optionally tagged, registered as `slug`."""
    path = Path(make_git_repo(tmp_path / slug))
    if tag:
        git(path, "tag", tag)
    create_project(engine, slug=slug, name=slug.upper(), path=str(path),
                   current_version=current)
    return path


# ── 1. the rules table (contract section B) ──────────────────────────────────

SELECTIONS = {
    "none": (),
    "fixed": ("fixed",),
    "added": ("added",),
    "changed": ("changed",),
    "removed": ("removed",),
    "fixed+added": ("fixed", "added"),
    "removed+fixed": ("removed", "fixed"),
}
# Which row of the category rule each selection hits.
RULE_OF = {"none": "version_only", "fixed": "fixes_only", "added": "features",
           "changed": "features", "removed": "breaking", "fixed+added": "features",
           "removed+fixed": "breaking"}

# current → (current_version, base_tag, {rule → expected suggestion}); the
# alternatives are listed in ascending order (patch, minor, major).
MATRIX = {
    "no version, no tag": (None, None, dict.fromkeys(
        ("version_only", "fixes_only", "features", "breaking"),
        expected(None, "0.1.0", "first_version", True))),
    "no version, ancestor tag v1.3.0": (None, "1.3.0", {
        "version_only": expected("patch", "1.3.1", "after_tag", False,
                                 [("minor", "1.4.0"), ("major", "2.0.0")]),
        "fixes_only": expected("patch", "1.3.1", "after_tag", False,
                               [("minor", "1.4.0"), ("major", "2.0.0")]),
        "features": expected("minor", "1.4.0", "after_tag", False,
                             [("patch", "1.3.1"), ("major", "2.0.0")]),
        "breaking": expected("major", "2.0.0", "after_tag", False,
                             [("patch", "1.3.1"), ("minor", "1.4.0")]),
    }),
    "0.3.1": ("0.3.1", None, {
        "version_only": expected("patch", "0.3.2", "version_only", True, [("minor", "0.4.0")]),
        "fixes_only": expected("patch", "0.3.2", "fixes_only", True, [("minor", "0.4.0")]),
        "features": expected("minor", "0.4.0", "features", True, [("patch", "0.3.2")]),
        "breaking": expected("minor", "0.4.0", "breaking_0x", True, [("patch", "0.3.2")]),
    }),
    "1.4.2": ("1.4.2", None, {
        "version_only": expected("patch", "1.4.3", "version_only", True,
                                 [("minor", "1.5.0"), ("major", "2.0.0")]),
        "fixes_only": expected("patch", "1.4.3", "fixes_only", True,
                               [("minor", "1.5.0"), ("major", "2.0.0")]),
        "features": expected("minor", "1.5.0", "features", True,
                             [("patch", "1.4.3"), ("major", "2.0.0")]),
        "breaking": expected("major", "2.0.0", "breaking", False,
                             [("patch", "1.4.3"), ("minor", "1.5.0")]),
    }),
    "1.4 (X.Y)": ("1.4", None, {
        "version_only": expected("patch", "1.4.1", "version_only", True,
                                 [("minor", "1.5"), ("major", "2.0")]),
        "fixes_only": expected("patch", "1.4.1", "fixes_only", True,
                               [("minor", "1.5"), ("major", "2.0")]),
        "features": expected("minor", "1.5", "features", True,
                             [("patch", "1.4.1"), ("major", "2.0")]),
        "breaking": expected("major", "2.0", "breaking", False,
                             [("patch", "1.4.1"), ("minor", "1.5")]),
    }),
    "garbage": ("garbage", None, dict.fromkeys(
        ("version_only", "fixes_only", "features", "breaking"),
        expected(None, None, "current_unparseable", False))),
}


@pytest.mark.parametrize("current_case, selection", list(itertools.product(MATRIX, SELECTIONS)))
def test_suggestion_matrix(current_case, selection):
    current, base_tag, by_rule = MATRIX[current_case]
    result = suggest(current, SELECTIONS[selection], base_tag)
    assert result == by_rule[RULE_OF[selection]], (current_case, selection)


@pytest.mark.parametrize("categories, rule", [
    ({"changed", "fixed"}, "features"),
    ({"removed", "added"}, "breaking"),
    ({"removed", "changed"}, "breaking"),
    (set(CATEGORIES), "breaking"),
    ({"added", "changed"}, "features"),
])
def test_category_precedence_removed_over_features_over_fixes(categories, rule):
    assert suggest("1.4.2", categories) == MATRIX["1.4.2"][2][rule]
    assert suggest("0.3.1", categories) == MATRIX["0.3.1"][2][rule]


def test_the_result_has_exactly_the_documented_keys():
    for current in (None, "0.3.1", "1.4.2", "1.4", "garbage"):
        assert set(suggest(current, {"added"})) == SUGGESTION_KEYS


def test_a_current_version_wins_over_the_base_tag():
    """The `after_tag` row applies only without a current version: a stored
    current version is the base of the bump, whatever tag the range starts
    at."""
    assert suggest("1.4.2", {"fixed"}, "0.9.0") == MATRIX["1.4.2"][2]["fixes_only"]
    assert suggest("garbage", {"fixed"}, "1.3.0") == MATRIX["garbage"][2]["fixes_only"]


def test_after_a_0x_tag_the_category_rule_of_0x_applies():
    """No current version, the range starts at v0.4.0: a `removed` entry is
    still a minor bump (never 1.0.0), with the reason `after_tag`."""
    assert suggest(None, {"removed"}, "0.4.0") == expected(
        "minor", "0.5.0", "after_tag", False, [("patch", "0.4.1")])


def test_a_leading_v_on_the_current_version_is_not_carried_into_the_target():
    result = suggest("v1.4.2", {"added"})
    assert result["target"] == "1.5.0"
    assert all(not a["target"].startswith(("v", "V")) for a in result["alternatives"])


@pytest.mark.parametrize("current", ["1.4.2-rc1", "1", "1.2.3.4", "one.two", " "])
def test_unsupported_current_versions_are_unparseable_never_guessed(current):
    assert suggest(current, {"fixed"}) == expected(None, None, "current_unparseable", False)


# ── 2. properties ────────────────────────────────────────────────────────────

ZERO_CURRENTS = ["0.0.0", "0.0.1", "0.3.1", "0.9.9", "0.99.99", "0.3", "0.0", "v0.5.0"]
ALL_SELECTIONS = [set(c) for n in range(len(CATEGORIES) + 1)
                  for c in itertools.combinations(CATEGORIES, n)]


def _majors(result) -> list[int]:
    versions = [result["target"]] + [a["target"] for a in result["alternatives"]]
    return [parse_version(v)[0] for v in versions if v is not None]


@pytest.mark.parametrize("current", ZERO_CURRENTS)
def test_a_0x_project_is_never_suggested_1_0_0(current):
    """For ANY 0.x current version and ANY selection — also when the target
    and the minor are taken — neither the target nor an alternative has a
    major version of 1 or more: 1.0.0 is only ever typed by the owner."""
    minor = bump(current, "minor")
    for categories in ALL_SELECTIONS:
        for taken in ((), (minor,), (minor, bump(minor, "patch"), bump(current, "patch"))):
            result = suggest(current, categories, taken=taken)
            assert result["target"] is not None, (current, categories, taken)
            assert _majors(result) and max(_majors(result)) == 0, (current, categories, result)


@pytest.mark.parametrize("base_tag", ["0.0.1", "0.4.0", "0.99"])
def test_after_a_0x_tag_nothing_reaches_1_0_0(base_tag):
    for categories in ALL_SELECTIONS:
        result = suggest(None, categories, base_tag)
        assert max(_majors(result)) == 0, (base_tag, categories, result)


@pytest.mark.parametrize("current", ["0.3.1", "1.4.2", "1.4", "2.0", "10.99.99"])
def test_every_proposed_version_is_free_distinct_and_above_the_current(current):
    taken = (bump(current, "patch"), bump(current, "major"))
    for categories in ALL_SELECTIONS:
        result = suggest(current, categories, taken=taken)
        proposed = [result["target"]] + [a["target"] for a in result["alternatives"]]
        assert not any(same_version(a, b) for a, b in itertools.combinations(proposed, 2)), result
        assert all(compare_versions(v, current) > 0 for v in proposed), result
        assert not any(same_version(v, t) for v in proposed for t in taken), result
        assert all(a["bump"] != result["bump"] for a in result["alternatives"]), result


@pytest.mark.parametrize("current", ["0.3.1", "1.4.2", "1.4"])
def test_an_untaken_target_is_exactly_the_bump_of_the_current(current):
    for categories in ALL_SELECTIONS:
        result = suggest(current, categories)
        assert result["target"] == bump(current, result["bump"]), (categories, result)
        assert result["taken"] == []
        for alternative in result["alternatives"]:
            assert alternative["target"] == bump(current, alternative["bump"])


# ── 3. taken targets ─────────────────────────────────────────────────────────


def test_a_taken_tag_and_a_taken_record_move_to_the_next_free_patch():
    """Contract example: local v0.2.0 and an abandoned record 0.2.1 → 0.2.2.
    The bump the category rule chose is kept; the reason and safety change."""
    assert suggest("0.1.5", {"added"}, taken=("v0.2.0", "0.2.1")) == expected(
        "minor", "0.2.2", "tag_taken", False, [("patch", "0.1.6")], taken=["0.2.0", "0.2.1"])


@pytest.mark.parametrize("current, categories, taken, target, skipped", [
    ("0.3.1", {"fixed"}, ("0.3.2",), "0.3.3", ["0.3.2"]),        # a safe rule becomes unsafe
    ("1.4.2", {"removed"}, ("2.0.0",), "2.0.1", ["2.0.0"]),      # next patch, not 3.0.0
    ("1.4", {"added"}, ("1.5",), "1.5.1", ["1.5"]),              # X.Y: the patch above 1.5
    ("1.4", {"added"}, ("1.5.0",), "1.5.1", ["1.5"]),            # 1.5 == 1.5.0 is taken
])
def test_the_taken_target_moves_by_patches(current, categories, taken, target, skipped):
    result = suggest(current, categories, taken=taken)
    assert result["target"] == target and result["reason"] == "tag_taken"
    assert result["safe"] is False
    assert [parse_version(v) for v in result["taken"]] == [parse_version(v) for v in skipped]


def test_a_taken_first_version_moves_and_is_no_longer_first_version():
    """0.1.0 exists already (a stray tag, an abandoned record): the owner
    must see the version question again — `tag_taken`, not `first_version`."""
    assert suggest(None, {"added"}, taken=("0.1.0",)) == expected(
        None, "0.1.1", "tag_taken", False, taken=["0.1.0"])


def test_taken_alternatives_are_left_out_never_shifted():
    assert suggest("1.4.2", {"fixed"}, taken=("1.5.0",))["alternatives"] == [
        {"bump": "major", "target": "2.0.0"}]
    assert suggest("0.3.1", {"added"}, taken=("0.3.2",)) == expected(
        "minor", "0.4.0", "features", True, [])


def test_exactly_one_hundred_taken_patches_are_still_skipped():
    """Up to 100 steps: 1.4.3 … 1.4.102 taken (100 versions) → 1.4.103."""
    taken = [f"1.4.{p}" for p in range(3, 103)]
    result = suggest("1.4.2", {"fixed"}, taken=taken)
    assert result["target"] == "1.4.103" and result["reason"] == "tag_taken"
    assert result["taken"] == taken and result["safe"] is False


def test_more_than_one_hundred_taken_patches_leave_the_target_unresolved():
    """1.4.3 … 1.4.103 taken (101 versions): no suggestion past 100 steps —
    target null (the preview then refuses with `version_unresolved`); the
    plan never raises for it."""
    result = suggest("1.4.2", {"fixed"}, taken=[f"1.4.{p}" for p in range(3, 104)])
    assert result["target"] is None and result["safe"] is False


def test_an_unparseable_current_never_asks_about_taken_versions():
    asked = []
    result = plan_module.suggest_version("garbage", {"fixed"}, None,
                                         lambda v: asked.append(v) or True)
    assert result == expected(None, None, "current_unparseable", False) and asked == []


# ── gitinfo.version_tags (moved from the effects layer) ──────────────────────


@pytest.mark.slow
@needs_git
def test_version_tags_finds_every_spelling_of_one_version(tmp_path):
    repo = make_git_repo(tmp_path / "tags")
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "0.2.0")
    git(repo, "tag", "v0.2")
    commit(repo, "chore: second\n")
    second = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "-m", "annotated", "v0.2.0")
    for other in ("v0.2.1", "v0.20", "release-0.2.0", "v0.2.0-rc1"):
        git(repo, "tag", other)
    tag_object = git(repo, "rev-parse", "refs/tags/v0.2.0")
    assert tag_object != second
    assert gitinfo.version_tags(repo, "0.2.0") == [
        {"name": "0.2.0", "commit": first, "object": first, "annotated": False},
        {"name": "v0.2", "commit": first, "object": first, "annotated": False},
        {"name": "v0.2.0", "commit": second, "object": tag_object, "annotated": True},
    ]
    assert gitinfo.version_tags(repo, "v0.2") == gitinfo.version_tags(repo, "0.2.0")
    assert gitinfo.version_tags(repo, "0.3.0") == []


def test_effects_has_no_second_tag_reader():
    """The contract moves the reader to gitinfo; the effects layer delegates
    (one implementation for plan and effects, never two that can drift)."""
    from dct.workflows.release import effects

    assert callable(getattr(gitinfo, "version_tags", None))
    own = getattr(effects, "_version_tags", None)
    if own is not None and own is not gitinfo.version_tags:
        body = inspect.getsource(own)
        assert "gitinfo.version_tags(" in body and "for-each-ref" not in body, body


# ── 4. the plan document ─────────────────────────────────────────────────────


@pytest.mark.slow
@needs_git
def test_the_plan_suggests_for_the_snapshot_selection(engine, tmp_path):
    """The categories are those of the SELECTED entries: an unproven
    `removed` entry outside the selection never makes the release major."""
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "added", "feature")
    fix = proven(engine, "proj-s", repo, "fixed", "bugfix")
    add_entry(engine, "proj-s", "removed", "not committed yet")

    def suggestion(**kw):
        return build_plan(engine, "proj-s", cwd=str(repo), **kw)["version"]["suggested"]

    assert suggestion() == expected("minor", "1.1.0", "features", True,
                                    [("patch", "1.0.1"), ("major", "2.0.0")])
    assert suggestion(entry_ids=[fix]) == expected("patch", "1.0.1", "fixes_only", True,
                                                   [("minor", "1.1.0"), ("major", "2.0.0")])
    assert suggestion(no_entries=True) == expected("patch", "1.0.1", "version_only", True,
                                                   [("minor", "1.1.0"), ("major", "2.0.0")])


@pytest.mark.slow
@needs_git
def test_the_version_object_keeps_current_target_and_bump(engine, tmp_path):
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "fixed", "bugfix")
    version = build_plan(engine, "proj-s", cwd=str(repo), bump_type="minor")["version"]
    assert set(version) == {"current", "target", "bump", "suggested"}
    assert (version["current"], version["target"], version["bump"]) == ("1.0.0", "1.1.0", "minor")
    assert version["suggested"]["target"] == "1.0.1"  # the selection's, not the requested bump


@pytest.mark.slow
@needs_git
def test_the_suggestion_is_outside_the_snapshot_and_the_digest(engine, tmp_path):
    """A tag of the suggested version on a side branch changes ONLY the
    suggestion: the snapshot and its digest D are byte-identical, so an
    approved D stays valid and apply re-derives the same D."""
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "added", "feature")
    before = build_plan(engine, "proj-s", cwd=str(repo))
    side_tag(repo, "v1.1.0")
    after = build_plan(engine, "proj-s", cwd=str(repo))

    assert before["version"]["suggested"]["target"] == "1.1.0"
    assert after["version"]["suggested"]["target"] == "1.1.1"
    assert after["snapshot"] == before["snapshot"]
    assert after["snapshot_digest"] == before["snapshot_digest"]
    assert plan_module.snapshot_digest(after["snapshot"]) == after["snapshot_digest"]
    assert set(after["snapshot"]) == {
        "project", "version", "current_version", "git", "repo_root", "checkout_root",
        "project_root", "candidate_sha", "range", "selected_ids", "proof", "entries"}
    assert "suggested" not in json.dumps(after["snapshot"])


@pytest.mark.slow
@needs_git
@pytest.mark.parametrize("name, annotated", [
    ("v0.2.0", False), ("0.2.0", False), ("v0.2", False), ("V0.2.0", False), ("v0.2.0", True),
])
def test_a_local_tag_in_any_spelling_takes_the_version(engine, tmp_path, name, annotated):
    repo = project_repo(engine, tmp_path, current="0.1.5", tag="v0.1.5")
    proven(engine, "proj-s", repo, "added", "feature")
    side_tag(repo, name, annotated=annotated)
    suggested = build_plan(engine, "proj-s", cwd=str(repo))["version"]["suggested"]
    assert suggested["target"] == "0.2.1" and suggested["reason"] == "tag_taken"
    assert suggested["safe"] is False and suggested["taken"] == ["0.2.0"]


@pytest.mark.slow
@needs_git
def test_a_local_tag_and_an_abandoned_record_are_both_skipped(engine, tmp_path):
    """Contract example end to end: v0.2.0 tagged, a record of 0.2.1
    abandoned (the version stays taken) → 0.2.2."""
    repo = project_repo(engine, tmp_path, current="0.1.5", tag="v0.1.5")
    proven(engine, "proj-s", repo, "added", "feature")
    side_tag(repo, "v0.2.0")
    release_entries(engine, "proj-s", "0.2.1", entry_ids=[])
    abandon_release(engine, "proj-s", "0.2.1", "test: version stays taken", confirm=True)
    set_version(engine, "proj-s", "0.1.5")
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["kind"] == "release"
    assert plan["version"]["suggested"] == expected(
        "minor", "0.2.2", "tag_taken", False, [("patch", "0.1.6")], taken=["0.2.0", "0.2.1"])


@pytest.mark.slow
@needs_git
def test_a_record_of_the_target_in_another_spelling_takes_it(engine, tmp_path):
    """A record stored as `0.2` is the same version as the target 0.2.0."""
    repo = project_repo(engine, tmp_path, current="0.1.5", tag="v0.1.5")
    proven(engine, "proj-s", repo, "added", "feature")
    release_entries(engine, "proj-s", "0.2", entry_ids=[])
    abandon_release(engine, "proj-s", "0.2", "test: version stays taken", confirm=True)
    set_version(engine, "proj-s", "0.1.5")
    suggested = build_plan(engine, "proj-s", cwd=str(repo))["version"]["suggested"]
    assert suggested["target"] == "0.2.1" and suggested["reason"] == "tag_taken"


@pytest.mark.slow
@needs_git
def test_a_first_release_without_version_or_tag_is_0_1_0(engine, tmp_path):
    repo = project_repo(engine, tmp_path, current=None, tag=None)
    proven(engine, "proj-s", repo, "added", "feature")
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["version"]["suggested"] == expected(None, "0.1.0", "first_version", True)


@pytest.mark.slow
@needs_git
def test_without_a_version_the_ancestor_tag_is_the_base(engine, tmp_path):
    repo = project_repo(engine, tmp_path, current=None, tag="v1.3.0")
    proven(engine, "proj-s", repo, "fixed", "bugfix")
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["range"]["base_tag"] == "v1.3.0"
    assert plan["version"]["suggested"] == expected(
        "patch", "1.3.1", "after_tag", False, [("minor", "1.4.0"), ("major", "2.0.0")])


@pytest.mark.slow
@needs_git
def test_an_unparseable_stored_version_gives_no_target_and_no_crash(engine, tmp_path):
    """A legacy current_version outside the contract (written before it
    existed): the plan is still built; the suggestion names the reason."""
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "fixed", "bugfix")
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == "proj-s")
                     .values(current_version="release-7"))
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["version"]["suggested"] == expected(None, None, "current_unparseable", False)
    assert plan["snapshot_digest"] is not None


@pytest.mark.slow
def test_a_project_confirmed_outside_git_still_gets_a_suggestion(engine, tmp_path):
    """Outside git the selection is every entry (no_git) and only release
    records can take a version — the suggestion is computed, not null."""
    plain = tmp_path / "plain"
    plain.mkdir()
    create_project(engine, slug="proj-n", name="N", path=str(plain), current_version="1.0")
    add_entry(engine, "proj-n", "added", "x")
    plan = build_plan(engine, "proj-n", cwd=str(plain))
    assert plan["checkout"]["git"] is False
    assert plan["version"]["suggested"] == expected(
        "minor", "1.1", "features", True, [("patch", "1.0.1"), ("major", "2.0")])


# ── the plans without a suggestion ───────────────────────────────────────────


@pytest.mark.slow
@needs_git
def test_a_resume_plan_has_no_suggestion(engine, tmp_path):
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "added", "feature")
    release_entries(engine, "proj-s", "1.1.0", entry_ids=[],
                    candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["kind"] == "resume"
    assert "suggested" in plan["version"] and plan["version"]["suggested"] is None


def test_a_project_without_a_path_has_no_suggestion(engine, tmp_path):
    create_project(engine, slug="proj-np", name="NP", current_version="1.0.0")
    add_entry(engine, "proj-np", "added", "x")
    plan = build_plan(engine, "proj-np", cwd=str(tmp_path))
    assert plan["checkout"]["reason"] == "no_path"
    assert "suggested" in plan["version"] and plan["version"]["suggested"] is None


@pytest.mark.slow
@needs_git
def test_an_own_executor_project_has_no_suggestion(engine, tmp_path):
    """Its own release tool picks the version; dct proposes none."""
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "added", "feature")
    set_release_executor(engine, "proj-s", "own")
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert plan["kind"] == "release" and plan["snapshot"] is not None
    assert "suggested" in plan["version"] and plan["version"]["suggested"] is None


# ── pending_consent (step 1: present, always null) ───────────────────────────


@pytest.mark.slow
@needs_git
def test_pending_consent_is_present_and_null(engine, tmp_path):
    repo = project_repo(engine, tmp_path)
    proven(engine, "proj-s", repo, "added", "feature")
    assert "pending_consent" in plan_module.PLAN_KEYS
    plan = build_plan(engine, "proj-s", cwd=str(repo))
    assert "pending_consent" in plan and plan["pending_consent"] is None
    create_project(engine, slug="proj-np", name="NP", current_version="1.0.0")
    no_path = build_plan(engine, "proj-np", cwd=str(tmp_path))
    assert "pending_consent" in no_path and no_path["pending_consent"] is None


# ── schema 5 in the documentation ────────────────────────────────────────────


def _plan_table_row(key: str) -> str:
    for line in _plan_section().splitlines():
        if line.startswith(f"| `{key}` |"):
            return line
    raise AssertionError(f"no `{key}` row in the plan table")


def test_schema_version_is_5():
    assert plan_module.SCHEMA_VERSION == 5


def test_the_documented_version_row_names_the_suggestion():
    row = _plan_table_row("version")
    assert "{current, target, bump, suggested}" in row
    assert "snapshot" in row and "null" in row  # outside the snapshot; null cases named


def test_the_version_history_names_schema_5():
    flat = " ".join(DOC.read_text(encoding="utf-8").split())
    bullet = re.search(r"- version 5 added (.+?)(?= - version \d| A client that needs)", flat)
    assert bullet, "no `version 5 added` bullet in the schema version history"
    assert "suggested" in bullet.group(1) and "pending_consent" in bullet.group(1)
    assert re.search(r"lower than 5 no `", flat), "the capability sentence must name schema 5"


def _plan_section() -> str:
    """The `dct release plan` section of the docs (to the next `## `
    heading) — `after_tag` / `version_only` also name other things elsewhere
    in the file, so only this section counts."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## `dct release plan ")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


@pytest.mark.parametrize("reason", REASONS)
def test_every_suggestion_reason_is_documented(reason):
    assert f"`{reason}`" in _plan_section(), reason


@pytest.mark.slow
def test_version_of_imported_released_history_is_taken(engine, tmp_path):
    """A version only released changelog entries carry (imported history:
    no release record, no tag) is taken — a project without current_version
    must not get 0.1.0 as a safe `first_version` the stamp would refuse."""
    repo = make_git_repo(tmp_path / "p")
    create_project(engine, slug="proj-h", name="H", path=str(repo), current_version=None)
    import_released_entry(engine, "proj-h", "added", "old feature", "0.1.0")
    eid = add_entry(engine, "proj-h", "added", "new feature")["id"]
    (tmp_path / "p" / "n.txt").write_text("x\n")
    git(repo, "add", "n.txt")
    git(repo, "commit", "-q", "-m", f"feat: new\n\nChangelog: {eid}\n")
    suggested = build_plan(engine, "proj-h", cwd=str(repo))["version"]["suggested"]
    assert suggested["target"] == "0.1.1"
    assert suggested["reason"] == "tag_taken"
    assert suggested["safe"] is False
    assert suggested["taken"] == ["0.1.0"]
