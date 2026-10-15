"""~/.claude/dct-reference.md across package moves, races and foreign files.

Contract (K-57, K-58, K-66, K-73, K-76 of the #917 triage):
- Ownership of an installed reference LINK is proven, never guessed: either
  it resolves into the RUNNING package's assets dir, or dct recorded its
  exact target when it created it (a record kept beside the link, so it
  outlives the package location). A dangling link or a path that merely
  looks like ``*/dct/assets/*`` proves nothing — it is preserved.
- A recorded link re-points after the package moves (new venv / Python,
  editable <-> wheel), also when the old location is gone.
- A file or link that appears between the ownership check and the creation
  is never overwritten (no fallback copy over it, no write through it).
- Every report distinguishes missing / outdated / not dct's, names the path
  (and the link target), and offers a remedy that changes the state: a
  foreign file needs moving aside first; never the interactive `dct init`.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from dct import assets, cli
from dct.integrations import base, claude, manager

REPO_ASSETS = assets.assets_dir()


def _fake_package(root: Path) -> Path:
    """A copy of the packaged assets dir at another install location."""
    target = root / "dct" / "assets"
    target.mkdir(parents=True)
    for name in ("dct-reference.md", "claude-md-snippet.md"):
        shutil.copy2(REPO_ASSETS / name, target / name)
    return target


def _run_from(monkeypatch, assets_dir: Path) -> None:
    """Make the running package live at ``assets_dir``."""
    monkeypatch.setattr(assets, "assets_dir", lambda: assets_dir)


@pytest.fixture
def ref(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".claude" / "dct-reference.md"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_REFERENCE", path)
    return path


def _row(rows):
    return next(r for r in rows if r["check"] == "reference")


# ── K-57: provenance survives a package move ─────────────────────────────────


@pytest.mark.parametrize("old, new", [
    ("venv/lib/python3.13/site-packages", "venv/lib/python3.14/site-packages"),
    ("checkout", "venv/lib/python3.14/site-packages"),   # editable -> wheel
    ("venv/lib/python3.14/site-packages", "checkout"),   # wheel -> editable
])
@pytest.mark.parametrize("old_location_removed", [False, True])
def test_link_dct_created_follows_the_package_to_its_new_location(
        ref, tmp_path, monkeypatch, old, new, old_location_removed):
    old_assets, new_assets = _fake_package(tmp_path / old), _fake_package(tmp_path / new)
    _run_from(monkeypatch, old_assets)
    assert claude.install_reference()["status"] == "installed"
    if old_location_removed:
        shutil.rmtree(old_assets)
    _run_from(monkeypatch, new_assets)

    result = claude.install_reference()

    assert result["status"] == "refreshed"
    assert os.readlink(ref) == str(new_assets / "dct-reference.md")
    assert _row(claude.ClaudeIntegration().doctor())["status"] == "ok"
    # the record now names the new target: a second move works again
    assert claude.install_reference()["status"] == "unchanged"


def test_known_old_link_to_the_snippet_is_migrated_to_the_full_guide(ref, monkeypatch):
    """K-76, the owner's real case: a hand-made link to
    <package assets>/claude-md-snippet.md — repurposed as the 3-line pointer
    that names this very path — must become the full guide on upgrade."""
    ref.symlink_to(assets.claude_snippet_path())
    monkeypatch.setattr(claude.ClaudeIntegration, "detect", lambda self: True)
    monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
    monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
    monkeypatch.setattr(claude, "prune_dangling_symlinks", list)
    monkeypatch.setattr(manager, "get_integration",
                        lambda name: claude.ClaudeIntegration() if name == "claude" else _Undetected())

    result = manager.refresh_integrations()[0]

    read = ref.read_text(encoding="utf-8")
    assert read == assets.dct_reference_path().read_text(encoding="utf-8")
    assert "### " in read  # the guide's sections, not the pointer
    assert result["reference"]["status"] == "refreshed"
    assert _row(result["checks"])["status"] == "ok"


@pytest.mark.parametrize("dangling", [False, True])
def test_foreign_link_that_only_looks_like_a_dct_install_is_preserved(ref, tmp_path, dangling):
    other = tmp_path / "other-tool" / "dct" / "assets" / "dct-reference.md"
    if not dangling:
        other.parent.mkdir(parents=True)
        other.write_text("their guide\n", encoding="utf-8")
    ref.symlink_to(other)

    result = claude.install_reference()

    assert result["status"] == "preserved"
    assert os.readlink(ref) == str(other)
    assert dangling or other.read_text(encoding="utf-8") == "their guide\n"
    assert str(other) in result["message"]


def test_link_retargeted_by_the_user_after_install_is_preserved(ref, tmp_path):
    claude.install_reference()
    mine = tmp_path / "my-reference.md"
    mine.write_text("mine\n", encoding="utf-8")
    ref.unlink()
    ref.symlink_to(mine)

    assert claude.install_reference()["status"] == "preserved"
    assert os.readlink(ref) == str(mine)


def test_record_is_dropped_when_an_owned_link_is_uninstalled(ref, tmp_path, monkeypatch):
    old_assets = _fake_package(tmp_path / "old")
    _run_from(monkeypatch, old_assets)
    claude.install_reference()
    claude.ClaudeIntegration().uninstall()
    assert not ref.is_symlink()
    # the user later links the same old target by hand: no longer dct's
    ref.symlink_to(old_assets / "dct-reference.md")
    _run_from(monkeypatch, _fake_package(tmp_path / "new"))

    assert claude.install_reference()["status"] == "preserved"


def test_corrupt_record_proves_nothing(ref, tmp_path, monkeypatch):
    old_assets = _fake_package(tmp_path / "old")
    _run_from(monkeypatch, old_assets)
    claude.install_reference()
    (ref.parent / base.LINK_RECORD).write_text("{not json", encoding="utf-8")
    _run_from(monkeypatch, _fake_package(tmp_path / "new"))

    assert claude.install_reference()["status"] == "preserved"
    assert os.readlink(ref) == str(old_assets / "dct-reference.md")


# ── K-58: no overwrite when something appears after the check ────────────────

_REAL_SYMLINK_TO = Path.symlink_to


def _intrude(dst: Path, kind: str, victim: Path) -> None:
    if kind == "file":
        dst.write_text("appeared meanwhile\n", encoding="utf-8")
    else:
        os.symlink(victim, dst)


@pytest.fixture
def victim(tmp_path):
    path = tmp_path / "victim.md"
    path.write_text("victim content\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("kind", ["file", "symlink"])
@pytest.mark.parametrize("symlinks_available", [True, False])
def test_reference_appearing_mid_install_is_never_overwritten(
        ref, monkeypatch, victim, kind, symlinks_available):
    def racing(self, target, *args, **kwargs):
        if self != ref:
            return _REAL_SYMLINK_TO(self, target, *args, **kwargs)
        _intrude(self, kind, victim)
        if symlinks_available:
            return _REAL_SYMLINK_TO(self, target, *args, **kwargs)
        raise OSError(1, "symlinks unavailable")
    monkeypatch.setattr(Path, "symlink_to", racing)

    result = claude.install_reference()

    assert result["status"] == "preserved"
    assert victim.read_text(encoding="utf-8") == "victim content\n"
    if kind == "file":
        assert ref.read_text(encoding="utf-8") == "appeared meanwhile\n"
    else:
        assert os.readlink(ref) == str(victim)
    assert "appeared" in result["message"] or "created" in result["message"]


@pytest.mark.parametrize("kind", ["file", "symlink"])
@pytest.mark.parametrize("symlinks_available", [True, False])
def test_hook_appearing_mid_install_is_never_overwritten(
        tmp_path, monkeypatch, victim, kind, symlinks_available):
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(claude, "CLAUDE_HOOKS_DIR", hooks)
    raced = hooks / "dct-check-changelog.sh"

    def racing(self, target, *args, **kwargs):
        if self == raced:
            _intrude(self, kind, victim)
        if symlinks_available:
            return _REAL_SYMLINK_TO(self, target, *args, **kwargs)
        raise OSError(1, "symlinks unavailable")
    monkeypatch.setattr(Path, "symlink_to", racing)

    claude.install_hook()

    assert victim.read_text(encoding="utf-8") == "victim content\n"
    if kind == "file":
        assert raced.read_text(encoding="utf-8") == "appeared meanwhile\n"
    else:
        assert os.readlink(raced) == str(victim)
    # the other hooks still installed
    assert (hooks / "dct-plan-autoscan.sh").read_bytes() == (claude.HOOKS_DIR / "dct-plan-autoscan.sh").read_bytes()


@pytest.mark.parametrize("kind", ["dir", "symlink"])
@pytest.mark.parametrize("symlinks_available", [True, False])
def test_skill_appearing_mid_install_is_never_overwritten(
        tmp_path, monkeypatch, kind, symlinks_available):
    skills = tmp_path / "skills"
    monkeypatch.setattr(claude, "CLAUDE_SKILLS_DIR", skills)
    raced = skills / "track"
    theirs = tmp_path / "their-skill"
    theirs.mkdir()
    (theirs / "SKILL.md").write_text("theirs\n", encoding="utf-8")

    def racing(self, target, *args, **kwargs):
        if self == raced:
            if kind == "dir":
                self.mkdir()
                (self / "SKILL.md").write_text("appeared meanwhile\n", encoding="utf-8")
            else:
                os.symlink(theirs, self)
        if symlinks_available:
            return _REAL_SYMLINK_TO(self, target, *args, **kwargs)
        raise OSError(1, "symlinks unavailable")
    monkeypatch.setattr(Path, "symlink_to", racing)

    claude.install_skills()

    assert (theirs / "SKILL.md").read_text(encoding="utf-8") == "theirs\n"
    if kind == "dir":
        assert (raced / "SKILL.md").read_text(encoding="utf-8") == "appeared meanwhile\n"
        assert not (raced / base.COPY_MARKER).exists()
    else:
        assert os.readlink(raced) == str(theirs)


def test_copy_fallback_io_error_is_not_reported_as_installed(ref, monkeypatch):
    def unavailable(self, *args, **kwargs):
        raise OSError(1, "symlinks unavailable")
    monkeypatch.setattr(Path, "symlink_to", unavailable)
    ref.parent.chmod(0o500)
    try:
        with pytest.raises(PermissionError):
            claude.install_reference()
    finally:
        ref.parent.chmod(0o700)
    assert not ref.exists()


# ── K-66 / K-73: each state is reported with a remedy that changes it ────────


def test_doctor_distinguishes_missing_outdated_and_foreign(ref, tmp_path, monkeypatch):
    assert _row(claude.ClaudeIntegration().doctor())["state"] == "missing"

    old_assets = _fake_package(tmp_path / "old")
    _run_from(monkeypatch, old_assets)
    claude.install_reference()
    _run_from(monkeypatch, _fake_package(tmp_path / "new"))
    outdated = _row(claude.ClaudeIntegration().doctor())
    assert (outdated["state"], outdated["status"]) == ("outdated", "error")
    assert str(old_assets / "dct-reference.md") in outdated["message"]

    ref.unlink()
    ref.write_text("mine\n", encoding="utf-8")
    foreign = _row(claude.ClaudeIntegration().doctor())
    assert (foreign["state"], foreign["status"]) == ("foreign", "error")


def test_missing_reference_offers_the_full_repair_with_its_scope(ref):
    row = _row(claude.ClaudeIntegration().doctor())

    assert str(ref) in row["message"]
    assert "dct integrate repair claude" in row["remedy"]
    for part in ("MCP", "skills", "hooks", "CLAUDE.md", "reference"):
        assert part in row["remedy"]
    assert "dct init" not in row["remedy"] + row["message"]
    assert "dct upgrade" not in row["remedy"]


@pytest.mark.parametrize("kind", ["file", "live-link", "dangling-link"])
def test_foreign_reference_names_the_file_and_asks_to_move_it_first(ref, tmp_path, kind):
    target = tmp_path / "elsewhere.md"
    if kind == "file":
        ref.write_text("mine\n", encoding="utf-8")
    else:
        if kind == "live-link":
            target.write_text("x", encoding="utf-8")
        ref.symlink_to(target)

    installed = claude.install_reference()
    row = _row(claude.ClaudeIntegration().doctor())

    for text in (installed["message"] + installed["remedy"], row["message"] + row["remedy"]):
        assert str(ref) in text
        assert "dct init" not in text
        assert "move" in text
        if kind != "file":
            assert str(target) in text
    # repair alone would preserve the same file again: never the bare advice
    assert not row["remedy"].startswith("Run `dct integrate repair")


def test_upgrade_output_carries_the_cause_and_no_looping_remedy(ref, monkeypatch, capsys):
    from dct import update

    ref.write_text("mine\n", encoding="utf-8")
    monkeypatch.setattr(update, "detect_install_method", lambda: "editable")
    monkeypatch.setattr(claude.ClaudeIntegration, "detect", lambda self: True)
    monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
    monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
    monkeypatch.setattr(claude, "prune_dangling_symlinks", list)
    monkeypatch.setattr(claude.ClaudeIntegration, "doctor", lambda self: [
        claude.reference_row()])
    monkeypatch.setattr(manager, "get_integration",
                        lambda name: claude.ClaudeIntegration() if name == "claude" else _Undetected())

    cli.cmd_upgrade(cli.build_parser().parse_args(["upgrade"]))

    err = capsys.readouterr().err
    assert str(ref) in err
    assert "move" in err
    assert "dct init" not in err
    assert "Run `dct integrate repair claude`" not in err


def test_repair_output_shows_the_remedy(ref, monkeypatch, capsys):
    ref.write_text("mine\n", encoding="utf-8")
    monkeypatch.setattr(claude.ClaudeIntegration, "install_mcp", lambda self: {"status": "ok"})
    monkeypatch.setattr(claude, "install_skills", lambda config=None: True)
    monkeypatch.setattr(claude, "install_hook", lambda config=None: True)
    monkeypatch.setattr(claude, "wire_hooks_into_settings", lambda: {"added": []})
    monkeypatch.setattr(claude.ClaudeIntegration, "install_instructions", lambda self: {"status": "unchanged"})

    with pytest.raises(SystemExit):
        cli.cmd_integrate(cli.build_parser().parse_args(["integrate", "repair", "claude"]))

    out = capsys.readouterr().out
    assert "reference" in out
    assert "move" in out
    assert "dct init" not in out


def test_doctor_json_row_carries_state_and_remedy(ref):
    row = claude.reference_row()
    json.dumps(row)
    assert {"check", "status", "state", "message", "remedy"} <= set(row)


class _Undetected:
    def detect(self):
        return False


# ── K-78: the foreign remedy names the file but never hands out a command ────


def _foreign_ref(tmp_path, monkeypatch, home_name: str, link: bool) -> tuple[Path, Path]:
    path = tmp_path / home_name / ".claude" / "dct-reference.md"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_REFERENCE", path)
    target = tmp_path / home_name / "my notes.md"
    target.write_text("mine\n", encoding="utf-8")
    if link:
        path.symlink_to(target)
    else:
        path.write_text("mine\n", encoding="utf-8")
    return path, target


@pytest.mark.parametrize("link", [False, True], ids=["file", "link"])
@pytest.mark.parametrize("home_name, earlier_backup", [
    ("home with space", False),
    # A `"` is invalid in a Windows file name: the double-quote case lives in
    # test_foreign_remedy_keeps_a_double_quote_in_the_path_as_text (K-82).
    ("home'with apostrophe", False),
    ("home", True),
], ids=["space", "apostrophe", "existing-mine"])
def test_foreign_remedy_names_the_file_without_a_ready_made_command(
        tmp_path, monkeypatch, home_name, earlier_backup, link):
    """A copy-pasted `mv path path.mine` broke on a space or quote and
    overwrote an earlier `.mine` without a copy: the remedy names the file
    (and the link target), asks the user to move it to an unused name or
    replace it after keeping what they need, and only then to repair."""
    path, target = _foreign_ref(tmp_path, monkeypatch, home_name, link)
    if earlier_backup:
        backup = path.with_name(path.name + ".mine")
        backup.write_text("earlier\n", encoding="utf-8")

    for remedy in (claude.reference_state()["remedy"], claude.install_reference()["remedy"]):
        _assert_names_the_file_without_a_command(remedy, path, target if link else None)
    if earlier_backup:
        assert backup.read_text(encoding="utf-8") == "earlier\n"


def _assert_names_the_file_without_a_command(remedy: str, path, target) -> None:
    before, sep, after = remedy.partition("After that, ")
    assert sep and after == claude.REPAIR_REMEDY  # move/replace first, then repair
    assert str(path) in before
    if target is not None:
        assert str(target) in before
    assert "move" in before and "unused name" in before
    assert "replace" in before
    # no shell command, no fixed backup name, no deletion advice
    assert "`" not in before
    assert "mv " not in before and "rm " not in before
    assert ".mine" not in before and ".bak" not in before
    assert "delete" not in before.lower()


@pytest.mark.parametrize("link", [False, True], ids=["file", "link"])
def test_foreign_remedy_keeps_a_double_quote_in_the_path_as_text(tmp_path, monkeypatch, link):
    """A `"` in the path or link target is rendered verbatim, with no command
    around it. `"` is invalid in a Windows file name (#917 K-82), so this case
    never creates such a path: only the probes for this one path are stubbed."""
    path = tmp_path / 'home"with"quotes' / ".claude" / "dct-reference.md"
    target = str(tmp_path / 'my "notes".md') if link else None
    is_symlink, exists = Path.is_symlink, Path.exists
    monkeypatch.setattr(Path, "is_symlink",
                        lambda self: link if self == path else is_symlink(self))
    monkeypatch.setattr(Path, "exists",
                        lambda self, *a, **k: True if self == path else exists(self, *a, **k))
    readlink = claude.os.readlink
    monkeypatch.setattr(claude.os, "readlink",
                        lambda p, *a, **k: target if Path(p) == path else readlink(p, *a, **k))
    monkeypatch.setattr(claude, "_owned_reference", lambda p: False)

    state = claude.reference_state(path)
    assert state["state"] == "foreign"
    assert not (tmp_path / 'home"with"quotes').exists()  # nothing created on disk
    _assert_names_the_file_without_a_command(state["remedy"], path, target)
