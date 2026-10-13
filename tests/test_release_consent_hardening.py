"""The one-dialog release, hardening after the review of phase 2 — driven
through the REAL CLI (and the effects layer where a race must be injected)
on throwaway git repositories with a bare push remote.

What these tests pin:

- ONE "can this approved release still be finished here" predicate
  (plan.consent_adoption) for the plan's `pending_consent` and for
  `apply --consent`: the ref HEAD names (a detached HEAD included) is part
  of it, it is checked again after the work-in-progress commit, after the
  attestation commit and right before the stamp — a branch switched while
  HEAD stays on the same commit is still another context; the plan says why
  a release cannot be finished and what works instead;
- the remote-tracking ref refresh is ONE ref transaction that never writes
  through a symbolic ref created between the check and the write;
- the preview pin keeps the blobs of EVERY variant reachable (also after
  apply re-pins it), `git gc` never makes an approved variant unappliable;
- the checkout snapshot around the version sync sees the bytes of tracked
  files that were already modified (status "M" before and after);
- a flag that conflicts with --consent / --discard-preview is refused when
  it is PRESENT — an empty value is not "absent";
- `--attest` without any entry released is refused; "Release with all N"
  after "None" with another version shows the right entries;
- the preview's version refusals carry machine keys;
- untracked files left out of a preview are listed (display only, outside
  the consent digest), secret-looking ones labelled, none ever added.
"""

from __future__ import annotations

import re
import shlex
import shutil

import pytest
from conftest import git
from test_release_apply_consent import (
    NO_MADE,
    Interrupted,
    applied,
    apply_consent,
    at_point,  # noqa: F401 — the fixture
    cli,  # noqa: F401 — the fixture
    do_preview,
    interrupt,
    pending_consent,
    record_count,
    refused_apply,
    rel,  # noqa: F401 — the fixture
    subject,
    variant,
)
from test_release_effects import SLUG, apply, make_release_repo, proven
from test_release_preview import commit_all, head, message_file, slot, write

from dct.core.changelog import add_entry
from dct.core.releases import get_release
from dct.workflows.release.effects import ATTEST_SUBJECT

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """dct makes real commits (W, A, the release commit) in these tests."""


def branch_of(repo) -> str | None:
    """The branch HEAD names, None when detached."""
    ref = git(repo, "rev-parse", "--symbolic-full-name", "HEAD")
    return None if ref == "HEAD" else ref.removeprefix("refs/heads/")


def ref_of(repo, ref: str) -> str | None:
    return git(repo, "for-each-ref", "--format=%(objectname)", ref) or None


@pytest.fixture
def both(engine, rel, tmp_path):  # noqa: F811 — `rel` is the imported fixture
    """A dirty tree whose "all" variant makes BOTH commits: a proven entry,
    a waiting one (→ the attestation commit A) and an uncommitted change
    with its message (→ the work-in-progress commit W)."""
    repo = rel["repo"]
    a = proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    more = add_entry(engine, SLUG, "added", "more work")["id"]
    write(repo, "f.txt", "more work\n")
    msg = message_file(tmp_path, f"feat: more work\n\nChangelog: {more}\n")
    return {**rel, "a": a, "waiting": waiting, "more": more, "msg": msg, "h0": head(repo)}


def _all_variant(cli, both) -> str:  # noqa: F811
    doc = do_preview(cli, both["repo"], "--message-file", both["msg"])
    return variant(doc, "all")["consent_digest"]


# ═════════════════════════════════════════════════════════════════════════════
# The ref HEAD names is checked after W, after A and before the stamp
# ═════════════════════════════════════════════════════════════════════════════


def _switch(repo):
    git(repo, "checkout", "-q", "-b", "elsewhere")


def _detach(repo):
    git(repo, "checkout", "-q", "--detach")


@pytest.mark.parametrize("point", ["after_work_commit", "after_attest_commit", "before_stamp"])
@pytest.mark.parametrize("how", [_switch, _detach], ids=["switched", "detached"])
def test_a_branch_switch_after_a_commit_refuses_before_the_stamp(
        engine, both, cli, at_point, point, how):  # noqa: F811
    """A hook (or another terminal) switches the branch — HEAD stays on the
    same commit — after W, after A or right before the stamp: refused with
    `branch_switched`, the commits made are named and stay, nothing is
    stamped, dct switches nothing back; switching back and running the same
    apply again finishes the release by adopting them."""
    repo, h0 = both["repo"], both["h0"]
    c = _all_variant(cli, both)
    at_point[point] = lambda: how(repo)
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["why"] == "branch_switched", doc["details"]
    made = doc["details"]["made"]
    assert made["work_commit"] is not None
    if point != "after_work_commit":
        assert made["attest_commit"] is not None
    assert "commit made, release not started" in doc["details"]["error"]
    assert get_release(engine, SLUG) is None and record_count(engine) == 0
    # dct never switched back for the owner, and nothing landed elsewhere.
    expected_branch = "elsewhere" if how is _switch else None
    assert branch_of(repo) == expected_branch
    tip = made["attest_commit"] or made["work_commit"]
    assert head(repo) == tip
    assert git(repo, "rev-list", "--count", f"{h0}..{tip}") == (
        "1" if made["attest_commit"] is None else "2")

    git(repo, "checkout", "-q", "main")
    out = applied(cli, repo, c)
    assert out["details"]["made"]["work_commit"] == made["work_commit"]
    record = get_release(engine, SLUG, "1.1.0")
    assert record["status"] == "completed"
    assert git(repo, "rev-parse", "refs/heads/main") == record["release_commit_sha"]
    if how is _switch:
        assert git(repo, "rev-parse", "refs/heads/elsewhere") == tip, \
            "the release commit landed on the switched-to branch"


# ═════════════════════════════════════════════════════════════════════════════
# Plan and apply share ONE predicate
# ═════════════════════════════════════════════════════════════════════════════


def _none(repo, h0):
    return None


def _branch(repo, h0):
    git(repo, "checkout", "-q", "-b", "tmp")
    return "branch_switched"


def _detached(repo, h0):
    git(repo, "checkout", "-q", "--detach")
    return "branch_switched"


def _moved_on(repo, h0):
    commit_all(repo, {"other.txt": "someone else\n"}, "chore: someone else")
    return "head_moved"


def _reset(repo, h0):
    git(repo, "reset", "-q", "--hard", h0)
    return "progress_lost"


@pytest.mark.parametrize("change", [_none, _branch, _detached, _moved_on, _reset],
                         ids=lambda f: f.__name__.lstrip("_"))
def test_the_plan_offers_finish_exactly_when_apply_finishes(
        engine, both, cli, at_point, change):  # noqa: F811
    """After an interrupted apply (W recorded): `pending_consent.adoptable`
    is true exactly when the same `apply --consent` finishes; when it is
    false the plan names the same reason apply refuses with (`blocked.why`
    == base_moved.why) and a next step that is not "Finish"."""
    repo, h0 = both["repo"], both["h0"]
    c = _all_variant(cli, both)
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    why = change(repo, h0)
    pending = pending_consent(engine, repo)
    if why is None:
        assert pending["adoptable"] is True and pending["consent_digest"] == c
        assert pending["blocked"] is None
        applied(cli, repo, c)
        return
    assert pending["adoptable"] is False and pending["consent_digest"] is None
    blocked = pending["blocked"]
    assert blocked is not None and blocked["why"] == why, blocked
    # A step that works for THIS reason — never a bare "finish".
    works = {"branch_switched": "git switch", "head_moved": "dct release prepare --preview",
             "progress_lost": "dct release prepare --discard-preview"}[why]
    assert works in blocked["next_step"], blocked["next_step"]
    assert not blocked["next_step"].lower().startswith("finish")
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["why"] == why, doc["details"]


@pytest.mark.parametrize("change", [_branch, _detached], ids=["branch", "detached"])
def test_after_a_branch_switch_the_plan_names_the_way_back(
        engine, both, cli, at_point, change):  # noqa: F811
    repo, h0 = both["repo"], both["h0"]
    c = _all_variant(cli, both)
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    w = head(repo)
    change(repo, h0)
    assert head(repo) == w, "precondition: the same commit, another ref"
    blocked = pending_consent(engine, repo)["blocked"]
    assert blocked["expected_ref"] == "refs/heads/main"
    assert blocked["head_ref"] == (None if change is _detached else "refs/heads/tmp")
    assert "git switch main" in blocked["next_step"], blocked["next_step"]
    git(repo, "switch", "-q", "main")
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True and pending["blocked"] is None
    applied(cli, repo, c)


def test_a_preview_made_detached_is_not_finished_on_a_branch(engine, both, cli, at_point):  # noqa: F811
    """The approved ref of a detached preview is "no branch": attaching
    HEAD to a branch at the same commit is another context."""
    repo = both["repo"]
    git(repo, "checkout", "-q", "--detach")
    c = _all_variant(cli, both)
    at_point["after_work_commit"] = interrupt("after_work_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    git(repo, "checkout", "-q", "-b", "attached")
    blocked = pending_consent(engine, repo)["blocked"]
    assert blocked["why"] == "branch_switched" and blocked["expected_ref"] is None
    assert "git switch --detach" in blocked["next_step"], blocked["next_step"]
    doc = refused_apply(cli, repo, c, key="base_moved")
    assert doc["details"]["base_moved"]["why"] == "branch_switched"


def advised_switch(text: str) -> list[str]:
    """The one `git switch …` command a next step names (in backticks)."""
    found = re.findall(r"`(git switch [^`]+)`", text)
    assert len(found) == 1, text
    return shlex.split(found[0])


@pytest.mark.parametrize("point", ["after_work_commit", "after_attest_commit"])
def test_the_way_back_to_a_detached_preview_finishes_the_same_apply(
        engine, both, cli, at_point, point):  # noqa: F811
    """K-42: the next step the plan AND apply give for a detached preview is
    a WORKING one — executed literally, it puts HEAD back on the verified
    progress commit (A, else W; never the base H0, which drops the commits
    made from HEAD's chain and refuses `progress_lost`), and the SAME
    `apply --consent` then finishes the release, adopting them."""
    repo = both["repo"]
    git(repo, "checkout", "-q", "--detach")
    c = _all_variant(cli, both)
    at_point[point] = interrupt(point)
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, c)
    tip = head(repo)
    assert tip != both["h0"], "precondition: a commit was made under the approval"
    git(repo, "checkout", "-q", "-b", "attached")
    blocked = pending_consent(engine, repo)["blocked"]
    assert blocked["why"] == "branch_switched", blocked
    from_plan = advised_switch(blocked["next_step"])
    doc = refused_apply(cli, repo, c, key="base_moved")
    from_apply = advised_switch(doc["details"]["error"])
    assert from_plan == from_apply, (from_plan, from_apply)
    git(repo, *from_plan[1:])  # the advice, executed as given
    assert head(repo) == tip and branch_of(repo) is None
    pending = pending_consent(engine, repo)
    assert pending["adoptable"] is True and pending["blocked"] is None, pending
    out = applied(cli, repo, c)
    made = out["details"]["made"]
    assert tip in (made["work_commit"], made["attest_commit"]), made
    assert get_release(engine, SLUG, "1.1.0")["status"] == "completed"


# ═════════════════════════════════════════════════════════════════════════════
# The tracking-ref refresh: one ref transaction, never through a symref
# ═════════════════════════════════════════════════════════════════════════════


def test_a_symref_created_between_the_check_and_the_write_is_never_written_through(
        engine, tmp_path, monkeypatch):
    """refs/remotes/origin/main becomes a symbolic ref onto a local branch
    AFTER the refresh checked it and before it writes: the local branch and
    the symbolic ref stay exactly as they are; the refresh is a warning,
    the release is completed."""
    from dct.core import gitinfo

    repo, _ = make_release_repo(engine, tmp_path)
    ref = "refs/remotes/origin/main"
    old = git(repo, "rev-parse", ref)
    git(repo, "branch", "work", old)
    real = gitinfo.git_write
    raced = {"done": False}

    def racing(cwd, *args, **kwargs):
        if not raced["done"] and args[:3] == ("rev-parse", "--verify", "-q") \
                and args[-1] == ref:
            raced["done"] = True
            git(repo, "symbolic-ref", ref, "refs/heads/work")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(gitinfo, "git_write", racing)
    a = proven(engine, repo, "feature a")
    out = apply(engine, repo, version="1.1.0", entry_ids=[a])
    assert raced["done"], "precondition: the race was injected"
    assert out["outcome"] == "completed", out
    assert git(repo, "rev-parse", "refs/heads/work") == old, \
        "the local branch was moved through the symbolic ref"
    assert git(repo, "symbolic-ref", ref) == "refs/heads/work", \
        "the symbolic ref was replaced"
    tracking = out["details"]["tracking_ref"]
    assert tracking["updated"] is False, tracking
    assert any(ref in w for w in out["warnings"]), out["warnings"]


# ═════════════════════════════════════════════════════════════════════════════
# The preview pin keeps every variant's blobs
# ═════════════════════════════════════════════════════════════════════════════


def _gc(repo) -> None:
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now")


def _two_versions(engine, invoke, repo) -> dict:
    proven(engine, repo, "fix a", "fixed")
    add_entry(engine, SLUG, "added", "waiting b")
    doc = do_preview(invoke, repo)
    shown, every = variant(doc), variant(doc, "all")
    versions = (shown["consent"]["version"]["target"], every["consent"]["version"]["target"])
    assert versions == ("1.0.1", "1.1.0"), versions  # precondition: two versions
    blobs = {v["name"]: v["consent"]["version_files"]["expected_blobs"]["pyproject.toml"]
             for v in (shown, every)}
    assert blobs["shown"] != blobs["all"], "precondition: different expected blobs"
    return {"doc": doc, "shown": shown, "all": every, "blobs": blobs}


def _exists(repo, blob: str) -> bool:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", blob],
                          capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("name", ["shown", "all"])
def test_gc_keeps_the_blobs_of_every_variant(engine, rel, cli, name):  # noqa: F811
    repo = rel["repo"]
    found = _two_versions(engine, cli, repo)
    _gc(repo)
    missing = [n for n, blob in found["blobs"].items() if not _exists(repo, blob)]
    assert not missing, f"git gc pruned the expected blobs of {missing}"
    out = applied(cli, repo, found[name]["consent_digest"])
    assert out["version"] == found[name]["consent"]["version"]["target"]


def test_the_re_pin_in_apply_keeps_every_variant(engine, rel, cli, at_point):  # noqa: F811
    """The pin was lost (deleted): apply pins the preview again — with the
    blobs of EVERY variant, so a later `git gc` drops none of them."""
    repo = rel["repo"]
    found = _two_versions(engine, cli, repo)
    pin = slot(engine)["pin"]
    git(repo, "update-ref", "-d", pin)
    every = found["all"]["consent_digest"]
    at_point["after_attest_commit"] = interrupt("after_attest_commit")
    with pytest.raises(Interrupted):
        apply_consent(cli, repo, every)
    assert ref_of(repo, pin) is not None, "precondition: apply pinned the preview again"
    _gc(repo)
    missing = [n for n, blob in found["blobs"].items() if not _exists(repo, blob)]
    assert not missing, f"git gc pruned the expected blobs of {missing}"
    out = applied(cli, repo, every)
    assert out["version"] == "1.1.0"


# ═════════════════════════════════════════════════════════════════════════════
# The checkout snapshot sees the bytes of already-modified tracked files
# ═════════════════════════════════════════════════════════════════════════════

_REWRITE = 'printf "changed by the sync\\n" > "{path}"\n'
_DELETE = 'mv "{path}" "{path}.gone"\n'
_RETYPE = 'mv "{path}" "{path}.gone" && ln -s elsewhere "{path}"\n'


@pytest.mark.parametrize("body", [_REWRITE, _DELETE, _RETYPE],
                         ids=["rewritten", "deleted", "retyped"])
def test_a_sync_that_changes_an_already_modified_file_is_refused_naming_it(
        engine, rel, cli, tmp_path, body):  # noqa: F811
    """b.txt is already " M" when the preview starts; the version sync
    writes it in the ORIGINAL checkout (by absolute path). The status line
    is " M" before and after the rewrite — the refusal still sees the bytes
    and names b.txt; dct restores nothing."""
    repo = rel["repo"]
    commit_all(repo, {"b.txt": "b0\n"})
    (repo / "scripts" / "dct-version-sync.sh").write_text(
        "#!/bin/bash\nset -e\n"
        'printf \'[project]\\nname = "x"\\nversion = "%s"\\n\' "$1" > pyproject.toml\n'
        + body.format(path=repo / "b.txt"))
    git(repo, "add", "scripts/dct-version-sync.sh")
    git(repo, "commit", "-q", "-m", "chore: a sync that writes the checkout")
    wip = add_entry(engine, SLUG, "added", "work in progress")["id"]
    write(repo, "b.txt", "b1\n")
    assert git(repo, "status", "--porcelain", "--", "b.txt") == "M b.txt"
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {wip}\n")
    code, doc, err = cli(repo, "prepare", "--preview", "--message-file", msg, "--no-variants")
    assert code == 1, f"the preview was not refused: {doc and doc['outcome']} {err}"
    assert "checkout_changed" in doc["details"], doc["details"]
    assert "b.txt" in doc["details"]["error"], doc["details"]["error"]
    if body is _REWRITE:
        assert git(repo, "status", "--porcelain", "--", "b.txt") == "M b.txt"
        assert (repo / "b.txt").read_text() == "changed by the sync\n"  # not restored
    assert slot(engine) is None


def test_git_state_reports_a_rewritten_modified_file_by_name(rel):  # noqa: F811
    """Producer → consumer: the snapshot's difference names the file."""
    from dct.workflows.release.effects import (
        _describe_checkout_change,
        _git_state,
        _state_diff,
    )

    repo = rel["repo"]
    commit_all(repo, {"b.txt": "b0\n"})
    write(repo, "b.txt", "b1\n")
    before = _git_state(str(repo))
    write(repo, "b.txt", "b2\n")
    changed = _state_diff(before, _git_state(str(repo)))
    assert changed, "a byte change of an already-modified file went unseen"
    assert "b.txt" in _describe_checkout_change(changed)


# ═════════════════════════════════════════════════════════════════════════════
# Presence, not truthiness, of conflicting flags
# ═════════════════════════════════════════════════════════════════════════════


def _actions(parser) -> dict:
    return {opt: action for action in parser._actions for opt in action.option_strings}


def _subparser(*path: str):
    from dct import cli as cli_module

    parser = cli_module.build_parser()
    for name in path:
        sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
        parser = sub.choices[name]
    return parser


def _flag_cases() -> list:
    """(command, flag list, flag, argv, present) for every flag that
    conflicts with --consent / --discard-preview: absent, given with a
    value, and — a value flag — given EMPTY; a store_true flag by its real
    default (absent) or given."""
    from dct import cli as cli_module

    cases = []
    for command, names in (("apply", "_CONSENT_BINDING_FLAGS"),
                           ("prepare", "_PREVIEW_ONLY_FLAGS")):
        actions = _actions(_subparser("release", command))
        for _, flag in getattr(cli_module, names):
            cases.append((command, names, flag, [], False))
            if actions[flag].nargs == 0:
                cases.append((command, names, flag, [flag], True))
            else:
                cases.append((command, names, flag, [flag, "x"], True))
                cases.append((command, names, flag, [flag, ""], True))
    return cases


@pytest.mark.parametrize(("command", "names", "flag", "argv", "present"), _flag_cases(),
                         ids=lambda v: repr(v) if isinstance(v, list) else str(v))
def test_a_conflicting_flag_is_in_use_exactly_when_present(command, names, flag, argv,
                                                           present):
    from dct import cli as cli_module

    parser = _subparser("release", command)
    base = ["--consent", "a" * 64, "--confirm"] if command == "apply" else ["--discard-preview"]
    args = parser.parse_args([*base, *argv])
    used = cli_module._prepare_flags_in_use(args, getattr(cli_module, names))
    assert (flag in used) is present, (flag, argv, used)


def test_the_parser_defaults_mean_absent():
    """Booleans by their real defaults: a store_true flag that was not given
    is False, a value flag None — both read as absent."""
    from dct import cli as cli_module

    for path, names in ((("release", "apply"), cli_module._CONSENT_BINDING_FLAGS),
                        (("release", "prepare"), cli_module._PREVIEW_ONLY_FLAGS)):
        parser = _subparser(*path)
        args = parser.parse_args(["--consent", "a" * 64] if path[-1] == "apply" else [])
        assert cli_module._prepare_flags_in_use(args, names) == []


@pytest.mark.parametrize("extra,flags", [
    (["--version", ""], ["--version"]),
    (["--entries", ""], ["--entries"]),
    (["--plan-digest", ""], ["--plan-digest"]),
    (["--proposal-digest", ""], ["--proposal-digest"]),
    (["--push-remote", ""], ["--push-remote"]),
    (["--push-branch", ""], ["--push-branch"]),
    (["--push-url", ""], ["--push-url"]),
])
def test_an_empty_binding_flag_beside_consent_is_refused(engine, rel, cli, extra, flags):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    c = variant(do_preview(cli, repo))["consent_digest"]
    doc = refused_apply(cli, repo, c, *extra, key="consent_flag_conflict")
    assert doc["details"]["consent_flag_conflict"] == flags
    assert doc["details"]["made"] == NO_MADE
    assert get_release(engine, SLUG) is None


@pytest.mark.parametrize("extra", [["--version", ""], ["--entries", ""], ["--attest", ""],
                                   ["--message-file", ""], ["--push-url", ""]])
def test_an_empty_flag_beside_discard_preview_is_a_usage_error(engine, rel, cli, extra):  # noqa: F811
    repo = rel["repo"]
    code, _, err = cli(repo, "prepare", "--discard-preview", *extra)
    assert code == 2 and "--discard-preview takes no other flag" in err, (code, err)


# ═════════════════════════════════════════════════════════════════════════════
# "Release with all N" after "None", with another version
# ═════════════════════════════════════════════════════════════════════════════


def test_attest_without_entries_is_refused(engine, rel, cli):  # noqa: F811
    """`--attest` marks entries as done so they are released; with
    `--no-entries` nothing is released — the combination has no screen."""
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    code, doc, err = cli(repo, "prepare", "--preview", "--no-entries", "--attest", str(waiting))
    assert code == 1, (code, err)
    assert doc["details"]["attest_without_entries"] == {"attest": [waiting]}, doc["details"]
    assert slot(engine) is None


def test_none_then_all_then_another_version_shows_the_entries_of_all(engine, rel, cli):  # noqa: F811
    """Dialog B "None" (--no-entries) → the screen offers "Release with all
    N"; choosing it with another version REPLACES the selection: the next
    preview passes --entries <all.selected> --attest <all.attest.ids>
    --version V2 and no --no-entries — and its screen shows exactly those
    entries, attests exactly those ids, at V2."""
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    waiting = add_entry(engine, SLUG, "fixed", "waiting b")["id"]
    first = do_preview(cli, repo, "--no-entries")
    assert variant(first)["consent"]["entries"]["selected"] == []  # precondition: "None"
    every = variant(first, "all")["consent"]
    selected, attest = every["entries"]["selected"], every["attest"]["ids"]
    assert selected and waiting in selected and attest == [waiting], (selected, attest)
    second = do_preview(cli, repo, "--entries", ",".join(map(str, selected)),
                        "--attest", ",".join(map(str, attest)), "--version", "1.2.0")
    shown = variant(second)
    assert shown["consent"]["version"]["target"] == "1.2.0"
    assert shown["consent"]["entries"]["selected"] == selected
    assert shown["consent"]["attest"]["ids"] == attest
    assert [e["id"] for e in shown["display"]["entries"]["selected"]] == selected
    assert shown["version_only"] is False
    out = applied(cli, repo, shown["consent_digest"])
    record = get_release(engine, SLUG, "1.2.0")
    assert record["entry_ids"] == sorted(selected)
    attest_commit = out["details"]["made"]["attest_commit"]
    assert subject(repo, attest_commit) == ATTEST_SUBJECT
    assert record["candidate_sha"] == attest_commit


# ═════════════════════════════════════════════════════════════════════════════
# Machine keys on the preview's version refusals
# ═════════════════════════════════════════════════════════════════════════════


def test_a_lower_version_is_refused_with_version_lower(engine, rel, cli):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    code, doc, _ = cli(repo, "prepare", "--preview", "--version", "0.9.0")
    assert code == 1
    assert doc["details"]["version_lower"] == {"version": "0.9.0", "current": "1.0.0"}


def test_a_released_version_is_refused_with_version_taken(engine, rel, cli):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    applied(cli, repo, variant(do_preview(cli, repo, "--version", "1.1.0"))["consent_digest"])
    proven(engine, repo, "feature b")
    code, doc, _ = cli(repo, "prepare", "--preview", "--version", "1.1.0")
    assert code == 1
    assert doc["details"]["version_taken"] == {"version": "1.1.0", "status": "completed"}


def test_prepare_refuses_the_same_versions_with_the_same_keys(engine, rel, cli):  # noqa: F811
    """The D/P prepare shares the check (one helper): the same machine keys."""
    from dct.workflows.release.effects import NothingToPrepare, prepare_release

    repo = rel["repo"]
    proven(engine, repo, "feature a")
    with pytest.raises(NothingToPrepare) as lower:
        prepare_release(engine, SLUG, cwd=str(repo), version="0.9.0", no_entries=True,
                        strict=True)
    assert lower.value.details["version_lower"] == {"version": "0.9.0", "current": "1.0.0"}
    applied(cli, repo, variant(do_preview(cli, repo, "--version", "1.1.0"))["consent_digest"])
    with pytest.raises(NothingToPrepare) as taken:
        prepare_release(engine, SLUG, cwd=str(repo), version="1.1.0", no_entries=True,
                        strict=True)
    assert taken.value.details["version_taken"] == {"version": "1.1.0", "status": "completed"}


@pytest.mark.parametrize("given", ["abc", "1.x", "1.2.3.4"])
def test_an_invalid_version_is_refused_with_version_invalid(engine, rel, cli, given):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    code, doc, _ = cli(repo, "prepare", "--preview", "--version", given)
    assert code == 1
    assert doc["details"]["version_invalid"] == {"version": given}, doc["details"]


def test_index_only_keeps_its_own_key(engine, rel, cli, tmp_path):  # noqa: F811
    repo = rel["repo"]
    commit_all(repo, {"b.txt": "b0\n"})
    write(repo, "b.txt", "b1\n")
    git(repo, "add", "b.txt")
    write(repo, "b.txt", "b0\n")
    msg = message_file(tmp_path, "feat: x\n")
    code, doc, _ = cli(repo, "prepare", "--preview", "--message-file", msg)
    assert code == 1
    assert doc["details"]["index_only"] == {"paths": ["b.txt"]}
    assert not {"version_lower", "version_taken", "version_invalid"} & set(doc["details"])


# ═════════════════════════════════════════════════════════════════════════════
# Untracked files left out of the preview are listed, never added
# ═════════════════════════════════════════════════════════════════════════════


def test_a_clean_tree_lists_its_untracked_files_as_left_out(engine, rel, cli):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    before = variant(do_preview(cli, repo))["consent_digest"]
    write(repo, "notes.txt", "scratch\n")
    write(repo, ".env", "TOKEN=x\n")
    doc = do_preview(cli, repo)
    assert doc["details"]["work"] is None  # precondition: nothing to commit
    assert doc["details"]["left_out"] == [{"path": ".env", "secret_like": True},
                                          {"path": "notes.txt", "secret_like": False}]
    assert variant(doc)["consent_digest"] == before, \
        "files left out changed the consent digest"
    status = git(repo, "status", "--porcelain", "--untracked-files=all").splitlines()
    assert sorted(status) == ["?? .env", "?? notes.txt"], status


def test_a_dirty_tree_lists_the_untracked_files_it_leaves_out(engine, rel, cli, tmp_path):  # noqa: F811
    repo = rel["repo"]
    commit_all(repo, {"b.txt": "b0\n"})
    wip = add_entry(engine, SLUG, "added", "work in progress")["id"]
    write(repo, "b.txt", "b1\n")
    write(repo, "notes.local", "scratch\n")
    msg = message_file(tmp_path, f"feat: work in progress\n\nChangelog: {wip}\n")
    doc = do_preview(cli, repo, "--message-file", msg)
    assert doc["details"]["work"]["untracked_left"] == ["notes.local"]
    assert doc["details"]["left_out"] == [{"path": "notes.local", "secret_like": False}]


def test_ignored_files_are_never_listed(engine, rel, cli):  # noqa: F811
    repo = rel["repo"]
    proven(engine, repo, "feature a")
    commit_all(repo, {".gitignore": "*.log\n"})
    write(repo, "build.log", "noise\n")
    doc = do_preview(cli, repo)
    assert doc["details"]["left_out"] == []

