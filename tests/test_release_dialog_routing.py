"""ROUTING and CONTINUATION of the `/release` dialog on the REAL workflow
(K-111, K-112, K-120 — round 4).

The dialog is prose (dct/skills/release/SKILL.md, the `/release` fallback of
docs/assistant-fallbacks.md). What these tests pin is that the prose DECIDES
and RUNS what the code does:

- the routing table of the dialog (Step 1) is read as data and applied to
  the plan JSON the real CLI prints (`dct release plan --json`, schema 3) —
  for a dct release whose project switched to `own`, an `own` release whose
  project switched to `dct`, a record without a consent and a new release.
  Each lands on the row the effects layer agrees with (the row's command is
  then run), and routing itself writes nothing: no new stamp.
- the commands are the DOCUMENTED ones: every argv below is built from the
  flag list of the invocation the document shows (`documented`), so a flag
  the text drops or invents breaks the run, not only a string comparison.
- continuation: an unchanged resume needs no further approval and finishes
  the commit, the tag and the push; a takeover is a new consent (preview →
  that digest → resume); work in progress is committed inside the workflow
  (the `/commit` procedure), the plan recomputed, the release finished and
  pushed to the approved target.

Table / numbering equality of the two documents is the auxiliary doc test
(tests/test_skill_prose_changelog.py)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import git, run_cli
from test_release_effects import (
    SLUG,
    Interrupted,
    apply,
    assert_released,
    commit,
    interrupt_at,
    make_release_repo,
    proven,
    record_date,
    released_section,
    remote_refs,
    use_cli,
)
from test_release_executor import world
from test_release_round4_reconstruct import legacy
from test_skill_prose_changelog import (
    _commands,
    _refusal_rows,
    _release_fallback,
    _resume_path,
    _skill,
    route,
    routing_rows,
)

from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import set_release_executor
from dct.core.releases import get_release
from dct.db import releases
from dct.workflows.release.plan import PLAN_KEYS

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

DIALOGS = [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
]
BARE_FLAGS = {"--json", "--confirm", "--no-push", "--no-entries", "--none"}


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The workflow creates commits."""


@pytest.fixture
def proj(engine, tmp_path):
    return make_release_repo(engine, tmp_path)


@pytest.fixture
def cli(proj, engine, monkeypatch, capsys):
    """(repo, run): the real CLI in the project's checkout on the test
    database; run(argv) → (exit code, the JSON document it printed)."""
    repo, _ = use_cli(proj, engine, monkeypatch)

    def run(argv: list[str]) -> tuple[int, dict]:
        capsys.readouterr()
        code = 0
        try:
            run_cli(monkeypatch, argv)
        except SystemExit as exc:
            code = exc.code or 0
        return code, json.loads(capsys.readouterr().out)

    return repo, run


def documented(body: str, sub: str, *, having: tuple = (), lacking: tuple = ()) -> list[str]:
    """The flags (in order) of the first `dct release <sub> …` invocation
    `body` shows that carries every flag of `having` and none of `lacking`."""
    for group, name, flags in _commands(body):
        if (group, name) == ("release", sub) and set(having) <= set(flags) \
                and not set(lacking) & set(flags):
            return flags
    raise AssertionError(f"no documented `dct release {sub}` with {having} without {lacking}")


def argv(sub: str, flags: list[str], values: dict[str, str] | None = None) -> list[str]:
    """`dct release <sub>` with exactly the documented flags; every flag
    that takes a value gets it from `values` — a documented value flag the
    test has no value for is a failure (the document grew a flag)."""
    values = values or {}
    out = ["release", sub]
    for flag in flags:
        out.append(flag)
        if flag in values:
            out.append(values[flag])
        else:
            assert flag in BARE_FLAGS, f"dct release {sub}: no value for {flag}"
    return out


def first_plan(body: str, run) -> dict:
    """Step 0 as documented — `dct release plan --json` — and its document."""
    code, plan = run(argv("plan", documented(body, "plan", having=("--json",),
                                             lacking=("--version",))))
    assert code == 0
    return plan


def release_rows(engine) -> list[tuple]:
    with engine.begin() as conn:
        return [tuple(r) for r in conn.execute(
            sa.select(releases.c.id, releases.c.version, releases.c.executor,
                      releases.c.created_at).order_by(releases.c.id))]


def stamped_versions(engine) -> dict[int, str | None]:
    return {e["id"]: e["version"] for e in list_entries(engine, SLUG)}


def routed_commands(row: dict) -> set[str]:
    return {sub for group, sub, _ in _commands(row["text"]) if group == "release"}


# ── the routing table reads the plan's real keys ────────────────────────────


@pytest.mark.parametrize("text", DIALOGS)
def test_routing_conditions_use_keys_the_plan_really_has(text, engine, cli):
    """Every `<path> == <value>` of the table resolves in the plan the CLI
    prints (a renamed key would silently route nothing): the head keys are
    PLAN_KEYS, `executor` holds project + record, and the record's keys are
    the ones core.releases.release_routing returns."""
    repo, run = cli
    a = proven(engine, repo, "feature a")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at("after_stamp"))
    plan = first_plan(text(), run)
    assert tuple(plan) == PLAN_KEYS
    leaves = {"kind", "executor.project"} | {
        f"executor.record.{key}" for key in plan["executor"]["record"]}
    for row in routing_rows(text()):
        for path, _ in row["conditions"]:
            assert path in leaves, f"{path}: not a key of the plan ({sorted(leaves)})"


# ── K-111: an unfinished release is routed by ITS record ────────────────────


@pytest.mark.parametrize("text", DIALOGS)
def test_a_dct_release_is_resumed_although_the_project_is_own_now(text, engine, cli):
    """dct → own: the dialog must not stop at today's `own` setting. The
    plan shows both executors in ONE result; the table routes by the record
    to `dct release resume`; routing writes nothing (no new stamp); the
    documented resume then finishes commit, tag and push on the stored
    consent — no approval flag exists (K-112)."""
    body = text()
    repo, run = cli
    a = proven(engine, repo, "feature a")
    candidate = git(repo, "rev-parse", "HEAD")
    with pytest.raises(Interrupted):
        apply(engine, repo, version="1.1.0", entry_ids=[a], on_point=interrupt_at("after_stamp"))
    set_release_executor(engine, SLUG, "own")
    rows, stamps, before = release_rows(engine), stamped_versions(engine), world(engine, repo)

    plan = first_plan(body, run)
    assert plan["schema_version"] == 4 and plan["kind"] == "resume"
    assert plan["executor"] == {"project": "own", "record": {
        "version": "1.1.0", "status": "stamped", "executor": "dct", "progress_owner": "dct",
        "consent": True}}
    row = route(body, plan)
    assert routed_commands(row) == {"resume"}, row["text"]
    assert "STOP" not in row["text"]
    assert world(engine, repo) == before  # routing: nothing written, nothing stamped

    resume_flags = documented(_resume_path(body), "resume", having=("--json",))
    assert resume_flags == ["--json"]  # no approval flag: the stored consent
    code, doc = run(argv("resume", resume_flags))
    assert code == 0 and doc["outcome"] == "completed"
    steps = {step["step"]: step["result"] for step in doc["steps"]}
    assert (steps["commit"], steps["tag"], steps["push"]) == ("done", "done", "done")
    record = assert_released(engine, repo, candidate, [a])  # commit + tag + the push verified
    assert record["executor"] == "dct"
    assert release_rows(engine) == rows  # the SAME record: no second stamp, no new version
    assert stamped_versions(engine) == stamps


@pytest.mark.parametrize("text", DIALOGS)
def test_an_own_release_stays_with_its_tool_although_the_project_is_dct_now(text, engine, cli):
    """own → dct: the dialog must not treat it as dct's because the setting
    says `dct` today — the row hands it to the own tool (the ledger path),
    names no dct command for it, and the effects layer agrees: resume and
    takeover refuse, nothing changes."""
    body = text()
    repo, run = cli
    set_release_executor(engine, SLUG, "own")
    a = proven(engine, repo, "feature a")
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a], expected_executor="own",
                    candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
    set_release_executor(engine, SLUG, "dct")
    rows, before = release_rows(engine), world(engine, repo)

    plan = first_plan(body, run)
    assert plan["kind"] == "resume"
    assert plan["executor"] == {"project": "dct", "record": {
        "version": "1.1.0", "status": "stamped", "executor": "own", "progress_owner": "ledger",
        "consent": False}}
    row = route(body, plan)
    assert not routed_commands(row) & {"resume", "takeover", "apply", "prepare"}, row["text"]
    assert "own" in row["who"] and "ledger path" in row["path"]

    code, doc = run(["release", "resume", "--json"])
    assert code == 1 and doc["outcome"] == "refused" and doc["details"]["executor"] == "own"
    code, doc = run(["release", "takeover", "--version", "1.1.0", "--no-push", "--json"])
    assert code == 1 and doc["outcome"] == "refused"
    assert world(engine, repo) == before and release_rows(engine) == rows
    assert get_release(engine, SLUG, "1.1.0")["executor"] == "own"


@pytest.mark.parametrize("text", DIALOGS)
def test_a_record_without_consent_is_offered_the_takeover_not_resumed(text, engine, cli):
    """Stamped outside `apply` (executor dct, no consent): the row offers
    the takeover or the ledger path; the effects layer refuses a resume and
    names the same two ways."""
    body = text()
    repo, run = cli
    a = proven(engine, repo, "feature a")
    release_entries(engine, SLUG, "1.1.0", entry_ids=[a],
                    candidate_sha=git(repo, "rev-parse", "HEAD"), checkout_root=str(repo))
    before = world(engine, repo)
    plan = first_plan(body, run)
    assert plan["executor"]["record"] == {
        "version": "1.1.0", "status": "stamped", "executor": "dct", "progress_owner": "ledger",
        "consent": False}
    row = route(body, plan)
    assert "takeover" in routed_commands(row) and "ledger path" in row["path"]
    assert "resume" not in routed_commands(row)
    code, doc = run(["release", "resume", "--json"])
    assert code == 1 and doc["details"]["consent"] is None
    assert "dct release takeover" in doc["details"]["error"]
    assert world(engine, repo) == before


@pytest.mark.parametrize("text", DIALOGS)
@pytest.mark.parametrize("setting, marker", [("own", "STOP"), ("dct", "Step 2")])
def test_a_new_release_is_routed_by_the_project_setting(text, setting, marker, engine, cli):
    """No unfinished record: `executor.record` is null and the project
    setting decides — `own` hands over (and the effects layer refuses a
    prepare), `dct` goes on."""
    body = text()
    repo, run = cli
    set_release_executor(engine, SLUG, setting)
    proven(engine, repo, "feature a")
    before = world(engine, repo)
    plan = first_plan(body, run)
    assert plan["kind"] == "release" and plan["executor"] == {"project": setting, "record": None}
    row = route(body, plan)
    assert marker in row["path"], row["text"]
    assert (setting == "own") == ("own" in row["who"])
    assert world(engine, repo) == before


# ── K-112 / R8d: a takeover is a NEW consent — preview, that digest, resume ──


@pytest.mark.parametrize("text", DIALOGS)
def test_the_documented_takeover_records_the_shown_consent_then_resume_finishes(
        text, engine, cli):
    """A reconstruction that ended at `committed` (executor dct, no
    consent): the row offers the takeover; the documented preview shows the
    consent, the missing effects and the digest; the documented
    confirmation takes exactly that digest; the documented resume performs
    only the tag and the push."""
    body = text()
    section = _resume_path(body)
    repo, run = cli
    a, candidate, head = legacy(engine, repo, tag=None)
    code, doc = run(argv("reconstruct", documented(body, "reconstruct", having=("--confirm",),
                                                   lacking=("--evidence", "--release-commit")),
                         {"--version": "1.1.0"}))
    assert code == 0 and doc["outcome"] == "reconstructed"
    assert doc["release"]["status"] == "committed"

    plan = first_plan(body, run)
    assert plan["executor"]["record"]["consent"] is False
    assert "takeover" in routed_commands(route(body, plan))

    push = {"--version": "1.1.0", "--push-remote": "origin", "--push-branch": "main"}
    before = world(engine, repo)
    code, preview = run(argv("takeover", documented(
        section, "takeover", having=("--push-remote",), lacking=("--takeover-digest",)), push))
    assert code == 0 and preview["outcome"] == "preview" and world(engine, repo) == before
    details = preview["details"]
    assert details["consent"]["entry_ids"] == [a]
    assert details["consent"]["candidate_sha"] == candidate
    assert details["consent"]["takeover"]["release_commit_sha"] == head
    assert details["consent"]["push_target"]["url"] == git(repo, "remote", "get-url", "--push",
                                                           "origin")
    assert details["missing_effects"] == ["tag", "push"]

    code, taken = run(argv("takeover", documented(
        section, "takeover", having=("--push-remote", "--takeover-digest")),
        {**push, "--takeover-digest": details["takeover_digest"]}))
    assert code == 0 and taken["outcome"] == "taken_over"
    assert git(repo, "tag", "--list", "v1.1.0") == ""  # a takeover performs no effect

    code, doc = run(argv("resume", documented(section, "resume", having=("--json",))))
    assert code == 0 and doc["outcome"] == "completed"
    steps = {step["step"]: step["result"] for step in doc["steps"]}
    assert (steps["commit"], steps["tag"], steps["push"]) == ("verified", "done", "done")
    remote = remote_refs(repo)
    assert remote["refs/tags/v1.1.0"] == head and remote["refs/heads/main"] == head


# ── K-120: WIP commit → re-plan → release → the approved push, one workflow ──


@pytest.mark.parametrize("text", DIALOGS)
def test_work_in_progress_is_committed_then_the_documented_run_releases_and_pushes(
        text, engine, cli):
    """The run the guard and the dialog protect: with uncommitted work the
    workflow refuses (the refusal row points at Step 3 — the commit inside
    the workflow); the agent commits it by the /commit procedure (trailer
    from `dct clog candidates`), plans again, binds the selection, prepares,
    applies — the documented commands, with the documented flags, the push
    choice being the complete target the prepare showed (K-123) — and the
    release commit and its tag are on the approved push target."""
    body = text()
    repo, run = cli
    commit(repo, "chore: base")                       # f.txt, tracked and clean
    eid = add_entry(engine, SLUG, "added", "wip feature")["id"]
    Path(repo, "f.txt").write_text("work in progress\n")

    # Step 0 / 3: the plan shows the dirty index; prepare refuses it untouched
    plan = first_plan(body, run)
    assert plan["kind"] == "release" and plan["index"]["clean"] is False
    assert plan["index"]["unstaged"] == ["f.txt"]
    bind = documented(body, "plan", having=("--version", "--no-entries"))
    _, bound = run(argv("plan", bind, {"--version": "1.1.0"}))
    code, doc = run(argv("prepare", documented(body, "prepare", having=("--no-entries",)),
                         {"--version": "1.1.0", "--plan-digest": bound["snapshot_digest"]}))
    assert code == 1 and doc["outcome"] == "refused" and doc["details"]["unstaged"] == ["f.txt"]
    assert "Step 3" in _refusal_rows(body)["unstaged"]

    # Step 3: the /commit procedure, performed by the agent in this workflow
    code, candidates = run(["clog", "candidates", "--json"])
    assert code == 0
    offered = {e["id"] for key in ("confirmed", "uncertain") for e in candidates[key]}
    assert eid in offered
    head = commit(repo, f"feat: wip feature\n\nChangelog: {eid}\n")

    # … then the plan AGAIN: the new HEAD is the candidate, the entry proven
    plan = first_plan(body, run)
    assert plan["index"]["clean"] is True and plan["checkout"]["candidate_sha"] == head
    assert [e["id"] for e in plan["entries"]["proven"]] == [eid]

    # Steps 7–9 with the documented flags
    values = {"--version": "1.1.0", "--entries": str(eid)}
    _, bound = run(argv("plan", documented(body, "plan", having=("--version", "--entries")),
                        values))
    values["--plan-digest"] = bound["snapshot_digest"]
    code, prepared = run(argv("prepare", documented(body, "prepare", having=("--entries",)),
                              values))
    assert code == 0 and prepared["outcome"] == "prepared"
    url = git(repo, "remote", "get-url", "--push", "origin")
    shown = prepared["details"]["push_target"]                # what the owner approves
    assert shown == {"remote": "origin", "branch": "main", "url": url, "tag": "v1.1.0"}
    values["--proposal-digest"] = prepared["details"]["proposal_sha256"]
    # K-123: no documented apply pushes without the complete approved target —
    # the dialog passes details.push_target.{remote,branch,url} of the prepare.
    pushing = [flags for group, sub, flags in _commands(body)
               if (group, sub) == ("release", "apply") and len(flags) > 1
               and "--no-push" not in flags]
    assert pushing and all(
        {"--push-remote", "--push-branch", "--push-url"} <= set(flags) for flags in pushing)
    values.update({"--push-remote": shown["remote"], "--push-branch": shown["branch"],
                   "--push-url": shown["url"]})
    code, doc = run(argv("apply", documented(body, "apply", having=("--entries",),
                                             lacking=("--no-push",)), values))
    assert code == 0 and doc["outcome"] == "completed"
    steps = {step["step"]: step for step in doc["steps"]}
    assert steps["push"]["result"] == "done" and steps["push"]["url"] == url
    assert doc["details"]["push_target"]["url"] == url
    assert_released(engine, repo, head, [eid],
                    section=released_section(record_date(engine), lines="- wip feature\n"))
