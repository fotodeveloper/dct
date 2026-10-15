"""K-73 (#917), the skill side: a missing dct reference is an asset
diagnostic of /dct-init, never "dct is not bootstrapped".

The skills are links into the package, so a package upgrade without `dct
upgrade` runs the new /dct-init before `~/.claude/dct-reference.md` exists.
The old Step 1 counted the reference into `bootstrap_complete` and then ran
the interactive `dct init` through Bash, which has no terminal (its first
`input()` raises EOFError) — registration was blocked by a missing asset.

The contract locked here (shared with the code lane): the reference is
probed apart from the bootstrap and never blocks registration; the remedy
offered is `dct integrate repair claude`, described with its real scope and
run only after the owner consents to that scope; `dct upgrade` is never run
by the skill and `dct init` never through Bash; a foreign file at the
reference path is named with its link target and must be moved aside or
replaced first — repair alone is not offered as its fix.

K-77 (#917 round 2): the skill never decides ownership itself (a header
line or a link target proves nothing — an edited copy keeps its header, and
only dct knows which links it recorded). It reads the canonical `shared_reference` row of
`dct doctor --client claude --json` — the installer's own classification
(current / missing / outdated / foreign, with its remedy) — and a valid JSON
on stdout with exit code 1 still carries that diagnosis.
"""

import inspect
import json
import re
import shutil

import pytest

from dct import assets, cli, paths
from dct.assets import skills_dir
from dct.cli import build_parser
from dct.integrations import claude, reference
from dct.integrations import doctor as doctor_mod


def _flat(text: str) -> str:
    return " ".join(text.split())


def _init() -> str:
    return (skills_dir() / "dct-init" / "SKILL.md").read_text(encoding="utf-8")


def _step(title: str) -> str:
    body = _init()
    at = body.index(title)
    nxt = body.find("\n### ", at + len(title))
    return body[at:nxt if nxt > 0 else len(body)]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n\s*\n|\n\s*(?:[-*]|\d+\.)\s+", text)
    return [_flat(p) for p in parts if p.strip()]


def _bootstrap_definition() -> str:
    hits = [s for s in _sentences(_step("### Step 1")) if "`bootstrap_complete` =" in s]
    assert len(hits) == 1, hits
    return hits[0]


def _reference_section() -> str:
    """Step 1's reference check: from its heading to the end of Step 1."""
    step = _step("### Step 1")
    at = step.index("**The reference**")
    return step[at:]


def test_the_reference_is_not_part_of_the_bootstrap():
    definition = _bootstrap_definition()
    assert not re.search(r"reference|four", definition, re.IGNORECASE), \
        f"a missing reference still reads as a missing bootstrap: {definition}"


def test_registration_never_waits_for_the_reference():
    flat = _flat(_reference_section())
    assert re.search(r"never blocks? (the )?registration|registration (goes on|continues)"
                     r"[^.]{0,60}whatever", flat, re.IGNORECASE), \
        "the reference check may still stop the registration"
    assert re.search(r"Step 2", flat), "the reference check does not continue to Step 2"


def test_the_skill_never_runs_dct_init_or_dct_upgrade():
    """`dct init` is interactive (no terminal under Bash → EOFError) and
    `dct upgrade` also upgrades the package — neither is run by the skill."""
    body = _init()
    flat = _flat(body)
    assert not re.search(r"Bash:?\s*`dct init`", flat), "the skill still runs `dct init` via Bash"
    assert not re.search(r"Bash:?\s*`dct upgrade`", flat), "the skill runs `dct upgrade`"
    for command in ("`dct init`", "`dct upgrade`"):
        for s in _sentences(body):
            if command in s and re.search(r"\b(run|runs|running)\b", s):
                assert re.search(r"\bnever\b|\bnot\b|\byour own terminal\b|\bowner runs\b|"
                                 r"\byou run\b|\bby the owner\b", s, re.IGNORECASE), \
                    f"{command} may be run by the skill: {s}"


def test_the_remedy_is_integrate_repair_claude_and_it_parses():
    flat = _flat(_reference_section())
    assert "`dct integrate repair claude`" in flat
    args = build_parser().parse_args(["integrate", "repair", "claude"])
    assert (args.command, args.action, args.client) == ("integrate", "repair", "claude")


def test_repair_runs_only_after_consent_to_its_scope():
    flat = _flat(_reference_section())
    assert "AskUserQuestion" in flat, "repair is not put to the owner as a question"
    assert re.search(r"\bonly (after|on)\b[^.]{0,80}\b(yes|consent|answer|agree)", flat,
                     re.IGNORECASE), "repair may run without the owner's consent"


# What each step of the real Claude repair does, as the owner must read it.
# A helper the repair starts calling and that is missing here fails the test:
# the skill's description of the repair's scope must follow the code.
REPAIR_EFFECTS = {
    "register_mcp": r"MCP",
    "install_skills": r"\bskills\b",
    "install_hook": r"\bhooks\b",
    "wire_hooks_into_settings": r"settings\.json",
    "install_reference": r"dct-reference\.md",
    "attach_claude_md_snippet": r"~/\.claude/CLAUDE\.md",
}


def _repair_helpers() -> set[str]:
    from dct.integrations import claude, manager

    adapter = claude.ClaudeIntegration
    steps = re.findall(r'"(install_\w+)"', inspect.getsource(manager.install_integration))
    hooks_step = getattr(adapter, "install_hooks", None)
    sources = [inspect.getsource(getattr(adapter, name)) for name in steps]
    if callable(hooks_step):
        sources.append(inspect.getsource(hooks_step))
    names = set()
    for source in sources:
        body = source.split(":", 1)[1]  # drop the def line (the method's own name)
        names |= {n for n in re.findall(r"\b([a-z_]+)\(", body)
                  if callable(getattr(claude, n, None))}
    return names


def test_the_repair_scope_named_to_the_owner_matches_the_code():
    flat = _flat(_reference_section())
    helpers = _repair_helpers()
    unknown = helpers - set(REPAIR_EFFECTS)
    assert not unknown, f"the repair does more than the skill describes: {unknown}"
    for helper in helpers:
        assert re.search(REPAIR_EFFECTS[helper], flat), \
            f"the repair's scope omits what {helper} changes"
    assert re.search(r"\bdatabase\b", flat) and re.search(r"\bpackage\b", flat), \
        "the owner is not told the repair leaves the database and the package alone"


def test_a_foreign_file_is_named_and_must_be_moved_aside_first():
    """A file at the reference path that is not dct's is preserved by every
    installer; the owner is told the path and the link target, and that it
    must be moved aside or replaced first — repair alone is not its fix."""
    flat = _flat(_reference_section())
    foreign = [s for s in _sentences(_reference_section())
               if re.search(r"\bforeign\b|not dct'?s", s, re.IGNORECASE)]
    assert foreign, "a foreign file at the reference path is not handled"
    assert re.search(r"link target", flat), "the link target is not shown"
    assert re.search(r"\bmove[^.]{0,40}\baside\b[^.]{0,40}\breplace|\breplace[^.]{0,40}\bmove"
                     r"[^.]{0,40}\baside", flat, re.IGNORECASE), \
        "the owner is not told to move aside or replace the foreign file"
    assert re.search(r"\bfirst\b", flat), "the collision is not settled before any repair"
    offers = [s for s in foreign if re.search(r"AskUserQuestion|\boffer", s)]
    for s in offers:
        assert re.search(r"\bnever\b|\bnot\b|\bno\b", s, re.IGNORECASE), \
            f"repair is offered as the fix of a foreign file: {s}"


@pytest.mark.parametrize("title", ["### Step 2", "### Step 7"])
def test_the_reports_carry_the_reference_state(title):
    """The diagnostic is not lost when the owner declines the repair: the
    status of a registered project and the final report both show it."""
    assert re.search(r"Reference:", _step(title)), f"{title} does not report the reference"


# ── K-77: the state comes from dct's diagnostics, never from a header probe ──

DOCTOR = "dct doctor --client claude --json"


def test_the_reference_state_is_read_from_the_doctor_json_and_the_command_parses():
    section = _reference_section()
    assert DOCTOR in _flat(section), "the skill does not read dct doctor's JSON"
    args = build_parser().parse_args(DOCTOR.split()[1:])
    assert (args.command, args.client, args.json) == ("doctor", "claude", True)


def test_the_skill_does_not_guess_ownership_from_the_file_itself():
    """An edited copy keeps its `<!-- dct:managed-copy` header, and a link's
    target alone does not say whether dct recorded it: the old probe called
    both dct's while the installer calls them foreign (K-77)."""
    section = _reference_section()
    assert "dct:managed-copy" not in section, "the skill still trusts the copy header"
    assert not re.search(r"`readlink`|first line", section), \
        "the skill still probes the file or the link itself"


def test_exit_code_one_with_valid_json_still_carries_the_diagnosis():
    flat = _flat(_reference_section())
    assert re.search(r"exits? (?:with )?(?:code )?1\b.{0,300}?\bstill carries", flat,
                     re.IGNORECASE), "exit code 1 may be taken for no answer"
    assert re.search(r"(?:other|unrelated) (?:rows?|checks?)[^.]{0,120}never block",
                     flat, re.IGNORECASE), "an unrelated failing check may block registration"


@pytest.fixture
def ref(tmp_path, monkeypatch):
    directory = tmp_path / "home" / ".config" / "dctools"
    directory.mkdir(parents=True)
    monkeypatch.setattr(paths, "config_dir", lambda: directory)
    legacy = tmp_path / "home" / ".claude" / "dct-reference.md"
    legacy.parent.mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_REFERENCE", legacy)
    return paths.dct_reference_path()


def _fake_package(root):
    target = root / "dct" / "assets"
    target.mkdir(parents=True)
    for name in ("dct-reference.md", "claude-md-snippet.md"):
        shutil.copy2(assets.assets_dir() / name, target / name)
    return target


def _edited_copy(ref, tmp_path, monkeypatch):
    claude._write_marked_copy(assets.dct_reference_path(), ref)
    with ref.open("a", encoding="utf-8") as stream:
        stream.write("my own note\n")  # the header stays, the body changed


def _old_valid_copy(ref, tmp_path, monkeypatch):
    old = tmp_path / "old-reference.md"
    old.write_text("# dct reference of an older package\n", encoding="utf-8")
    claude._write_marked_copy(old, ref)


def _link_from_previous_install(ref, tmp_path, monkeypatch):
    old_assets = _fake_package(tmp_path / "old-venv")
    monkeypatch.setattr(assets, "assets_dir", lambda: old_assets)
    assert claude.install_reference()["status"] == "installed"
    new_assets = _fake_package(tmp_path / "new-venv")
    (new_assets / "dct-reference.md").write_text("# a newer reference\n", encoding="utf-8")
    monkeypatch.setattr(assets, "assets_dir", lambda: new_assets)


def _missing(ref, tmp_path, monkeypatch):
    pass


def _current(ref, tmp_path, monkeypatch):
    assert claude.install_reference()["status"] == "installed"


# (setup, the installer's state for it)
SCENARIOS = {
    "edited copy": (_edited_copy, "foreign"),
    "old valid copy": (_old_valid_copy, "outdated"),
    "link from a previous install": (_link_from_previous_install, "outdated"),
    "missing": (_missing, "missing"),
    "current": (_current, "current"),
}


def _state_rule(state: str) -> str:
    """The skill's bullet that handles `state` (named as **`state`**)."""
    rules = [b for b in re.split(r"\n(?=- )", _reference_section())
             if b.startswith("- ") and re.search(rf"\*\*`{state}`\*\*", b.split("—")[0])]
    assert len(rules) == 1, f"the skill names no single rule for the state {state!r}: {rules}"
    return _flat(rules[0])


def _doctor_json(capsys, monkeypatch) -> tuple[dict, int]:
    """The real `dct doctor --client claude --json` output and exit code;
    only the Python runtime probes (subprocesses) are stubbed out."""
    monkeypatch.setattr(doctor_mod, "discover_pythons", lambda extra=(): [])
    monkeypatch.setattr(doctor_mod, "configured_runtimes", lambda client: ([], []))
    code = 0
    try:
        cli.cmd_doctor(build_parser().parse_args(DOCTOR.split()[1:]))
    except SystemExit as exc:
        code = exc.code
    return json.loads(capsys.readouterr().out), code


def _reference_row_as_the_skill_reads_it(result: dict) -> dict:
    """The path the skill names: integrations[] → client "claude" →
    checks[] → check "shared_reference"."""
    [integration] = [i for i in result["integrations"] if i["client"] == "claude"]
    [row] = [r for r in integration["checks"] if r["check"] == "shared_reference"]
    return row


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_skill_handles_each_state_the_installer_reports(
        scenario, ref, tmp_path, monkeypatch, capsys):
    setup, expected = SCENARIOS[scenario]
    setup(ref, tmp_path, monkeypatch)
    row = reference.reference_row(client="claude", check="shared_reference")
    assert row["state"] == expected, (scenario, row)

    result, code = _doctor_json(capsys, monkeypatch)
    shown = _reference_row_as_the_skill_reads_it(result)
    assert (shown["state"], shown["remedy"]) == (row["state"], row["remedy"])
    if code == 1:  # an unrelated check failed: the JSON still carries the row
        assert result["ok"] is False

    rule = _state_rule(row["state"])
    if row["remedy"] is None:
        assert re.search(r"nothing to (say|report)", rule, re.IGNORECASE), rule
    elif row["remedy"] == claude.REPAIR_REMEDY:
        assert re.search(r"repair question", rule, re.IGNORECASE), rule
        assert re.search(r"never blocks? (the )?registration|whatever the answer", rule), rule
    else:  # the file must be moved aside first: repair alone is not its fix
        assert row["state"] == "foreign", row
        assert re.search(r"\bno question\b", rule, re.IGNORECASE), rule
        assert re.search(r"never offered", rule, re.IGNORECASE), rule
    if scenario == "edited copy":
        assert re.search(r"\bedited\b", rule), "an edited copy is not named as foreign"


def test_the_skill_names_the_json_path_and_fields_it_reads():
    flat = _flat(_reference_section())
    for word in ("`integrations`", '`"client": "claude"`', "`checks`",
                 '`"check": "shared_reference"`', "`state`", "`message`", "`remedy`"):
        assert word in flat, f"the skill does not name {word}"
    row = reference.reference_row(client="claude", check="shared_reference")
    assert {"check", "state", "message", "remedy"} <= set(row)


@pytest.mark.parametrize("state", ["current", "missing", "outdated", "foreign"])
def test_the_state_rule_guard_notices_a_dropped_state(state):
    """Mutation check: renaming one state in the skill turns its lookup red."""
    section = _reference_section()
    assert re.search(rf"\*\*`{state}`\*\*", section)
    mutated = section.replace(f"**`{state}`**", "**`gone`**")
    rules = [b for b in re.split(r"\n(?=- )", mutated)
             if b.startswith("- ") and re.search(rf"\*\*`{state}`\*\*", b.split("—")[0])]
    assert rules == []


def test_canonical_reference_row_wins_when_legacy_alias_is_foreign(tmp_path, monkeypatch, capsys):
    """The pointer's actual asset and its compatibility alias can disagree."""
    canonical_dir = tmp_path / 'canonical'
    monkeypatch.setattr(paths, 'config_dir', lambda: canonical_dir)
    legacy = tmp_path / 'legacy-reference.md'
    monkeypatch.setattr(claude, 'CLAUDE_REFERENCE', legacy)
    legacy.write_text('Owner alias content')
    reference.install_reference(client='claude')
    result, _ = _doctor_json(capsys, monkeypatch)
    shown = _reference_row_as_the_skill_reads_it(result)
    assert shown['state'] == 'current'
    [integration] = [row for row in result['integrations'] if row['client'] == 'claude']
    [alias] = [row for row in integration['checks'] if row['check'] == 'reference']
    assert alias['state'] == 'foreign'
    assert legacy.read_text() == 'Owner alias content'
