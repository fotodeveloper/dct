"""`dct triage export / page / decide / accept / close` — the triage CLI (#909, #930).

Driven through `cli.main()` on a patched argv (parser wiring + dispatch end to
end) against the fixture engine (`cli_engine`, project 'proj-a'). Expectations:
the --json output parses and equals the workflow export; the default output is
three constant declarations whose values parse as JSON (JSON is valid
JavaScript, so they paste into the page template) and stay inert inside an
inline <script> whatever a finding's text holds (K-1); the positional cycle is
always a code within the project, an id goes through --cycle-id (K-13).

#930: `page` builds the page from the export into a copy of the template
(test_triage_page.SKELETON) and never loses an existing file; `decide` /
`accept` / `close` are CLI writers over the same workflow functions as the
MCP tools; every command shares one addressing matrix (code with the CWD's
project or --project, --cycle-id, both / neither refused, a digit code is a
code).
"""

from __future__ import annotations

import io
import json
import os
import stat
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa
from conftest import run_cli
from test_triage_page import SKELETON  # the one template fixture (a minimal copy)
from test_triage_page import _constant as _page_constant

from dct import cli
from dct.core.projects import create_project
from dct.db import findings as findings_table
from dct.workflows.triage import (
    close_triage,
    decide_findings,
    export_triage,
    open_triage,
    record_findings,
    suggest_findings,
)


def _finding(**over):
    base = {"title": "Lost update on concurrent edit", "fp": "last write wins at save by a race",
            "S": 2, "O": 1, "E": 2, "K": "M", "origin": "pre", "type": "correctness",
            "src": "codex", "ctx": ["Zażółć gęślą jaźń."],
            "opts": [{"k": "a", "t": "Row lock", "rec": True, "root": True}]}
    base.update(over)
    return base


@pytest.fixture
def cycle(cli_engine):
    cycle_id = open_triage(cli_engine, "proj-a", "rev-1", "review", "claude-code")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding()])
    return cycle_id


def _export(monkeypatch, capsys, *argv):
    run_cli(monkeypatch, ["triage", "export", *argv])
    return capsys.readouterr().out


def test_parser_wiring():
    args = cli.build_parser().parse_args(["triage", "export", "rev-1", "--project", "p", "--json"])
    assert (args.command, args.triage_command, args.cycle, args.project, args.json) == (
        "triage", "export", "rev-1", "p", True)


def test_json_output_equals_the_workflow_export(monkeypatch, capsys, cli_engine, cycle):
    decide_findings(cli_engine, cycle, {"K-1": {"dec": "fix", "opt": "a", "note": "ok"}},
                    decided_by="owner", decision_source="chat 2026-10-13")
    close_triage(cli_engine, cycle, source="chat")
    out = json.loads(_export(monkeypatch, capsys, "rev-1", "--project", "proj-a", "--json"))
    assert out == json.loads(json.dumps(export_triage(cli_engine, cycle)))
    assert out["closed"]["answers"]["K-1"]["dec"] == "fix"


def test_default_output_is_three_parseable_constants(monkeypatch, capsys, cli_engine, cycle):
    text = _export(monkeypatch, capsys, "rev-1", "--project", "proj-a")
    expected = export_triage(cli_engine, cycle)
    found = {}
    for name in ("CLOSED", "SUGGESTED", "CARDS"):
        start = text.index(f"const {name} = ") + len(f"const {name} = ")
        end = text.index(";\n", start)
        found[name] = json.loads(text[start:end])
    assert found == {"CLOSED": expected["closed"], "SUGGESTED": expected["suggested"],
                     "CARDS": expected["cards"]}
    assert "Zażółć gęślą jaźń." in text  # readable, not \\u-escaped


def test_cycle_id_is_accepted_with_or_without_project(monkeypatch, capsys, cycle):
    for extra in ((), ("--project", "proj-a")):
        out = json.loads(_export(monkeypatch, capsys, "--cycle-id", str(cycle), *extra, "--json"))
        assert out["cycle"]["code"] == "rev-1"


def test_a_digit_only_cycle_is_a_code_never_an_id(monkeypatch, capsys, cli_engine, cycle):
    """Cycle #1 exists; the positional "1" is a code — found when a cycle is
    CODED "1", refused when none is, never guessed to mean id #1."""
    assert cycle == 1
    with pytest.raises(SystemExit):
        _export(monkeypatch, capsys, "1", "--project", "proj-a")
    assert "No triage cycle '1'" in capsys.readouterr().err
    coded = open_triage(cli_engine, "proj-a", "1", "digit code", "me")["cycle_id"]
    out = json.loads(_export(monkeypatch, capsys, "1", "--project", "proj-a", "--json"))
    assert out["cycle"]["id"] == coded


@pytest.mark.parametrize(("ref", "message"), [
    (("rev-1",), "No triage cycle 'rev-1' in project 'proj-b'"),
    (("--cycle-id", "1"), "does not belong to project 'proj-b'"),
])
def test_another_projects_cycle_is_refused(monkeypatch, capsys, cli_engine, cycle, ref, message):
    create_project(cli_engine, slug="proj-b", name="B", path="/tmp/proj-b")
    with pytest.raises(SystemExit) as exc:
        _export(monkeypatch, capsys, *ref, "--project", "proj-b")
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ("nope", "--project", "proj-a"),
    ("rev-1", "--project", "zzz"),
    ("--cycle-id", "999"),
    ("--cycle-id", "0"),
    ("rev-1", "--cycle-id", "1", "--project", "proj-a"),   # both
    ("--project", "proj-a"),                               # neither
])
def test_unknown_cycle_or_project_exits_1(monkeypatch, capsys, cycle, argv):
    with pytest.raises(SystemExit) as exc:
        _export(monkeypatch, capsys, *argv)
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("✗ Error:")


def test_group_without_subcommand_prints_help(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(monkeypatch, ["triage"])
    assert exc.value.code == 0
    assert "export" in capsys.readouterr().out


# ── K-1: the constants are inert inside the page's <script> ─────────────────

HOSTILE = {
    "title": "Closes the page: </script><script>alert(1)</script>",
    "ctx": ["<!-- opens a comment", "a & b < c > d", "line\u2028separator\u2029para"],
    "opt": "</SCRIPT >&lt;",
}


class _ScriptCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.starts: list[str] = []
        self.comments = 0

    def handle_starttag(self, tag, attrs):
        self.starts.append(tag)

    def handle_comment(self, data):
        self.comments += 1


@pytest.fixture
def hostile_cycle(cli_engine):
    cycle_id = open_triage(cli_engine, "proj-a", "rev-h", "review", "claude-code")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding(
        title=HOSTILE["title"], ctx=HOSTILE["ctx"],
        opts=[{"k": "a", "t": HOSTILE["opt"], "why": "<b>bold</b>", "rec": True}])])
    decide_findings(cli_engine, cycle_id, {"K-1": {"dec": "fix", "opt": "a",
                                                   "note": "</script> verbatim"}},
                    decided_by="owner", decision_source="chat")
    close_triage(cli_engine, cycle_id, source="chat </script>", notes=["<!-- n -->"])
    return cycle_id


def test_hostile_texts_cannot_leave_the_script(monkeypatch, capsys, cli_engine, hostile_cycle):
    text = _export(monkeypatch, capsys, "rev-h", "--project", "proj-a")
    assert "</" not in text and "<!--" not in text
    assert "\u2028" not in text and "\u2029" not in text

    page = _ScriptCounter()
    page.feed(f"<html><body><script>\n{text}</script></body></html>")
    page.close()
    assert page.starts == ["html", "body", "script"] and page.comments == 0


def test_hostile_texts_parse_back_to_the_recorded_originals(
        monkeypatch, capsys, cli_engine, hostile_cycle):
    text = _export(monkeypatch, capsys, "rev-h", "--project", "proj-a")
    expected = export_triage(cli_engine, hostile_cycle)
    found = {}
    for name in ("CLOSED", "SUGGESTED", "CARDS"):
        start = text.index(f"const {name} = ") + len(f"const {name} = ")
        end = text.index(";\n", start)
        found[name] = json.loads(text[start:end])
    assert found == {"CLOSED": expected["closed"], "SUGGESTED": expected["suggested"],
                     "CARDS": expected["cards"]}
    (card,) = found["CARDS"]
    assert card["title"] == HOSTILE["title"] and card["ctx"] == HOSTILE["ctx"]
    assert card["opts"][0]["t"] == HOSTILE["opt"]
    assert found["CLOSED"]["answers"]["K-1"]["note"] == "</script> verbatim"


def test_json_output_keeps_the_texts_as_recorded(monkeypatch, capsys, hostile_cycle):
    """--json is data, not a page fragment: the owner's condition — no
    literal escape sequences written into the data."""
    out = json.loads(_export(monkeypatch, capsys, "rev-h", "--project", "proj-a", "--json"))
    assert out["cards"][0]["title"] == HOSTILE["title"]
    assert out["closed"]["source"] == "chat </script>"


# ── #930: page / decide / accept / close ─────────────────────────────────────
# One addressing contract for every triage command (spec #930 §8): the code
# within --project, else the CWD's project; --cycle-id checked against
# --project only when given; both / neither refused; a numeric code is a code.

OWNER_ARGS = ("--decided-by", "owner", "--source", "chat 2026-10-14")


def _digest_args(engine, cycle_id):
    """`--expected-digest <SUGGESTED.digest>` of the cycle's export now — the
    page the owner is taken to have seen."""
    return ("--expected-digest", export_triage(engine, cycle_id)["suggested"]["digest"])


def _decisions(engine, cycle_id):
    """{seq: (decision, opt, note, decided_by, decision_source)} as stored."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(findings_table.c.seq, findings_table.c.decision,
                      findings_table.c.decision_opt, findings_table.c.decision_note,
                      findings_table.c.decided_by, findings_table.c.decision_source)
            .where(findings_table.c.cycle_id == cycle_id)).all()
    return {r[0]: tuple(r[1:]) for r in rows}


@pytest.fixture
def two_cards(cli_engine):
    """Cycle 'rev-2' with K-1 (options a, b) and K-2 (option a)."""
    cycle_id = open_triage(cli_engine, "proj-a", "rev-2", "review two", "claude-code")["cycle_id"]
    record_findings(cli_engine, cycle_id, [
        _finding(opts=[{"k": "a", "t": "Row lock", "rec": True}, {"k": "b", "t": "Retry"}]),
        _finding(fp="second fp", title="Second")])
    return cycle_id


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "tpl" / "triage-template.html"
    path.parent.mkdir()
    path.write_text(SKELETON, encoding="utf-8")
    return path


def _run(monkeypatch, capsys, *argv):
    """(exit code, stdout, stderr) of `dct triage <argv>`."""
    code = 0
    try:
        run_cli(monkeypatch, ["triage", *argv])
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err




def test_new_subcommands_are_wired():
    parser = cli.build_parser()
    page = parser.parse_args(["triage", "page", "rev-1", "--out", "o.html", "--force"])
    assert (page.triage_command, page.cycle, page.out, page.force) == ("page", "rev-1",
                                                                      "o.html", True)
    close = parser.parse_args(["triage", "close", "--cycle-id", "3", "--source", "s",
                               "--note", "one", "--note", "two"])
    assert (close.cycle_id, close.note) == (3, ["one", "two"])
    accept = parser.parse_args(["triage", "accept", "r", "--expected-by", "Codex",
                                "--expected-digest", "ab12", *OWNER_ARGS])
    assert (accept.expected_by, accept.expected_digest, accept.overwrite) == (
        "Codex", "ab12", False)


@pytest.mark.parametrize("argv", [
    ("decide", "--from-text", "-", "--source", "s"),          # --decided-by missing
    ("accept", "--expected-digest", "d", "--decided-by", "o", "--source", "s"),  # --expected-by
    ("accept", "--expected-by", "C", "--decided-by", "o", "--source", "s"),  # --expected-digest
    ("close",),                                              # --source missing
    ("page",),                                               # --out missing
])
def test_required_flags_are_enforced_by_the_parser(argv):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["triage", *argv[:1], "rev-1", *argv[1:]])
    assert exc.value.code == 2


# ── page ─────────────────────────────────────────────────────────────────────


def test_page_writes_the_export_into_the_template(monkeypatch, capsys, cli_engine, two_cards,
                                                  template, tmp_path):
    out = tmp_path / "rev-2.html"
    code, stdout, _err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                              "--template", str(template), "--out", str(out))
    assert code == 0
    assert stdout.strip() == f"{out}: 2 cards, closed=no, suggested=none"
    page = out.read_text(encoding="utf-8")
    expected = export_triage(cli_engine, two_cards)
    assert _page_constant(page, "CARDS") == expected["cards"]
    assert _page_constant(page, "CLOSED") is None and _page_constant(page, "SUGGESTED") is None
    assert "<title>Triage review two</title>" in page
    assert f"triage-proj-a-rev-2-c{two_cards}-v1" in page  # K-34


def test_page_metadata_flags_override_the_defaults(monkeypatch, capsys, two_cards, template,
                                                   tmp_path):
    out = tmp_path / "p.html"
    code, _o, _e = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                        "--template", str(template), "--out", str(out),
                        "--topic", "A <b> topic", "--context", "ctx-x", "--lede", "My lede")
    assert code == 0
    page = out.read_text(encoding="utf-8")
    assert "<h1>Triage A &lt;b&gt; topic</h1>" in page
    assert "ctx-x" in page and '<p class="lede">My lede</p>' in page


def test_page_reports_a_closed_cycle_and_the_proposer(monkeypatch, capsys, cli_engine,
                                                      two_cards, template, tmp_path):
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "fix", "opt": "a"}})
    decide_findings(cli_engine, two_cards, {"K-1": {"dec": "fix"}, "K-2": {"dec": "note"}},
                    decided_by="owner", decision_source="chat")
    close_triage(cli_engine, two_cards, source="chat 2026-10-14")
    out = tmp_path / "closed.html"
    code, stdout, _e = _run(monkeypatch, capsys, "page", "--cycle-id", str(two_cards),
                            "--template", str(template), "--out", str(out))
    assert code == 0
    digest = export_triage(cli_engine, two_cards)["suggested"]["digest"]
    assert stdout.strip().endswith(f"2 cards, closed=yes, suggested=Codex, digest={digest}")
    assert _page_constant(out.read_text(encoding="utf-8"), "CLOSED")["source"] == \
        "chat 2026-10-14"


def test_page_template_comes_from_the_config_with_tilde_expanded(
        monkeypatch, capsys, two_cards, template, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "tpl.html").write_text(SKELETON.replace("<h1>", "<h1 id=cfg>"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    config = tmp_path / "dct.toml"
    config.write_text('[triage]\ntemplate = "~/tpl.html"\n', encoding="utf-8")
    monkeypatch.setenv("DCT_CONFIG_PATH", str(config))

    out = tmp_path / "from-config.html"
    assert _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                "--out", str(out))[0] == 0
    assert "<h1 id=cfg>" in out.read_text(encoding="utf-8")

    explicit = tmp_path / "from-flag.html"
    assert _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                "--template", str(template), "--out", str(explicit))[0] == 0
    assert "<h1 id=cfg>" not in explicit.read_text(encoding="utf-8")  # --template wins


def test_page_without_any_template_names_both_ways_and_writes_nothing(
        monkeypatch, capsys, two_cards, tmp_path):
    monkeypatch.setenv("DCT_CONFIG_PATH", str(tmp_path / "absent.toml"))
    out = tmp_path / "p.html"
    code, _o, err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                         "--out", str(out))
    assert code == 1
    assert "--template" in err and "[triage] template" in err
    assert not out.exists()


def test_page_refuses_an_existing_out_without_force(monkeypatch, capsys, two_cards, template,
                                                    tmp_path):
    out = tmp_path / "published.html"
    out.write_text("the published page", encoding="utf-8")
    argv = ("page", "rev-2", "--project", "proj-a", "--template", str(template),
            "--out", str(out))
    code, _o, err = _run(monkeypatch, capsys, *argv)
    assert code == 1 and "--force" in err
    assert out.read_text(encoding="utf-8") == "the published page"

    assert _run(monkeypatch, capsys, *argv, "--force")[0] == 0
    assert "const CARDS = " in out.read_text(encoding="utf-8")


def test_a_failed_build_keeps_the_existing_page(monkeypatch, capsys, two_cards, tmp_path):
    broken = tmp_path / "broken.html"
    broken.write_text(SKELETON.replace("const CARDS = [", "const KARDS = ["), encoding="utf-8")
    out_dir = tmp_path / "pages"
    out_dir.mkdir()
    out = out_dir / "rev-2.html"
    out.write_bytes(b"old page \xc5\xbc")
    code, _o, err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                         "--template", str(broken), "--out", str(out), "--force")
    assert code == 1 and "CARDS" in err
    assert out.read_bytes() == b"old page \xc5\xbc"
    assert sorted(p.name for p in out_dir.iterdir()) == ["rev-2.html"]  # no temp file left


def test_page_refuses_a_missing_parent_directory(monkeypatch, capsys, two_cards, template,
                                                 tmp_path):
    out = tmp_path / "no-such-dir" / "p.html"
    code, _o, err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                         "--template", str(template), "--out", str(out), "--force")
    assert code == 1 and "no such directory" in err
    assert not out.parent.exists()


def test_page_refuses_to_overwrite_its_template(monkeypatch, capsys, two_cards, template,
                                                tmp_path):
    link = tmp_path / "link.html"
    link.symlink_to(template)
    for target in (template, link):
        code, _o, err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                             "--template", str(template), "--out", str(target), "--force")
        assert code == 1 and "template itself" in err, target
    assert template.read_text(encoding="utf-8") == SKELETON


def test_page_into_a_directory_uses_a_safe_file_name(monkeypatch, capsys, cli_engine,
                                                     template, tmp_path):
    cycle_id = open_triage(cli_engine, "proj-a", "../rev 2/ü", "t", "me")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding()])
    out_dir = tmp_path / "pages"
    out_dir.mkdir()
    code, stdout, _e = _run(monkeypatch, capsys, "page", "--cycle-id", str(cycle_id),
                            "--template", str(template), "--out", str(out_dir))
    assert code == 0
    assert [p.name for p in out_dir.iterdir()] == ["-rev-2--.html"]
    assert stdout.startswith(str(out_dir / "-rev-2--.html"))


def test_page_never_writes_through_a_symlink(monkeypatch, capsys, two_cards, template,
                                             tmp_path):
    """A (dangling) symlink at --out counts as existing; --force replaces the
    link itself and never creates or changes its target."""
    target = tmp_path / "elsewhere.html"
    out = tmp_path / "p.html"
    out.symlink_to(target)
    argv = ("page", "rev-2", "--project", "proj-a", "--template", str(template),
            "--out", str(out))
    assert _run(monkeypatch, capsys, *argv)[0] == 1
    assert _run(monkeypatch, capsys, *argv, "--force")[0] == 0
    assert not out.is_symlink() and not target.exists()


@pytest.mark.parametrize("empty", ["", "   "])
def test_page_with_an_empty_out_is_refused_before_anything_is_read(
        monkeypatch, capsys, two_cards, tmp_path, empty):
    """K-41: `--out ""` would resolve to the CWD (a directory) and write
    `<code>.html` there; it is refused first — before the template (absent
    here) or the database is read."""
    monkeypatch.chdir(tmp_path)
    code, _o, err = _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                         "--template", str(tmp_path / "absent.html"), "--out", empty)
    assert code == 1 and "--out is empty" in err
    assert list(tmp_path.iterdir()) == []


# ── page --force: permissions of the replaced file (K-36) ────────────────────


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def umask_027():
    """A known process umask for the creation-policy tests (restored after)."""
    old = os.umask(0o027)
    yield 0o027
    os.umask(old)


def _page(monkeypatch, capsys, template, out, *extra):
    return _run(monkeypatch, capsys, "page", "rev-2", "--project", "proj-a",
                "--template", str(template), "--out", str(out), *extra)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640, 0o755])
def test_force_keeps_the_replaced_files_permission_bits(monkeypatch, capsys, two_cards,
                                                        template, tmp_path, umask_027, mode):
    out = tmp_path / "page.html"
    out.write_text("old", encoding="utf-8")
    out.chmod(mode)
    assert _page(monkeypatch, capsys, template, out, "--force")[0] == 0
    assert "const CARDS = " in out.read_text(encoding="utf-8")
    assert _mode(out) == mode  # neither the umask nor the temp file's 0600 leaks in


def test_force_drops_special_bits_of_the_replaced_file(monkeypatch, capsys, two_cards,
                                                       template, tmp_path):
    """Only the rwx bits travel: a setuid / setgid / sticky page (reported by
    lstat — many filesystems refuse to set them for a user) is replaced by a
    plain 0755 file."""
    out = tmp_path / "page.html"
    out.write_text("old", encoding="utf-8")
    out.chmod(0o755)
    real_lstat = os.lstat

    def lstat_with_special_bits(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.fspath(path) != os.fspath(out) or not stat.S_ISREG(st.st_mode):
            return st
        fields = list(st)
        fields[stat.ST_MODE] = st.st_mode | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
        return os.stat_result(fields)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", lstat_with_special_bits)
        assert _page(monkeypatch, capsys, template, out, "--force")[0] == 0
    assert _mode(out) == 0o755


def test_a_new_file_gets_the_same_mode_with_or_without_force(monkeypatch, capsys, two_cards,
                                                             template, tmp_path, umask_027):
    plain, forced = tmp_path / "plain.html", tmp_path / "forced.html"
    assert _page(monkeypatch, capsys, template, plain)[0] == 0
    assert _page(monkeypatch, capsys, template, forced, "--force")[0] == 0
    assert _mode(plain) == _mode(forced) == 0o666 & ~umask_027
    assert os.umask(umask_027) == umask_027  # the CLI left the process umask alone


def test_force_onto_a_symlink_uses_the_creation_policy_not_the_targets_mode(
        monkeypatch, capsys, two_cards, template, tmp_path, umask_027):
    target = tmp_path / "secret.html"
    target.write_text("target", encoding="utf-8")
    target.chmod(0o600)
    out = tmp_path / "link.html"
    out.symlink_to(target)
    assert _page(monkeypatch, capsys, template, out, "--force")[0] == 0
    assert not out.is_symlink() and _mode(out) == 0o666 & ~umask_027
    assert target.read_text(encoding="utf-8") == "target" and _mode(target) == 0o600


def test_a_failed_chmod_leaves_the_old_page(monkeypatch, capsys, two_cards, template,
                                            tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    out = pages / "page.html"
    out.write_bytes(b"old page")
    out.chmod(0o640)

    def refuse(*_args, **_kwargs):
        raise PermissionError("chmod refused")

    monkeypatch.setattr(os, "chmod", refuse)
    code, _o, err = _page(monkeypatch, capsys, template, out, "--force")
    assert code == 1 and "chmod refused" in err
    assert out.read_bytes() == b"old page" and _mode(out) == 0o640
    assert [p.name for p in pages.iterdir()] == ["page.html"]  # no temp file left


# ── decide --from-text ───────────────────────────────────────────────────────

DECISION_TEXT = (
    "Triage review two (proj-a) — decyzje ownera (2026-10-14 10:00), rubric v2:\r\n"
    "K-1: Fix teraz · wariant (b) [inny niż rekomendacja] · uwagi: Zażółć · gęślą [jaźń]\r\n"
)


def test_decide_from_a_file_changes_only_the_named_cards(monkeypatch, capsys, cli_engine,
                                                         two_cards, tmp_path):
    text = tmp_path / "answers.txt"
    text.write_text(DECISION_TEXT, encoding="utf-8")
    code, stdout, _e = _run(monkeypatch, capsys, "decide", "rev-2", "--project", "proj-a",
                            "--from-text", str(text), *OWNER_ARGS)
    assert code == 0
    assert "Parsed 1, decided 1" in stdout and "undecided: K-2" in stdout
    assert _decisions(cli_engine, two_cards) == {
        1: ("fix", "b", "Zażółć · gęślą [jaźń]", "owner", "chat 2026-10-14"),
        2: (None, None, None, None, None)}


def test_decide_reads_utf8_from_stdin(monkeypatch, capsys, cli_engine, two_cards):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(
        io.BytesIO("K-2: Do dyskusji · uwagi: ąę\nk-01: odnotuj\n".encode()),
        encoding="ascii"))  # a C-locale stdin: the CLI must decode UTF-8 itself
    code, stdout, _e = _run(monkeypatch, capsys, "decide", "--cycle-id", str(two_cards),
                            "--from-text", "-", *OWNER_ARGS)
    assert code == 0 and "undecided: none" in stdout
    stored = _decisions(cli_engine, two_cards)
    assert stored[1][:3] == ("note", None, None)
    assert stored[2][:3] == ("discuss", None, "ąę")


@pytest.mark.parametrize("text", [
    "",                                                       # nothing
    "K-1: Fix teraz\nK-2: — (brak decyzji) · uwagi: później\n",  # a note with no decision
    "K-1: Fix teraz\nK-9: Odnotuj\n",                         # unknown card
    "K-1: Fix teraz · wariant (z)\n",                         # unknown option
    "K-1: fix\nK-01: note\n",                                 # the same card twice
    "K-1: fix\nsome prose\n",                                 # not a card line
])
def test_decide_with_an_invalid_text_writes_nothing(monkeypatch, capsys, cli_engine,
                                                    two_cards, tmp_path, text):
    path = tmp_path / "bad.txt"
    path.write_text(text, encoding="utf-8")
    before = _decisions(cli_engine, two_cards)
    code, _o, err = _run(monkeypatch, capsys, "decide", "rev-2", "--project", "proj-a",
                         "--from-text", str(path), *OWNER_ARGS)
    assert code == 1 and err.startswith("✗ Error:")
    assert _decisions(cli_engine, two_cards) == before


def test_decide_skips_a_card_left_without_a_decision_and_reports_it(
        monkeypatch, capsys, cli_engine, two_cards, tmp_path):
    """K-31: the page emits "— (brak decyzji)" for a card the owner did not
    answer; it is skipped (stored state kept) and named in the output."""
    decide_findings(cli_engine, two_cards, {"K-2": {"dec": "backlog", "note": "earlier"}},
                    decided_by="owner", decision_source="chat 1")
    path = tmp_path / "partial.txt"
    path.write_text("K-1: Odnotuj\nK-2: — (brak decyzji)\n", encoding="utf-8")
    code, stdout, _e = _run(monkeypatch, capsys, "decide", "rev-2", "--project", "proj-a",
                            "--from-text", str(path), *OWNER_ARGS)
    assert code == 0, _e
    assert "decided 1" in stdout
    assert "skipped (no decision on the page, unchanged): K-2" in stdout
    stored = _decisions(cli_engine, two_cards)
    assert stored[1][0] == "note"
    assert stored[2] == ("backlog", None, "earlier", "owner", "chat 1")


def test_decide_from_a_missing_file_is_an_error(monkeypatch, capsys, two_cards, tmp_path):
    code, _o, err = _run(monkeypatch, capsys, "decide", "rev-2", "--project", "proj-a",
                         "--from-text", str(tmp_path / "absent.txt"), *OWNER_ARGS)
    assert code == 1 and err.startswith("✗ Error:")


def test_decide_on_a_closed_cycle_is_refused(monkeypatch, capsys, cli_engine, two_cards,
                                            tmp_path):
    decide_findings(cli_engine, two_cards, {"K-1": {"dec": "fix"}, "K-2": {"dec": "note"}},
                    decided_by="owner", decision_source="chat")
    close_triage(cli_engine, two_cards, source="chat")
    path = tmp_path / "late.txt"
    path.write_text("K-1: Odrzuć / zaakceptuj stan\n", encoding="utf-8")
    code, _o, err = _run(monkeypatch, capsys, "decide", "rev-2", "--project", "proj-a",
                         "--from-text", str(path), *OWNER_ARGS)
    assert code == 1 and "closed" in err
    assert _decisions(cli_engine, two_cards)[1][0] == "fix"


# ── accept ───────────────────────────────────────────────────────────────────


def test_accept_takes_the_proposals_and_reports_the_rest(monkeypatch, capsys, cli_engine,
                                                         two_cards):
    suggest_findings(cli_engine, two_cards, "Codex",
                     {"K-1": {"dec": "fix", "opt": "b", "note": "go"}})
    code, stdout, _e = _run(monkeypatch, capsys, "accept", "rev-2", "--project", "proj-a",
                            "--expected-by", "Codex", *_digest_args(cli_engine, two_cards),
                            *OWNER_ARGS)
    assert code == 0
    assert "Accepted 1 proposal(s) by Codex" in stdout and "undecided: K-2" in stdout
    assert "without a proposal (untouched): K-2" in stdout
    assert _decisions(cli_engine, two_cards)[1] == (
        "fix", "b", "[zaakceptowana propozycja Codex] go", "owner", "chat 2026-10-14")


def test_accept_refuses_proposals_the_owner_did_not_see(monkeypatch, capsys, cli_engine,
                                                        two_cards):
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "fix"}})
    suggest_findings(cli_engine, two_cards, "panel", {"K-2": {"dec": "reject"}})
    before = _decisions(cli_engine, two_cards)
    code, _o, err = _run(monkeypatch, capsys, "accept", "rev-2", "--project", "proj-a",
                         "--expected-by", "Codex", *_digest_args(cli_engine, two_cards),
                         *OWNER_ARGS)
    assert code == 1 and "panel" in err
    assert _decisions(cli_engine, two_cards) == before


def test_accept_refuses_a_proposal_revised_since_the_page_was_built(
        monkeypatch, capsys, cli_engine, two_cards):
    """K-32: the same author revising a proposal after the owner's page was
    built changes the digest — accepting with the digest he saw is refused."""
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "fix", "note": "v1"}})
    seen = _digest_args(cli_engine, two_cards)
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "reject", "note": "v2"}})
    before = _decisions(cli_engine, two_cards)
    code, _o, err = _run(monkeypatch, capsys, "accept", "rev-2", "--project", "proj-a",
                         "--expected-by", "Codex", *seen, *OWNER_ARGS)
    assert code == 1 and "digest" in err
    assert _decisions(cli_engine, two_cards) == before


def test_accept_refuses_an_empty_digest(monkeypatch, capsys, cli_engine, two_cards):
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "fix"}})
    before = _decisions(cli_engine, two_cards)
    code, _o, err = _run(monkeypatch, capsys, "accept", "rev-2", "--project", "proj-a",
                         "--expected-by", "Codex", "--expected-digest", " ", *OWNER_ARGS)
    assert code == 1 and "expected_digest" in err
    assert _decisions(cli_engine, two_cards) == before


def test_accept_replaces_a_decision_only_with_overwrite(monkeypatch, capsys, cli_engine,
                                                        two_cards):
    suggest_findings(cli_engine, two_cards, "Codex", {"K-1": {"dec": "fix"}})
    decide_findings(cli_engine, two_cards, {"K-1": {"dec": "backlog"}},
                    decided_by="owner", decision_source="earlier")
    argv = ("accept", "rev-2", "--project", "proj-a", "--expected-by", "Codex",
            *_digest_args(cli_engine, two_cards), *OWNER_ARGS)
    code, _o, err = _run(monkeypatch, capsys, *argv)
    assert code == 1 and "K-1" in err
    assert _decisions(cli_engine, two_cards)[1][0] == "backlog"

    code, stdout, _e = _run(monkeypatch, capsys, *argv, "--overwrite")
    assert code == 0 and "replaced earlier decisions: K-1" in stdout
    assert _decisions(cli_engine, two_cards)[1][:3] == (
        "fix", None, "[zaakceptowana propozycja Codex]")


# ── close ────────────────────────────────────────────────────────────────────


def test_close_takes_repeatable_notes(monkeypatch, capsys, cli_engine, two_cards):
    decide_findings(cli_engine, two_cards, {"K-1": {"dec": "fix"}, "K-2": {"dec": "discuss"}},
                    decided_by="owner", decision_source="chat")
    code, stdout, _e = _run(monkeypatch, capsys, "close", "rev-2", "--project", "proj-a",
                            "--source", "chat 2026-10-14", "--note", "Executed: K-1.",
                            "--note", "Commit NOT approved.")
    assert code == 0 and "2 answer(s)" in stdout
    closed = export_triage(cli_engine, two_cards)["closed"]
    assert closed["source"] == "chat 2026-10-14"
    assert closed["notes"] == ["Executed: K-1.", "Commit NOT approved."]


def test_close_with_undecided_cards_is_refused(monkeypatch, capsys, cli_engine, two_cards):
    decide_findings(cli_engine, two_cards, {"K-1": {"dec": "fix"}},
                    decided_by="owner", decision_source="chat")
    code, _o, err = _run(monkeypatch, capsys, "close", "rev-2", "--project", "proj-a",
                         "--source", "chat")
    assert code == 1 and "K-2" in err
    assert export_triage(cli_engine, two_cards)["closed"] is None


# ── the addressing matrix, every command ─────────────────────────────────────


def _command(name, tmp_path, template, digest="0" * 64):
    """argv tail of one triage command, run against whatever cycle the matrix
    addresses (the cycle has K-1 with option a); `digest` = the addressed
    cycle's SUGGESTED.digest for `accept`."""
    text = tmp_path / "text.txt"
    text.write_text("K-1: note\n", encoding="utf-8")
    return {
        "export": ("--json",),
        "page": ("--template", str(template), "--out", str(tmp_path / "out.html"), "--force"),
        "decide": ("--from-text", str(text), *OWNER_ARGS),
        "accept": ("--expected-by", "Codex", "--expected-digest", digest, *OWNER_ARGS),
        "close": ("--source", "chat"),
    }[name]


@pytest.fixture
def matrix(cli_engine, tmp_path, monkeypatch):
    """Project 'proj-c' registered at a real directory (the CWD), with cycle
    'rev-m' — K-1 proposed by Codex and decided, so every command succeeds —
    and, in 'proj-a', cycle id 1 next to a cycle CODED '1'."""
    create_project(cli_engine, slug="proj-c", name="C", path=str(tmp_path))
    first = open_triage(cli_engine, "proj-a", "rev-first", "t", "me")["cycle_id"]
    cycle_id = open_triage(cli_engine, "proj-c", "rev-m", "t", "me")["cycle_id"]
    record_findings(cli_engine, cycle_id, [_finding()])
    suggest_findings(cli_engine, cycle_id, "Codex", {"K-1": {"dec": "note"}})
    decide_findings(cli_engine, cycle_id, {"K-1": {"dec": "note"}},
                    decided_by="owner", decision_source="chat")
    monkeypatch.chdir(tmp_path)
    return {"first": first, "cycle_id": cycle_id}


COMMANDS = ["export", "page", "decide", "accept", "close"]


@pytest.mark.parametrize("name", COMMANDS)
@pytest.mark.parametrize("selector", ["code-cwd", "code-project", "id", "id-project"])
def test_every_command_addresses_a_cycle_by_code_or_id(monkeypatch, capsys, cli_engine,
                                                       matrix, template, tmp_path, name,
                                                       selector):
    cycle_id = matrix["cycle_id"]
    ref = {"code-cwd": ("rev-m",), "code-project": ("rev-m", "--project", "proj-c"),
           "id": ("--cycle-id", str(cycle_id)),
           "id-project": ("--cycle-id", str(cycle_id), "--project", "proj-c")}[selector]
    digest = export_triage(cli_engine, cycle_id)["suggested"]["digest"]
    argv = _command(name, tmp_path, template, digest)
    if name == "accept":  # the proposal is accepted over the existing decision
        argv = (*argv, "--overwrite")
    code, _o, err = _run(monkeypatch, capsys, name, *ref, *argv)
    assert code == 0, err
    if name == "close":
        assert export_triage(cli_engine, cycle_id)["closed"] is not None


@pytest.mark.parametrize("name", COMMANDS)
@pytest.mark.parametrize("ref", [
    ("rev-m", "--cycle-id", "2"),          # both
    (),                                    # neither
    ("rev-m", "--project", "proj-a"),      # the code exists only in proj-c
    ("--cycle-id", "2", "--project", "proj-a"),
    ("--cycle-id", "999"),
], ids=["both", "neither", "code-other-project", "id-other-project", "unknown-id"])
def test_every_command_refuses_a_bad_target_and_changes_nothing(
        monkeypatch, capsys, cli_engine, matrix, template, tmp_path, name, ref):
    assert matrix["cycle_id"] == 2
    before = (_decisions(cli_engine, 2), export_triage(cli_engine, 2))
    # The real cycle's digest: the target alone is what is wrong.
    argv = _command(name, tmp_path, template, before[1]["suggested"]["digest"])
    code, _o, err = _run(monkeypatch, capsys, name, *ref, *argv)
    assert code == 1 and err.startswith("✗ Error:")
    assert (_decisions(cli_engine, 2), export_triage(cli_engine, 2)) == before
    assert not (tmp_path / "out.html").exists()


@pytest.mark.parametrize("name", COMMANDS)
def test_every_command_reads_a_digit_code_as_a_code(monkeypatch, capsys, cli_engine, matrix,
                                                    template, tmp_path, name):
    """Cycle id 1 exists in proj-a: the positional '1' must find the cycle
    CODED '1', never id #1."""
    coded = open_triage(cli_engine, "proj-a", "1", "digit", "me")["cycle_id"]
    record_findings(cli_engine, coded, [_finding()])
    suggest_findings(cli_engine, coded, "Codex", {"K-1": {"dec": "note"}})
    decide_findings(cli_engine, coded, {"K-1": {"dec": "note"}},
                    decided_by="owner", decision_source="chat")
    argv = _command(name, tmp_path, template, export_triage(cli_engine, coded)["suggested"]["digest"])
    if name == "accept":
        argv = (*argv, "--overwrite")
    code, stdout, err = _run(monkeypatch, capsys, name, "1", "--project", "proj-a", *argv)
    assert code == 0, err
    if name == "export":
        assert json.loads(stdout)["cycle"]["id"] == coded
    assert export_triage(cli_engine, matrix["first"])["cards"] == []


@pytest.mark.parametrize("name", COMMANDS)
def test_a_code_outside_a_registered_project_needs_project(monkeypatch, capsys, matrix,
                                                           template, tmp_path, name):
    elsewhere = tmp_path.parent / f"{tmp_path.name}-unregistered"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code, _o, err = _run(monkeypatch, capsys, name, "rev-m", *_command(name, tmp_path, template))
    assert code == 1 and "Cannot detect project" in err


def test_config_get_prints_an_unset_template_as_empty(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DCT_CONFIG_PATH", str(tmp_path / "absent.toml"))
    run_cli(monkeypatch, ["config", "get", "triage.template"])
    assert capsys.readouterr().out == "\n"
