"""The /release RESUME and ABANDON dialog against the workflow's real contract
(K-41 / K-51 / K-60 / K-72).

The dialog no longer walks the effects itself: it hands an unfinished record
to `dct release resume` (which verifies every effect, skips what exists,
performs what is missing and records the progress) or, with the owner's
reason and explicit confirmation, to `dct release abandon`. These tests tie
the documents to the code instead of to words:

- the skill's and the fallback's resume paths name the workflow commands and
  never record progress themselves (no advance_release);
- the resume table of docs/release-workflow.md covers exactly the code's
  statuses (RELEASE_FLOW / TERMINAL_STATUSES), and its interruption table
  exactly effects.INTERRUPTION_POINTS, in order;
- the documented abandon contract (reason + explicit confirmation; the
  record, the stamps and current_version stay; V stays taken) is EXECUTED
  against the production effects layer.

Resume execution on real repositories at every interruption point lives in
tests/test_release_effects.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_skill_prose_changelog import _codex, _release_fallback, _resume_path

from dct.assets import skills_dir
from dct.core.changelog import add_entry, list_entries, release_entries
from dct.core.projects import create_project, get_project
from dct.core.releases import RELEASE_FLOW, TERMINAL_STATUSES, get_release
from dct.workflows.release import effects

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parent.parent
FALLBACKS = ROOT / "docs" / "assistant-fallbacks.md"
WORKFLOW_DOC = ROOT / "docs" / "release-workflow.md"
CANDIDATE = "a" * 40


def _skill_resume() -> str:
    body = (skills_dir() / "release" / "SKILL.md").read_text(encoding="utf-8")
    start = body.index("## Resume path")
    return body[start:body.index("\n## ", start + 1)]


def _fallback_resume() -> str:
    return _resume_path(_release_fallback())


RESUME_DOCS = [
    pytest.param(_skill_resume, id="skill"),
    pytest.param(_fallback_resume, id="fallback"),
    pytest.param(lambda: _resume_path(_codex("release")), id="codex"),
]


@pytest.mark.parametrize("doc", RESUME_DOCS)
def test_resume_path_hands_the_record_to_the_workflow(doc):
    flat = " ".join(doc().split())
    assert "dct release resume --json" in flat
    # Abandon only with the owner's reason AND the explicit confirmation flag.
    assert re.search(r'dct release abandon --version \S+ --reason "[^"]+" --confirm', flat)
    # The workflow records progress — the dialog never does.
    assert "advance_release(" not in flat
    # K-51: the record is never re-pinned to another checkout.
    assert "never re-pinned" in flat and "checkout_root" in flat


def _table_first_column(section: str) -> list[str]:
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    return [re.findall(r"`([a-z_]+)`", row.split("|")[1]) for row in rows]


def _resume_section() -> str:
    doc = WORKFLOW_DOC.read_text(encoding="utf-8")
    start = doc.index("### Resume and interruptions")
    return doc[start:doc.index("\n### ", start + 1)]


def test_resume_table_covers_exactly_the_codes_statuses():
    section = _resume_section()
    status_table = section[:section.index("Interruption points")]
    cells = _table_first_column(status_table)
    unfinished = [c[0] for c in cells if len(c) == 1]
    assert unfinished == [s for s in RELEASE_FLOW if s not in TERMINAL_STATUSES]
    terminal = [set(c) for c in cells if len(c) > 1]
    assert terminal == [set(TERMINAL_STATUSES)]


def test_interruption_table_is_the_codes_interruption_points():
    section = _resume_section()
    table = section[section.index("| point |"):]
    documented = [name for cell in _table_first_column(table) for name in cell]
    assert documented == list(effects.INTERRUPTION_POINTS)


def _stamped_release(engine, tmp_path):
    # K-118 L8: abandon locks the directory of the project's registered path
    # — it must exist (here: CONFIRMED outside git).
    plain = tmp_path / "proj-a"
    plain.mkdir()
    create_project(engine, slug="proj-a", name="Project A", path=str(plain),
                   current_version="1.0.0")
    eid = add_entry(engine, "proj-a", "added", "shipped work")["id"]
    release_entries(engine, "proj-a", "1.1.0", entry_ids=[eid],
                    candidate_sha=CANDIDATE, checkout_root="/tmp/proj-a")
    return eid


def test_abandon_without_explicit_confirmation_changes_nothing(engine, tmp_path):
    """The dialog's "explicit confirmation" is the workflow's own gate."""
    _stamped_release(engine, tmp_path)
    with pytest.raises(effects.ReleaseRefused):
        effects.abandon(engine, "proj-a", "1.1.0", "superseded", confirm=False)
    record = get_release(engine, "proj-a")
    assert record is not None and record["status"] == "stamped"


def test_abandon_keeps_the_record_the_stamps_and_the_version(engine, tmp_path):
    """As the dialog tells the owner: everything stays, V stays taken."""
    eid = _stamped_release(engine, tmp_path)
    out = effects.abandon(engine, "proj-a", "1.1.0", "superseded by 1.2.0", confirm=True)
    assert out["outcome"] == "abandoned" and set(out) == set(effects.RESULT_KEYS)
    assert get_release(engine, "proj-a") is None, "nothing left to resume"
    record = get_release(engine, "proj-a", "1.1.0")
    assert record["status"] == "abandoned"
    assert record["abandon_reason"] == "superseded by 1.2.0"
    assert record["candidate_sha"] == CANDIDATE and record["entry_ids"] == [eid]
    assert get_project(engine, "proj-a")["current_version"] == "1.1.0"
    stamped = [e["id"] for e in list_entries(engine, "proj-a") if e.get("version") == "1.1.0"]
    assert stamped == [eid]
    # A repeat is a no-op, never a second transition; a DIFFERENT reason is
    # refused with a clear message (K-94 B5).
    again = effects.abandon(engine, "proj-a", "1.1.0", "superseded by 1.2.0", confirm=True)
    assert again["outcome"] == "noop"
    with pytest.raises(effects.ReleaseRefused, match="first reason stays"):
        effects.abandon(engine, "proj-a", "1.1.0", "again", confirm=True)
    assert get_release(engine, "proj-a", "1.1.0")["abandon_reason"] == "superseded by 1.2.0"
