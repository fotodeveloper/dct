"""MCP surface of the durable release record (K-41, K-60, K-61):
release_changelog's candidate_sha / checkout_root / proof + `release`,
get_release, advance_release (release_commit_sha, manifest), abandon_release."""

import json

import pytest
from conftest import ledger_proofs

pytestmark = pytest.mark.slow

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"
RECORD_KEYS = {"id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
               "release_commit_sha", "manifest", "proof", "status", "created_at",
               "updated_at", "completed_at", "abandoned_at", "abandon_reason",
               "release_mode", "executor", "consent", "proposal", "render",
               "tag_proof", "push_proof"}


@pytest.fixture
def released(srv):
    srv.add_changelog("added", "a", project="test")
    out = srv.release_changelog(project="test", version="1.1.0", candidate_sha=SHA,
                                checkout_root="/tmp/test")
    assert "error" not in out, out
    return srv


def test_release_changelog_pins_sha_and_root_in_the_record(released):
    rec = released.get_release(project="test", version="1.1.0")["release"]
    assert set(rec) == RECORD_KEYS
    assert (rec["candidate_sha"], rec["checkout_root"], rec["status"]) == (
        SHA, "/tmp/test", "stamped")


def test_release_record_is_json_native(released):
    rec = released.get_release(project="test")["release"]
    json.dumps(rec)  # datetimes rendered as ISO strings
    assert isinstance(rec["created_at"], str)


@pytest.mark.parametrize("field,value,match", [
    ("candidate_sha", "HEAD", "candidate_sha"),
    ("checkout_root", "rel/path", "absolute path"),
    ("candidate_sha", "<parameter name=x>", "tool-call markup"),
    ("checkout_root", "/tmp/<invoke name=x>", "tool-call markup"),
])
def test_release_changelog_refuses_bad_sha_or_root(srv, field, value, match):
    srv.add_changelog("added", "a", project="test")
    out = srv.release_changelog(project="test", version="1.1.0", **{field: value})
    assert match in out["error"]
    assert srv.get_release(project="test") == {"release": None}
    assert srv.count_unreleased_changelog(project="test") == {"count": 1}


def test_release_changelog_same_set_rerun_returns_the_record(released):
    again = released.release_changelog(project="test", version="1.1.0")
    assert again["released"] == 0
    assert again["release"]["status"] == "stamped"


def test_release_changelog_different_set_is_an_error(released):
    released.add_changelog("added", "b", project="test")
    out = released.release_changelog(project="test", version="1.1.0")
    assert "different set" in out["error"]


def test_get_release_unknown_version_and_nothing_unfinished(srv):
    assert srv.get_release(project="test", version="9.9.9") == {"release": None}
    assert srv.get_release(project="test") == {"release": None}


def test_get_release_bad_version_is_an_error(srv):
    assert "Unsupported version" in srv.get_release(project="test", version="latest")["error"]


def test_advance_release_ack_shape_and_idempotent_repeat(released):
    out = released.advance_release("1.1.0", "files_written", project="test")
    assert out == {"project": "test", "version": "1.1.0", "status": "files_written",
                   "updated": True}
    again = released.advance_release("1.1.0", "files_written", project="test")
    assert again == {**out, "updated": False}


def test_advance_release_records_the_release_commit_once(released):
    out = released.advance_release("1.1.0", "committed", project="test",
                                   release_commit_sha=SHA2)
    assert out["updated"] is True
    rec = released.get_release(project="test", version="1.1.0")["release"]
    assert (rec["status"], rec["candidate_sha"], rec["release_commit_sha"]) == (
        "committed", SHA, SHA2)
    out = released.advance_release("1.1.0", "tagged", project="test", release_commit_sha=SHA)
    assert "cannot change" in out["error"]
    assert released.get_release(project="test", version="1.1.0")["release"]["status"] == (
        "committed")


def test_advance_release_has_no_candidate_sha_parameter():
    import inspect

    from dct.mcp.server import advance_release

    params = inspect.signature(advance_release).parameters
    assert "candidate_sha" not in params
    assert {"release_commit_sha", "manifest"} <= set(params)


def test_advance_release_committed_without_release_commit_is_an_error(released):
    out = released.advance_release("1.1.0", "committed", project="test")
    assert "release commit" in out["error"]
    assert released.get_release(project="test")["release"]["status"] == "stamped"


def test_advance_release_manifest_is_write_once_per_key(released):
    manifest = {"changelog": {"path": "changelog.md", "sha256": "a" * 64}}
    out = released.advance_release("1.1.0", "files_written", project="test",
                                   manifest=manifest)
    assert out["updated"] is True
    rec = released.get_release(project="test", version="1.1.0")["release"]
    assert rec["manifest"] == manifest
    again = released.advance_release("1.1.0", "files_written", project="test",
                                     manifest=manifest)
    assert again["updated"] is False
    other = {"changelog": {"path": "changelog.md", "sha256": "b" * 64}}
    out = released.advance_release("1.1.0", "files_written", project="test", manifest=other)
    assert "different value" in out["error"]
    assert released.get_release(project="test", version="1.1.0")["release"]["manifest"] == (
        manifest)


def test_advance_release_refuses_abandoned_as_a_target(released):
    out = released.advance_release("1.1.0", "abandoned", project="test")
    assert "abandon_release" in out["error"]
    assert released.get_release(project="test")["release"]["status"] == "stamped"


def test_abandon_release_ack_and_record(released):
    refused = released.abandon_release("1.1.0", "superseded", project="test")
    assert "confirm" in refused["error"]
    assert "reason" in released.abandon_release(
        "1.1.0", "  ", confirm=True, project="test")["error"]
    out = released.abandon_release("1.1.0", "superseded", confirm=True, project="test")
    assert out == {"project": "test", "version": "1.1.0", "status": "abandoned",
                   "updated": True}
    rec = released.get_release(project="test", version="1.1.0")["release"]
    assert (rec["status"], rec["abandon_reason"]) == ("abandoned", "superseded")
    assert isinstance(rec["abandoned_at"], str)
    assert released.get_release(project="test") == {"release": None}
    again = released.abandon_release("1.1.0", "superseded", confirm=True, project="test")
    assert again == {**out, "updated": False}
    other = released.abandon_release("1.1.0", "other", confirm=True, project="test")
    assert "first reason stays" in other["error"]


def test_abandon_release_completed_is_an_error(released):
    released.advance_release("1.1.0", "completed", project="test",
                             **ledger_proofs("completed", SHA2))
    out = released.abandon_release("1.1.0", "late", confirm=True, project="test")
    assert "never reclassified" in out["error"]


def test_abandon_release_markup_is_an_error(released):
    out = released.abandon_release("1.1.0", "<parameter name=x>", confirm=True,
                                   project="test")
    assert "tool-call markup" in out["error"]


def test_release_changelog_stores_the_proof_map(srv):
    a = srv.add_changelog("added", "a", project="test")["id"]
    out = srv.release_changelog(project="test", version="1.1.0", entry_ids=[a],
                                proof={str(a): [SHA]})
    assert out["release"]["proof"] == {str(a): {"commits": [SHA], "merge_commit": None,
                                                "pr": None}}


def test_release_changelog_proof_outside_the_set_is_an_error(srv):
    a = srv.add_changelog("added", "a", project="test")["id"]
    out = srv.release_changelog(project="test", version="1.1.0", entry_ids=[a],
                                proof={str(a + 99): [SHA]})
    assert "outside the release set" in out["error"]
    assert srv.count_unreleased_changelog(project="test") == {"count": 1}


@pytest.mark.parametrize("version,status,match", [
    ("1.1.0", "shipped", "Invalid release status"),
    ("9.9.9", "tagged", "No release record"),
    ("1.1.0", "<parameter name=s>", "tool-call markup"),
])
def test_advance_release_errors(released, version, status, match):
    assert match in released.advance_release(version, status, project="test")["error"]


def test_advance_release_backwards_is_an_error(released):
    released.advance_release("1.1.0", "tagged", project="test",
                             **ledger_proofs("tagged", SHA2))
    assert "backwards" in released.advance_release("1.1.0", "committed", project="test")["error"]
    released.advance_release("1.1.0", "completed", project="test")
    assert "terminal" in released.advance_release("1.1.0", "tagged", project="test")["error"]
    assert released.get_release(project="test") == {"release": None}  # all completed


def test_advance_release_unknown_project_is_an_error(srv):
    assert "not found" in srv.advance_release("1.0.0", "tagged", project="nope")["error"]


def test_new_release_tools_are_deferred():
    from dct.mcp.server import _ALWAYS_LOAD_TOOLS

    assert not {"get_release", "advance_release", "abandon_release"} & _ALWAYS_LOAD_TOOLS


# ── K-62: a new version-only release must be explicit over MCP too ───────────


def test_release_changelog_with_nothing_unreleased_needs_explicit_empty_ids(srv):
    from dct.core.projects import get_project, set_version
    from dct.core.releases import get_release

    set_version(srv._get_engine(), "test", "1.0.0")
    for kwargs in ({"version": "1.1.0"}, {"bump_type": "minor"}):
        out = srv.release_changelog(project="test", **kwargs)
        assert set(out) == {"error"} and "must be explicit" in out["error"], out
    engine = srv._get_engine()
    assert get_project(engine, "test")["current_version"] == "1.0.0"
    assert get_release(engine, "test", "1.1.0") is None
    out = srv.release_changelog(project="test", version="1.1.0", entry_ids=[])
    assert out["mode"] == "version_only" and out["current_version"] == "1.1.0"
    # Resuming the same (recorded) version-only release stays a no-op.
    again = srv.release_changelog(project="test", version="1.1.0", entry_ids=[])
    assert again["release"]["id"] == out["release"]["id"]


def test_release_changelog_unparseable_current_version_is_a_controlled_error(srv):
    import sqlalchemy as sa

    from dct.db import projects

    engine = srv._get_engine()
    with engine.begin() as conn:
        conn.execute(projects.update().where(projects.c.slug == "test")
                     .values(current_version="2.0.0-rc1"))
    srv.add_changelog("added", "x", project="test")
    out = srv.release_changelog(project="test", version="1.0.0")
    assert set(out) == {"error"} and "not a supported version" in out["error"]
    with engine.begin() as conn:
        assert conn.execute(sa.select(projects.c.current_version)
                            .where(projects.c.slug == "test")).scalar() == "2.0.0-rc1"
