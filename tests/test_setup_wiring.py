"""Tests for wire_hooks_into_settings — auto-wiring dct hooks into settings.json.

Contract (dct/setup.py::wire_hooks_into_settings):
- Creates settings.json (with all four dct hooks) when the file is missing.
- Merges into existing settings without touching user-defined hooks.
- Idempotent: a hook already wired ANYWHERE (any event, any matcher, any
  path form) is detected by script basename and skipped — re-runs are no-ops.
- Backs up the original file before the first write (only when it existed).
- Malformed JSON or unexpected structure aborts with ValueError, no write.
"""

import json

import pytest

from dct.integrations.claude import HOOK_WIRING, wire_hooks_into_settings

ALL_HOOK_NAMES = [spec["script"] for spec in HOOK_WIRING]


def read(path):
    return json.loads(path.read_text())


def hook_commands(settings):
    """Flatten all hook command strings across all events and matcher groups."""
    out = []
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                out.append(hook.get("command", ""))
    return out


class TestWireHooksFresh:
    def test_creates_settings_when_missing(self, tmp_path):
        settings_path = tmp_path / "settings.json"

        result = wire_hooks_into_settings(settings_path=settings_path)

        assert settings_path.exists()
        assert sorted(result["added"]) == sorted(ALL_HOOK_NAMES)
        assert result["skipped"] == []
        assert result["backup"] is None  # nothing to back up

        settings = read(settings_path)
        commands = hook_commands(settings)
        for name in ALL_HOOK_NAMES:
            assert any(name in cmd for cmd in commands), f"{name} not wired"

    def test_wires_canonical_events_and_matchers(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        wire_hooks_into_settings(settings_path=settings_path)
        settings = read(settings_path)

        by_event = settings["hooks"]
        # changelog guard on PreToolUse/Bash
        bash_groups = [g for g in by_event["PreToolUse"] if g.get("matcher") == "Bash"]
        assert len(bash_groups) == 1
        assert any("dct-check-changelog.sh" in h["command"] for h in bash_groups[0]["hooks"])
        # task mirror on PreToolUse/TodoWrite|TaskCreate (per hook header, NOT PostToolUse)
        tm_groups = [g for g in by_event["PreToolUse"] if g.get("matcher") == "TodoWrite|TaskCreate"]
        assert len(tm_groups) == 1
        # stop reminder on Stop (no matcher key needed)
        assert any(
            "dct-check-changelog-on-stop.sh" in h["command"]
            for g in by_event["Stop"] for h in g["hooks"]
        )
        # plan autoscan on PostToolUse/Edit|Write|MultiEdit
        pa_groups = [g for g in by_event["PostToolUse"] if g.get("matcher") == "Edit|Write|MultiEdit"]
        assert len(pa_groups) == 1

    def test_empty_file_treated_as_empty_settings(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("")

        result = wire_hooks_into_settings(settings_path=settings_path)

        assert sorted(result["added"]) == sorted(ALL_HOOK_NAMES)
        assert read(settings_path)["hooks"]


class TestWireHooksMerge:
    def test_preserves_existing_user_hooks(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        original = {
            "permissions": {"allow": ["Bash(ls*)"]},
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "/usr/local/bin/my-guard.sh"},
                    ]},
                ],
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "echo hi"}]},
                ],
            },
        }
        settings_path.write_text(json.dumps(original))

        wire_hooks_into_settings(settings_path=settings_path)
        settings = read(settings_path)

        # user's unrelated top-level keys survive
        assert settings["permissions"] == {"allow": ["Bash(ls*)"]}
        # user's custom hook still present, in the same matcher group
        bash_groups = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"]
        assert len(bash_groups) == 1
        commands = [h["command"] for h in bash_groups[0]["hooks"]]
        assert "/usr/local/bin/my-guard.sh" in commands
        assert any("dct-check-changelog.sh" in c for c in commands)
        # user's custom event untouched
        assert settings["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]

    def test_idempotent_rerun_is_noop(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        wire_hooks_into_settings(settings_path=settings_path)
        first_content = settings_path.read_text()

        result = wire_hooks_into_settings(settings_path=settings_path)

        assert result["added"] == []
        assert sorted(result["skipped"]) == sorted(ALL_HOOK_NAMES)
        assert settings_path.read_text() == first_content

    def test_respects_user_custom_placement(self, tmp_path):
        """A hook the user wired themselves (different event/path form) is
        detected by basename and NOT duplicated under the canonical event."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({
            "hooks": {
                "PostToolUse": [
                    {"matcher": "TaskCreate|TaskUpdate", "hooks": [
                        {"type": "command",
                         "command": "bash /opt/someone/.claude/hooks/dct-task-mirror.sh"},
                    ]},
                ],
            },
        }))

        result = wire_hooks_into_settings(settings_path=settings_path)

        assert "dct-task-mirror.sh" in result["skipped"]
        settings = read(settings_path)
        mirror_mentions = [c for c in hook_commands(settings) if "dct-task-mirror.sh" in c]
        assert len(mirror_mentions) == 1  # no duplicate under PreToolUse


class TestWireHooksSafety:
    def test_malformed_json_aborts_without_write(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{not valid json")

        with pytest.raises(ValueError):
            wire_hooks_into_settings(settings_path=settings_path)

        assert settings_path.read_text() == "{not valid json"
        assert not list(tmp_path.glob("*.bak*"))

    def test_unexpected_hooks_structure_aborts_without_write(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": ["not", "a", "dict"]}))
        before = settings_path.read_text()

        with pytest.raises(ValueError):
            wire_hooks_into_settings(settings_path=settings_path)

        assert settings_path.read_text() == before

    def test_backup_created_when_file_existed(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        original = {"hooks": {}}
        settings_path.write_text(json.dumps(original))

        result = wire_hooks_into_settings(settings_path=settings_path)

        assert result["backup"] is not None
        backup = tmp_path / result["backup"].split("/")[-1]
        assert backup.exists()
        assert json.loads(backup.read_text()) == original

    def test_no_backup_when_nothing_to_write(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        wire_hooks_into_settings(settings_path=settings_path)
        backups_before = set(tmp_path.glob("*.bak*"))

        wire_hooks_into_settings(settings_path=settings_path)  # no-op re-run

        assert set(tmp_path.glob("*.bak*")) == backups_before


class TestCliSurface:
    def test_hooks_wire_command_wires_and_reports(self, tmp_path, capsys, monkeypatch):
        from dct import cli

        settings_path = tmp_path / "settings.json"
        monkeypatch.setattr(
            "sys.argv", ["dct", "hooks", "wire", "--settings", str(settings_path)]
        )
        cli.main()

        out = capsys.readouterr().out
        assert "4" in out and "hook" in out.lower()
        assert settings_path.exists()

    def test_hooks_wire_rerun_reports_noop(self, tmp_path, capsys, monkeypatch):
        from dct import cli

        settings_path = tmp_path / "settings.json"
        wire_hooks_into_settings(settings_path=settings_path)
        monkeypatch.setattr(
            "sys.argv", ["dct", "hooks", "wire", "--settings", str(settings_path)]
        )
        cli.main()

        out = capsys.readouterr().out.lower()
        assert "already wired" in out

    def test_hooks_wire_malformed_settings_exits_nonzero(
        self, tmp_path, capsys, monkeypatch
    ):
        from dct import cli

        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{broken")
        monkeypatch.setattr(
            "sys.argv", ["dct", "hooks", "wire", "--settings", str(settings_path)]
        )
        with pytest.raises(SystemExit) as excinfo:
            cli.main()

        assert excinfo.value.code != 0
        assert settings_path.read_text() == "{broken"
        assert "not valid JSON" in capsys.readouterr().out

    def test_init_parser_wire_flags(self):
        from dct import cli

        parser = cli.build_parser()
        assert parser.parse_args(["init"]).wire_hooks is None
        assert parser.parse_args(["init", "--wire-hooks"]).wire_hooks is True
        assert parser.parse_args(["init", "--no-wire-hooks"]).wire_hooks is False
