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

K-36: the attached block is a SHORT pointer naming ~/.claude/dct-reference.md
(no `@` import); the full reference is its own packaged file that the
installer links there (dct/integrations/claude.py::install_reference). A
block still holding the old full body refreshes to the pointer. The
reference's ownership is proven: a symlink into the package's assets, a link
whose target dct recorded, or an unchanged marked copy is dct's; anything
else is the user's, preserved and reported (package moves and install races:
tests/test_reference_install.py).
"""

import builtins
import os
import re
from pathlib import Path

import pytest

from dct import assets, cli
from dct.integrations import claude
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
        """Inside the markers is exactly the packaged pointer below its FIRST
        `---` — nothing of any preamble, one newline at each boundary."""
        _assert_only_the_pointer_is_attached(assets.claude_snippet_path(), tmp_path)

    @pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
    def test_preamble_is_not_attached_for_either_line_ending(self, tmp_path, monkeypatch,
                                                             newline):
        """K-80: a CRLF checkout of the packaged pointer (core.autocrlf)
        attaches the same block as an LF one — the source's line endings are
        normalized as the attach normalizes them, the boundaries stay exact."""
        packaged = assets.claude_snippet_path().read_bytes().decode("utf-8")
        lines = packaged.replace("\r\n", "\n").split("\n")
        variant = tmp_path / "assets" / "claude-md-snippet.md"
        variant.parent.mkdir()
        variant.write_bytes(newline.join(lines).encode("utf-8"))
        monkeypatch.setattr(assets, "claude_snippet_path", lambda: variant)

        _assert_only_the_pointer_is_attached(variant, tmp_path)

    def test_empty_file_treated_as_fresh(self, tmp_path):
        target(tmp_path).write_text("  \n", encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "added"
        assert result["backup"] is None


def _assert_only_the_pointer_is_attached(source, tmp_path):
    # Normalize the SOURCE's line endings only, as the attach's text-mode
    # read does; the target is compared byte for byte.
    raw = source.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    preamble, separator, pointer = raw.partition("\n---\n")
    assert separator and preamble.strip() and pointer.strip()

    attach_claude_md_snippet(claude_md_path=target(tmp_path))

    text = target(tmp_path).read_bytes().decode("utf-8")
    assert _attached_block(text) == "\n" + pointer.strip("\n") + "\n"


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


# ── K-36: short pointer + reference read on demand ──────────────────────────


def _attached_block(text: str) -> str:
    return text[text.index(SNIPPET_START) + len(SNIPPET_START):text.index(SNIPPET_END)]


def _old_full_body() -> str:
    """The pre-K-36 attached body: today's reference below its separator —
    byte-for-byte what the old snippet carried below its own `---`."""
    return assets.dct_reference_path().read_text(encoding="utf-8").split("\n---\n", 1)[1].strip("\n")


def _named_reference_path(block: str) -> str:
    named = re.findall(r"`(~/[^`]*dct-reference\.md)`", block)
    assert len(named) == 1, f"the block must name exactly one reference path: {named}"
    return named[0]


class TestPointerBlock:
    def test_attached_block_is_short_and_names_the_reference(self, tmp_path):
        attach_claude_md_snippet(claude_md_path=target(tmp_path))

        block = _attached_block(target(tmp_path).read_text(encoding="utf-8"))
        assert "## Cross-project tracking (dct)" in block
        assert _named_reference_path(block) == "~/.claude/dct-reference.md"
        # the pointer, not the guide: no section of the reference rides along
        assert "### " not in block
        assert len(block.strip().splitlines()) <= 6

    def test_attached_block_carries_no_import_marker(self, tmp_path):
        """`@path` makes Claude Code load the whole file every session and is
        plain text to Codex — the pointer must never use it."""
        attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert "@" not in _attached_block(target(tmp_path).read_text(encoding="utf-8"))

    def test_old_full_block_refreshes_to_the_pointer(self, tmp_path):
        old = f"{USER_HEADER}\n{SNIPPET_START}\n{_old_full_body()}\n{SNIPPET_END}{USER_FOOTER}"
        target(tmp_path).write_text(old, encoding="utf-8")

        result = attach_claude_md_snippet(claude_md_path=target(tmp_path))

        assert result["status"] == "refreshed"
        assert result["backup"] is not None
        assert Path(result["backup"]).read_text(encoding="utf-8") == old
        text = target(tmp_path).read_text(encoding="utf-8")
        before, _, rest = text.partition(SNIPPET_START)
        _, _, after = rest.partition(SNIPPET_END)
        assert before == f"{USER_HEADER}\n"
        assert after == USER_FOOTER
        assert "### Item type triggers" not in text
        assert "~/.claude/dct-reference.md" in text

        second = attach_claude_md_snippet(claude_md_path=target(tmp_path))
        assert second["status"] == "unchanged"
        assert target(tmp_path).read_text(encoding="utf-8") == text

    def test_target_without_markers_gets_the_pointer_appended(self, tmp_path):
        target(tmp_path).write_text(USER_HEADER, encoding="utf-8")

        attach_claude_md_snippet(claude_md_path=target(tmp_path))

        text = target(tmp_path).read_text(encoding="utf-8")
        assert text.startswith(USER_HEADER)
        assert "~/.claude/dct-reference.md" in _attached_block(text)


class TestPackagedReference:
    def test_reference_is_the_old_body_under_a_read_on_demand_header(self):
        text = assets.dct_reference_path().read_text(encoding="utf-8")
        header, _, body = text.partition("\n---\n")
        assert body.lstrip("\n").startswith("## Cross-project tracking (dct)")
        assert "read on demand" in header
        assert "Do not paste this file into `CLAUDE.md`" in header
        assert "paste the section below" not in text

    def test_block_path_is_the_installer_target(self):
        """Round trip: the path the attached block tells the model to read is
        exactly where the installer puts the reference."""
        body = assets.claude_snippet_path().read_text(encoding="utf-8").split("\n---\n", 1)[1]
        named = _named_reference_path(body)
        assert Path(named).expanduser() == claude.CLAUDE_REFERENCE


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake ~/.claude: every Claude path the adapter touches lives in tmp."""
    root = tmp_path / "home" / ".claude"
    root.mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_REFERENCE", root / "dct-reference.md")
    monkeypatch.setattr(claude, "CLAUDE_MD", root / "CLAUDE.md")
    monkeypatch.setattr(claude, "CLAUDE_SETTINGS", root / "settings.json")
    monkeypatch.setattr(claude, "CLAUDE_SKILLS_DIR", root / "skills")
    monkeypatch.setattr(claude, "CLAUDE_HOOKS_DIR", root / "hooks")
    monkeypatch.setattr(claude, "CLAUDE_JSON", tmp_path / "home" / ".claude.json")
    return root


def _reference_row(rows):
    return next(r for r in rows if r["check"] == "reference")


def _symlinks_unavailable(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise OSError("symlinks unavailable")
    monkeypatch.setattr(Path, "symlink_to", refuse)


class TestInstallReference:
    def test_fresh_install_links_the_packaged_reference(self, home):
        result = claude.install_reference()

        dst = claude.CLAUDE_REFERENCE
        assert result["status"] == "installed"
        assert dst.is_symlink()
        assert dst.resolve() == assets.dct_reference_path().resolve()

    def test_second_install_is_unchanged(self, home):
        claude.install_reference()

        assert claude.install_reference()["status"] == "unchanged"

    def test_owned_link_to_the_old_snippet_is_repointed(self, home):
        dst = claude.CLAUDE_REFERENCE
        dst.symlink_to(assets.claude_snippet_path())

        result = claude.install_reference()

        assert result["status"] == "refreshed"
        assert dst.resolve() == assets.dct_reference_path().resolve()

    def test_dangling_link_into_the_package_is_owned(self, home):
        dst = claude.CLAUDE_REFERENCE
        dst.symlink_to(assets.assets_dir() / "removed-in-an-upgrade.md")

        claude.install_reference()

        assert dst.resolve() == assets.dct_reference_path().resolve()

    def test_users_own_file_is_preserved_and_reported(self, home):
        dst = claude.CLAUDE_REFERENCE
        dst.write_text("my own notes\n", encoding="utf-8")

        result = claude.install_reference()

        assert result["status"] == "preserved"
        assert str(dst) in result["message"]
        assert not dst.is_symlink()
        assert dst.read_text(encoding="utf-8") == "my own notes\n"

    def test_foreign_symlink_is_preserved(self, home, tmp_path):
        mine = tmp_path / "elsewhere.md"
        mine.write_text("x", encoding="utf-8")
        dst = claude.CLAUDE_REFERENCE
        dst.symlink_to(mine)

        result = claude.install_reference()

        assert result["status"] == "preserved"
        assert os.readlink(dst) == str(mine)
        assert mine.read_text(encoding="utf-8") == "x"

    def test_reference_with_identical_bytes_but_no_marker_is_not_owned(self, home):
        """Ownership is proven by the link or the marker, never inferred from
        familiar content (the uninstaller relies on the same proof)."""
        dst = claude.CLAUDE_REFERENCE
        dst.write_bytes(assets.dct_reference_path().read_bytes())

        assert claude.install_reference()["status"] == "preserved"

    def test_copy_fallback_is_marked_and_refreshable(self, home, monkeypatch):
        _symlinks_unavailable(monkeypatch)

        first = claude.install_reference()

        dst = claude.CLAUDE_REFERENCE
        assert first["status"] == "installed"
        assert first["mode"] == "copy"
        assert not dst.is_symlink()
        assert assets.dct_reference_path().read_text(encoding="utf-8") in dst.read_text(encoding="utf-8")
        assert claude.install_reference()["status"] == "unchanged"
        assert _reference_row(claude.ClaudeIntegration().doctor())["status"] == "ok"

    def test_outdated_marked_copy_is_refreshed(self, home, monkeypatch):
        _symlinks_unavailable(monkeypatch)
        dst = claude.CLAUDE_REFERENCE
        claude._write_marked_copy(assets.claude_snippet_path(), dst)  # an older package's copy

        result = claude.install_reference()

        assert result["status"] == "refreshed"
        assert assets.dct_reference_path().read_text(encoding="utf-8") in dst.read_text(encoding="utf-8")

    def test_edited_marked_copy_is_preserved(self, home, monkeypatch):
        _symlinks_unavailable(monkeypatch)
        claude.install_reference()
        dst = claude.CLAUDE_REFERENCE
        dst.write_text(dst.read_text(encoding="utf-8") + "my edit\n", encoding="utf-8")

        result = claude.install_reference()

        assert result["status"] == "preserved"
        assert dst.read_text(encoding="utf-8").endswith("my edit\n")

    def test_creates_a_missing_claude_dir(self, tmp_path, monkeypatch):
        dst = tmp_path / "fresh-home" / ".claude" / "dct-reference.md"
        monkeypatch.setattr(claude, "CLAUDE_REFERENCE", dst)

        assert claude.install_reference()["status"] == "installed"
        assert dst.resolve() == assets.dct_reference_path().resolve()


class TestReferenceLifecycle:
    def test_doctor_ok_after_install(self, home):
        claude.install_reference()

        assert _reference_row(claude.ClaudeIntegration().doctor())["status"] == "ok"

    @pytest.mark.parametrize("state", ["missing", "user-file", "old-snippet-link"])
    def test_doctor_error_when_not_the_packaged_reference(self, home, state):
        dst = claude.CLAUDE_REFERENCE
        if state == "user-file":
            dst.write_text("mine", encoding="utf-8")
        elif state == "old-snippet-link":
            dst.symlink_to(assets.claude_snippet_path())

        row = _reference_row(claude.ClaudeIntegration().doctor())

        assert row["status"] == "error"
        assert str(dst) in row["message"]

    def test_uninstall_removes_an_owned_link(self, home):
        claude.install_reference()

        result = claude.ClaudeIntegration().uninstall()

        assert not claude.CLAUDE_REFERENCE.is_symlink()
        assert not claude.CLAUDE_REFERENCE.exists()
        assert str(claude.CLAUDE_REFERENCE) in result["removed"]

    def test_uninstall_removes_an_owned_copy(self, home, monkeypatch):
        _symlinks_unavailable(monkeypatch)
        claude.install_reference()

        claude.ClaudeIntegration().uninstall()

        assert not claude.CLAUDE_REFERENCE.exists()

    def test_uninstall_keeps_a_users_file(self, home):
        claude.CLAUDE_REFERENCE.write_text("mine", encoding="utf-8")

        result = claude.ClaudeIntegration().uninstall()

        assert claude.CLAUDE_REFERENCE.read_text(encoding="utf-8") == "mine"
        assert str(claude.CLAUDE_REFERENCE) not in result["removed"]

    def test_uninstall_keeps_a_foreign_symlink(self, home, tmp_path):
        mine = tmp_path / "elsewhere.md"
        mine.write_text("x", encoding="utf-8")
        claude.CLAUDE_REFERENCE.symlink_to(mine)

        claude.ClaudeIntegration().uninstall()

        assert claude.CLAUDE_REFERENCE.is_symlink()
        assert mine.exists()

    def test_adapter_install_skills_installs_the_reference(self, home, monkeypatch):
        monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
        monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
        monkeypatch.setattr(claude, "wire_hooks_into_settings", lambda: {"added": []})

        result = claude.ClaudeIntegration().install_skills()

        assert result["reference"]["status"] == "installed"
        assert claude.CLAUDE_REFERENCE.resolve() == assets.dct_reference_path().resolve()

    def test_upgrade_refresh_repoints_the_reference(self, home, monkeypatch):
        """The update path (dct upgrade → refresh_integrations) re-points an
        owned link even when CLAUDE.md never opted into the block."""
        from dct.integrations import manager

        claude.CLAUDE_REFERENCE.symlink_to(assets.claude_snippet_path())
        monkeypatch.setattr(claude.ClaudeIntegration, "detect", lambda self: True)
        monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
        monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
        monkeypatch.setattr(claude, "prune_dangling_symlinks", list)
        monkeypatch.setattr(manager, "get_integration",
                            lambda name: claude.ClaudeIntegration() if name == "claude" else _Undetected())

        result = manager.refresh_integrations()[0]

        assert claude.CLAUDE_REFERENCE.resolve() == assets.dct_reference_path().resolve()
        assert _reference_row(result["checks"])["status"] == "ok"
        assert not claude.CLAUDE_MD.exists()


class _Undetected:
    def detect(self):
        return False


def test_init_installs_the_reference_when_the_attach_is_declined(home, tmp_path, monkeypatch):
    """`dct init --no-attach-snippet`: the CLAUDE.md block is the user's
    choice, the reference file is not — it is installed either way."""
    answers = iter(["", "n"])  # sqlite backend, no project registration
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(cli.paths, "default_db_path", lambda: tmp_path / "init.db")
    monkeypatch.setattr(cli, "save_config", lambda config, path: tmp_path / "dct.toml")
    monkeypatch.setattr(claude, "register_mcp", lambda config: (True, "stubbed"))
    monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
    monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
    args = cli.build_parser().parse_args(["init", "--no-wire-hooks", "--no-attach-snippet"])

    cli.cmd_init(args)

    assert claude.CLAUDE_REFERENCE.resolve() == assets.dct_reference_path().resolve()
    assert not claude.CLAUDE_MD.exists()
