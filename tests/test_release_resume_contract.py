"""The documented /release RESUME walk against the real record code (K-41, R5).

test_skill_prose_changelog.py pins the NORMAL path's advance order against a
hard-coded list. Here the resume path is tied to the code instead of to
words: the advance_release calls the skill and the authoritative fallback
document for a resume are extracted from the text, checked against
RELEASE_STATUSES (a renamed / reordered status breaks the docs, not just the
code), and then REPLAYED against a release interrupted at every possible
point — the walk must finish the record without a single refused call.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dct.assets import skills_dir
from dct.core.changelog import add_entry, release_entries
from dct.core.projects import create_project
from dct.core.releases import RELEASE_STATUSES, advance_release, get_release

FALLBACKS = Path(__file__).resolve().parent.parent / "docs" / "assistant-fallbacks.md"
CANDIDATE = "a" * 40
RELEASE_COMMIT = "b" * 40


def _skill_resume() -> str:
    """The skill's resume section plus every normal-path step it delegates to
    ("Step 10, then Step 11") — the walk an agent actually performs."""
    body = (skills_dir() / "release" / "SKILL.md").read_text(encoding="utf-8")
    start = body.index("## Resume path")
    resume = body[start:body.index("\n## ", start + 1)]
    delegated = []
    for n in dict.fromkeys(re.findall(r"\bStep (\d+)\b", resume.split("6. **Push", 1)[-1])):
        head = body.index(f"### Step {n}:")
        ends = [i for i in (body.find("\n### ", head + 1), body.find("\n## ", head + 1)) if i > 0]
        delegated.append(body[head:min(ends)])
    return resume + "\n" + "\n".join(delegated)


def _fallback_resume() -> str:
    body = FALLBACKS.read_text(encoding="utf-8")
    start = body.index("**Resume path**")
    return body[start:body.index("\n## ", start)]


RESUME_DOCS = [
    pytest.param(_skill_resume, id="skill"),
    pytest.param(_fallback_resume, id="fallback"),
]


def _documented_advances(text: str) -> list[tuple[str, bool]]:
    """(status, re-pins candidate_sha) for each advance_release call, in order."""
    calls = re.findall(r"advance_release\(((?:[^()]|\([^()]*\))*)\)", text)
    out = []
    for args in calls:
        status = re.search(r'"(\w+)"', args).group(1)
        out.append((status, "candidate_sha=" in args))
    return out


@pytest.mark.parametrize("doc", RESUME_DOCS)
def test_resume_advances_every_effect_in_the_codes_status_order(doc):
    advances = _documented_advances(doc())
    statuses = [s for s, _ in advances]
    assert all(s in RELEASE_STATUSES for s in statuses), statuses
    # Every status after 'stamped' is reachable by a documented resume call,
    # in the record's forward order.
    assert statuses == list(RELEASE_STATUSES[1:]), statuses
    # Only the move into 'committed' re-pins (the release commit SHA).
    assert [s for s, repin in advances if repin] == ["committed"]


@pytest.mark.parametrize("doc", RESUME_DOCS)
def test_resume_walk_names_skip_perform_and_stop_for_each_effect(doc):
    flat = " ".join(doc().split())
    assert re.search(r"SKIP an effect that exists and matches", flat)
    assert re.search(r"STOP (and report )?on any contradiction", flat, re.IGNORECASE)
    # Each git-side effect has its own "already there → skip / elsewhere → STOP".
    assert re.search(r"it exists, skip", flat)
    assert re.search(r"pointing at the release commit → skip; pointing elsewhere → STOP", flat)
    assert re.search(r"later commits( on top)? → STOP", flat)


def _interrupted_release(engine, crashed_at: str):
    create_project(engine, slug="proj-a", name="Project A", path="/tmp/proj-a",
                   current_version="1.0.0")
    eid = add_entry(engine, "proj-a", "added", "shipped work")["id"]
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[eid],
                    candidate_sha=CANDIDATE, checkout_root="/tmp/proj-a")
    for status in RELEASE_STATUSES[1:RELEASE_STATUSES.index(crashed_at) + 1]:
        sha = RELEASE_COMMIT if status == "committed" else None
        advance_release(engine, "proj-a", "1.1.0", status, candidate_sha=sha)
    return eid


@pytest.mark.parametrize("doc", RESUME_DOCS)
@pytest.mark.parametrize("crashed_at", RELEASE_STATUSES[:-1])
def test_replaying_the_documented_resume_finishes_every_interrupted_release(
    engine, doc, crashed_at,
):
    """Interrupt after each effect, then follow the documented walk: an effect
    whose status the record already passed is only verified (no call); every
    other documented advance must be ACCEPTED, and the record ends completed
    with the release commit pinned."""
    eid = _interrupted_release(engine, crashed_at)
    pending = get_release(engine, "proj-a")  # Step 0: the unfinished record
    assert (pending["version"], pending["status"]) == ("1.1.0", crashed_at)
    assert pending["entry_ids"] == [eid]

    stored = RELEASE_STATUSES.index(crashed_at)
    for status, repins in _documented_advances(doc()):
        if RELEASE_STATUSES.index(status) < stored:
            continue  # "Status X or later: verify only"
        sha = RELEASE_COMMIT if repins else None
        advance_release(engine, "proj-a", "1.1.0", status, candidate_sha=sha)

    done = get_release(engine, "proj-a", "1.1.0")
    assert done["status"] == "completed" and done["completed_at"] is not None
    assert done["candidate_sha"] == RELEASE_COMMIT
    assert done["entry_ids"] == [eid]
    assert get_release(engine, "proj-a") is None, "nothing left to resume"
    # A second resume of the same version is a no-op returning the record.
    again = release_entries(engine, "proj-a", "1.1.0", entry_ids=[eid])
    assert again["released"] == 0 and again["release"] == done
