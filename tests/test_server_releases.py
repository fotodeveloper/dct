"""MCP surface of the durable release record (K-41): release_changelog's
candidate_sha / checkout_root + `release`, get_release, advance_release."""

import json

import pytest

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"
RECORD_KEYS = {"id", "version", "mode", "entry_ids", "candidate_sha", "checkout_root",
               "status", "created_at", "updated_at", "completed_at"}


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


def test_advance_release_repins_sha_before_committed(released):
    released.advance_release("1.1.0", "committed", project="test", candidate_sha=SHA2)
    rec = released.get_release(project="test", version="1.1.0")["release"]
    assert (rec["status"], rec["candidate_sha"]) == ("committed", SHA2)
    out = released.advance_release("1.1.0", "tagged", project="test", candidate_sha=SHA)
    assert "candidate_sha cannot change" in out["error"]


@pytest.mark.parametrize("version,status,match", [
    ("1.1.0", "shipped", "Invalid release status"),
    ("9.9.9", "tagged", "No release record"),
    ("1.1.0", "<parameter name=s>", "tool-call markup"),
])
def test_advance_release_errors(released, version, status, match):
    assert match in released.advance_release(version, status, project="test")["error"]


def test_advance_release_backwards_is_an_error(released):
    released.advance_release("1.1.0", "completed", project="test")
    assert "backwards" in released.advance_release("1.1.0", "tagged", project="test")["error"]
    assert released.get_release(project="test") == {"release": None}  # all completed


def test_advance_release_unknown_project_is_an_error(srv):
    assert "not found" in srv.advance_release("1.0.0", "tagged", project="nope")["error"]


def test_new_release_tools_are_deferred():
    from dct.mcp.server import _ALWAYS_LOAD_TOOLS

    assert not {"get_release", "advance_release"} & _ALWAYS_LOAD_TOOLS
