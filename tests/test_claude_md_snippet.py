"""Tests for attach_claude_md_snippet — the `dct init` CLAUDE.md attach (#443).

Contract (dct/integrations/claude.py::attach_claude_md_snippet):
- Creates the target file (managed block only) when missing or empty.
- Appends the block after existing user content, never modifying it.
- Idempotent: a second attach with identical content writes nothing.
- Refresh: stale content between the markers is replaced in place; user
  content above and below the markers survives byte-for-byte.
- Backs up the original before the first write (only when it had content).
- Malformed markers (start without end, stray end) abort with ValueError
  and no write.
- The packaged snippet's human preamble (everything above the first `---`)
  is never attached — only the instruction block below it.
"""

import pytest

from dct import assets
from dct.integrations.claude import (
    SNIPPET_END,
    SNIPPET_START,
    attach_claude_md_snippet,
)

USER_HEADER = "# My global rules\n\n- always be terse\n"
USER_FOOTER = "\n## After dct\n\nkeep this line\n"


def target(tmp_path):
    return tmp_path / "CLAUDE.md"


class TestPackagedSnippetContract:
    def test_snippet_ships_in_the_package(self):
        path = assets.claude_snippet_path()
        assert path.is_file()

    def test_snippet_has_the_preamble_separator(self):
        text = assets.claude_snippet_path().read_text(encoding="utf-8")
        assert "\n---\n" in text


class TestFreshAttach:
    def test_creates_missing_file_with_managed_block(self, tmp_path):
        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "added"
        assert result["backup"] is None
        text = target(tmp_path).read_text(encoding="utf-8")
        assert text.startswith(SNIPPET_START)
        assert text.rstrip("\n").endswith(SNIPPET_END)
        assert "Cross-project tracking (dct)" in text

    def test_preamble_is_not_attached(self, tmp_path):
        attach_claude_md_snippet(claude_md_path=target(tmp_path))

        text = target(tmp_path).read_text(encoding="utf-8")
        assert "paste the section below" not in text

    def test_empty_file_treated_as_fresh(self, tmp_path):
        target(tmp_path).write_text("  \n", encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "added"
        assert result["backup"] is None


class TestAppendToExisting:
    def test_user_content_preserved_and_backed_up(self, tmp_path):
        target(tmp_path).write_text(USER_HEADER, encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "added"
        assert result["backup"] is not None
        text = target(tmp_path).read_text(encoding="utf-8")
        assert text.startswith(USER_HEADER.rstrip("\n"))
        assert SNIPPET_START in text
        backups = list(tmp_path.glob("CLAUDE.md.bak-dct-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == USER_HEADER


class TestIdempotency:
    def test_second_attach_is_a_noop(self, tmp_path):
        attach_claude_md_snippet(claude_md_path=target(tmp_path))
        before = target(tmp_path).read_text(encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "unchanged"
        assert result["backup"] is None
        assert target(tmp_path).read_text(encoding="utf-8") == before

    def test_stale_block_is_refreshed_in_place(self, tmp_path):
        stale = f"{USER_HEADER}\n{SNIPPET_START}\nOLD CONTENT\n{SNIPPET_END}{USER_FOOTER}"
        target(tmp_path).write_text(stale, encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "refreshed"
        text = target(tmp_path).read_text(encoding="utf-8")
        assert "OLD CONTENT" not in text
        assert "Cross-project tracking (dct)" in text
        assert text.startswith(USER_HEADER.rstrip("\n") + "\n")
        assert text.endswith(USER_FOOTER)
        # exactly one managed block — the refresh must not duplicate it
        assert text.count(SNIPPET_START) == 1
        assert text.count(SNIPPET_END) == 1


class TestMalformedMarkers:
    def test_start_without_end_aborts_without_write(self, tmp_path):
        broken = f"{USER_HEADER}\n{SNIPPET_START}\ntruncated"
        target(tmp_path).write_text(broken, encoding="utf-8")

        with pytest.raises(ValueError, match="start marker without end"):
            attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert target(tmp_path).read_text(encoding="utf-8") == broken

    def test_stray_end_marker_aborts_without_write(self, tmp_path):
        broken = f"{USER_HEADER}\n{SNIPPET_END}\n"
        target(tmp_path).write_text(broken, encoding="utf-8")

        with pytest.raises(ValueError, match="end marker without start"):
            attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert target(tmp_path).read_text(encoding="utf-8") == broken
