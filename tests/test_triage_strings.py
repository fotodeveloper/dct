"""#991 — the triage string table and the template dct ships.

Contract under test (spec docs/superpowers/specs/2026-10-21-default-triage-template-design.md):
- the table (dct/assets/triage-strings.json) is validated whole: the same keys
  in every language, no empty text, the required keys, the decision-text
  words consistent, and no spelling that means two things to the parser once
  normalized — across languages, the canonical tokens and the no-decision
  label (R-3); a spelling shared with the SAME meaning is fine;
- the parser reads every language: Polish and English texts give the same
  decisions, lines may mix languages, the note starts at the EARLIEST note
  separator of any language;
- the shipped template holds no hard-coded user-visible text (text nodes,
  aria-label / placeholder / title, CSS content — R-2), reads STRINGS only
  through tx(), and every text helper escapes a hostile table value (R-1);
- the package carries the template and the table (R-6).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

from dct.workflows import triage_strings as ts
from dct.workflows.triage import parse_decision_batch
from dct.workflows.triage_page import SHIPPED_TEMPLATE, build_triage_page

ROOT = Path(__file__).resolve().parent.parent
CARDS = [{"id": "K-1", "opts": [{"k": "a"}, {"k": "b"}]}, {"id": "K-2", "opts": []}]


def _table() -> dict:
    return copy.deepcopy(json.loads(ts.TABLE_PATH.read_text(encoding="utf-8")))


def _write(tmp_path, data) -> Path:
    path = tmp_path / "strings.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ── the table ────────────────────────────────────────────────────────────────


def test_the_shipped_table_is_valid_english_first_and_symmetric():
    table = ts.load_table()
    assert ts.languages() == ("en", "pl")
    assert set(table["en"]) == set(table["pl"])
    assert all(k in table["en"] for k in ts.REQUIRED_KEYS)


def _missing_key(t):
    del t["pl"]["card.context"]


def _empty_text(t):
    t["en"]["toast.copied"] = "  "


def _required_key(t):
    for lang in t.values():
        del lang["text.notes"]


def _prefill(t):
    t["en"]["text.prefill"] = "Seeded: …"


def _accept_tag(t):
    t["pl"]["response.accept_tag"] = "[przyjęta]"


def _cross_language(t):
    t["pl"]["decision.note"] = "FIX  now"  # en decision.fix, normalized


def _canonical_token(t):
    t["en"]["decision.note"] = "verify"


def _no_decision(t):
    t["en"]["decision.none"] = "Note"  # en decision.note


def _separator_forms(t):
    t["pl"]["decision.discuss"] = "Fix -> now"
    t["en"]["decision.fix"] = "fix → now"


def _notes_word(t):
    t["en"]["text.notes"] = "notes:"


def _bad_code(t):
    t["EN!"] = t.pop("pl")


def _no_default(t):
    del t["en"]


@pytest.mark.parametrize(("mutate", "message"), [
    (_missing_key, "pl differs from the other languages — missing ['card.context']"),
    (_empty_text, "en: toast.copied is empty"),
    (_required_key, "required keys missing: text.notes"),
    (_prefill, "text.prefill must start with text.prefill_prefix"),
    (_accept_tag, "response.accept_tag needs {by}"),
    (_cross_language, "pl: decision.note"),
    (_canonical_token, "en: decision.note"),
    (_no_decision, "decision.none"),
    (_separator_forms, "means something else"),
    (_notes_word, "text.notes may not contain"),
    (_bad_code, "is not a language code"),
    (_no_default, "default language 'en' is missing"),
])
def test_a_malformed_table_is_refused_naming_the_problem(tmp_path, mutate, message):
    data = _table()
    mutate(data)
    with pytest.raises(ts.StringTableError, match=re.escape(message)):
        ts.load_table(_write(tmp_path, data))


def test_a_spelling_shared_with_the_same_meaning_is_allowed(tmp_path):
    data = _table()
    data["pl"]["decision.verify"] = "VERIFY"  # = the en label and the token, same meaning
    assert ts.load_table(_write(tmp_path, data))["pl"]["decision.verify"] == "VERIFY"


@pytest.mark.parametrize("raw", ["en", " PL ", "pl"])
def test_a_shipped_language_is_accepted(raw):
    assert ts.check_language(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw", ["xx", "", None, 5, "en-US"])
def test_an_unknown_language_is_refused_naming_the_shipped_ones(raw):
    with pytest.raises(ValueError, match=r"unknown language .* shipped: en, pl"):
        ts.check_language(raw)


def test_fmt_fills_named_parameters_only():
    assert ts.fmt("{n} of {total} {missing} {0} {a.b}", n=1, total=2) == \
        "1 of 2 {missing} {0} {a.b}"


@pytest.mark.parametrize(("lang", "n", "word"), [
    ("pl", 1, "karta"), ("pl", 2, "karty"), ("pl", 4, "karty"), ("pl", 5, "kart"),
    ("pl", 12, "kart"), ("pl", 14, "kart"), ("pl", 22, "karty"), ("pl", 0, "kart"),
    ("en", 1, "card"), ("en", 2, "cards"), ("en", 0, "cards"),
])
def test_the_lede_counts_cards_in_the_cycles_language(lang, n, word):
    assert ts.lede(lang, n, 2, "rev-1").startswith(f"{n} {word} · rubric v2 · ")


# ── the parser reads every language ──────────────────────────────────────────

POLISH = ("Triage X (p) — decyzje ownera (2026-10-21 10:00), rubric v2:\n"
          "Punkt wyjścia: strona z propozycjami Codex — to nie są decyzje.\n"
          "K-1: Odrzuć / zaakceptuj stan · wariant (b) [inny niż rekomendacja] · uwagi: nie\n"
          "K-2: — (brak decyzji)\n")
ENGLISH = ("Triage X (p) — owner decisions (2026-10-21 10:00), rubric v2:\n"
           "Starting point: a page with proposals by Codex — these are not decisions.\n"
           "K-1: Reject / accept as is · variant (b) [not the recommended variant] · notes: nie\n"
           "K-2: — (no decision)\n")


def test_polish_and_english_texts_give_the_same_decisions():
    pl = parse_decision_batch(POLISH, cards=CARDS)
    en = parse_decision_batch(ENGLISH, cards=CARDS)
    assert pl["decisions"] == en["decisions"] == {
        "K-1": {"dec": "reject", "opt": "b", "note": "nie"}}
    assert pl["skipped"] == en["skipped"] == ["K-2"]
    assert pl["prefill_line"].startswith("Punkt wyjścia:")
    assert en["prefill_line"].startswith("Starting point:")


def test_lines_may_mix_languages_and_the_earliest_note_separator_wins():
    text = ("K-1: Fix now · wariant (a) · notes: a · uwagi: b · notes: c\n"
            "K-2: Do dyskusji · uwagi: x · notes: y\n")
    out = parse_decision_batch(text, cards=CARDS)["decisions"]
    assert out["K-1"] == {"dec": "fix", "opt": "a", "note": "a · uwagi: b · notes: c"}
    assert out["K-2"] == {"dec": "discuss", "opt": None, "note": "x · notes: y"}


@pytest.mark.parametrize(("line", "word"), [("K-1: fix · wariant (z)", "wariant"),
                                            ("K-1: fix · Variant (z)", "Variant")])
def test_an_unknown_variant_is_named_with_the_texts_own_word(line, word):
    with pytest.raises(ValueError, match=rf"{word} \(z\) is not an option of K-1"):
        parse_decision_batch(line, cards=CARDS)


def test_an_unknown_label_lists_the_labels_of_every_language():
    with pytest.raises(ValueError, match="Fix now.*Fix teraz.*fix"):
        parse_decision_batch("K-1: Fix maybe", cards=CARDS)


# ── the shipped template ─────────────────────────────────────────────────────


def _shipped() -> str:
    return SHIPPED_TEMPLATE.read_text(encoding="utf-8")


def _script(page: str) -> str:
    start = page.index("<script>\n(function")
    return page[start:page.index("</script>", start)]


class _Visible(HTMLParser):
    """Text nodes and attributes outside <script>, <style> and comments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.texts: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        self.attrs.extend((tag, k, v or "") for k, v in attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.texts.append(data.strip())


def test_the_template_has_no_hard_coded_visible_text():
    """R-2: every visible text comes from STRINGS. Allowed: placeholders,
    punctuation, digits, the product word 'Triage' and the rubric letters."""
    parser = _Visible()
    parser.feed(_shipped())
    for text in parser.texts:
        rest = re.sub(r"\{\{[^{}]*\}\}|\bTriage\b|S O E K", "", text)
        assert not re.search(r"[^\W\d_]", rest), f"hard-coded text: {text!r}"
    for tag, name, value in parser.attrs:
        assert name not in ("aria-label", "placeholder", "title", "alt"), (tag, name, value)
    css = _shipped()[_shipped().index("<style>"):_shipped().index("</style>")]
    for content in re.findall(r"(?<![-\w])content:\s*([^;]+);", css):
        assert content.startswith("attr(") or not re.search(r"[^\W\d_]", content), content


def test_no_table_text_is_copied_into_the_template():
    page = re.sub(r"<!--.*?-->", "", _shipped(), flags=re.DOTALL)  # the contract comment
    page = re.sub(r"^\s*//.*$", "", page, flags=re.MULTILINE)  # script comments
    for lang, texts in ts.table().items():
        for key, text in texts.items():
            if len(text) >= 12 and "{" not in text:
                assert text not in page, f"{lang}:{key} is hard-coded in the template"
    assert not re.search(r"[ąćęłńóśźż]", page, re.IGNORECASE)


def test_strings_are_read_only_through_tx_and_never_concatenated_raw_into_html():
    """R-1 (static half): STRINGS is read in one place; a line that builds an
    HTML string never calls tx() — it uses th() — and wraps every label it
    takes from decLabel() / classLabel() in escapeHtml() or passes it as a
    th() parameter (escaped with the whole text)."""
    script = _script(_shipped())
    code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("//")]
    reads = [ln for ln in code if re.search(r"STRINGS\s*[\[.,)]", ln)]
    assert len(reads) == 2, reads  # tx()'s lookup and the generate() input
    for line in code:
        if "'<" not in line:
            continue
        assert "tx(" not in line, line
        for m in re.finditer(r"(decLabel|classLabel)\(", line):
            before = line[:m.start()]
            assert before.endswith(("escapeHtml(", "cls: ")), line


HOSTILE = "</script><b onmouseover=\"x\">'&' {n} **bold** `code` <i>"


@pytest.mark.slow
def test_every_text_helper_escapes_a_hostile_table_value(tmp_path):
    """R-1 (dynamic half): the helpers the page uses, run from the shipped
    file with a hostile table, emit no markup but their own."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    page = _shipped()

    def region(name: str) -> str:
        return page[page.index(f"// <{name}>"):page.index(f"// </{name}>")]

    script = tmp_path / "helpers.js"
    script.write_text(
        region("decision-text") + f"\nconst STRINGS = {json.dumps({'k': HOSTILE})};\n"
        + region("text-helpers") + "\nprocess.stdout.write(JSON.stringify("
        "{tx: tx('k', {n: 1}), th: th('k', {n: '<x>'}), rich: richHtml(tx('k')),"
        " text: textHtml(tx('k')), missing: th('<none>')}));\n", encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["tx"] == HOSTILE.replace("{n}", "1")  # plain text, for textContent only
    for key in ("th", "rich", "text", "missing"):
        markup = re.findall(r"<[^>]*>", out[key])
        assert set(markup) <= {"<b>", "</b>", "<code>", "</code>"}, (key, out[key])
        assert '"' not in out[key] and "'" not in out[key], (key, out[key])
    assert "&lt;x&gt;" in out["th"]  # a parameter is escaped with the text
    assert "<b>bold</b>" in out["rich"] and "<code>code</code>" in out["text"]


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["en", "pl"])
def test_the_shipped_template_builds_and_its_script_parses(tmp_path, lang):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    export = {"cycle": {"id": 3, "project": "proj-a", "code": "rev-1", "topic": "T",
                        "scope": None, "rubric_version": 2, "language": lang,
                        "opened_at": "2026-10-21T08:00:00"},
              "cards": [], "closed": None, "suggested": None}
    page = build_triage_page(export, _shipped())
    assert "triage-proj-a-rev-1-c3-v1" in page
    script = tmp_path / "page.js"
    script.write_text(_script(page).removeprefix("<script>"), encoding="utf-8")
    check = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                           check=False)
    assert check.returncode == 0, check.stderr


def test_an_override_without_a_string_slot_is_refused_with_the_upgrade_path():
    """R-4: a template written before #991 builds no more; the refusal names
    both ways out."""
    old = _shipped().replace("  const STRINGS = {};\n", "")
    export = {"cycle": {"id": 1, "project": "p", "code": "c", "topic": "t", "rubric_version": 2,
                        "language": "pl", "opened_at": "2026-10-21"},
              "cards": [], "closed": None, "suggested": None}
    with pytest.raises(ValueError, match=r"const STRINGS.*\[triage\] template.*--template"):
        build_triage_page(export, old)


# ── the package (R-6) ────────────────────────────────────────────────────────


def test_package_data_covers_the_shipped_template_and_table():
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["package-data"]["dct"]
    for asset in (SHIPPED_TEMPLATE, ts.TABLE_PATH):
        relative = asset.relative_to(ROOT / "dct").as_posix()
        assert any(fnmatch.fnmatch(relative, p) for p in patterns), relative


@pytest.mark.slow
def test_a_built_wheel_renders_a_page_outside_the_checkout(tmp_path):
    """The installed package finds its template and table: a wheel is built,
    unpacked away from the checkout and used to build a page there."""
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True,
                           check=False)
    if probe.returncode != 0:
        pytest.skip("pip not available to build a wheel")
    dist = tmp_path / "dist"
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(ROOT)],
        capture_output=True, text=True, check=False)
    if build.returncode != 0:
        pytest.skip(f"cannot build a wheel here: {build.stderr.strip()[-200:]}")
    wheel = next(dist.glob("*.whl"))
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert {"dct/assets/triage-page.html", "dct/assets/triage-strings.json"} <= names
        zf.extractall(site)
    code = (
        "from dct.workflows.triage_page import SHIPPED_TEMPLATE, build_triage_page\n"
        "e = {'cycle': {'id': 1, 'project': 'p', 'code': 'c', 'topic': 't',"
        " 'rubric_version': 2, 'language': 'pl', 'opened_at': '2026-10-21'},"
        " 'cards': [], 'closed': None, 'suggested': None}\n"
        "page = build_triage_page(e, SHIPPED_TEMPLATE.read_text(encoding='utf-8'))\n"
        f"assert str(SHIPPED_TEMPLATE).startswith({str(site)!r}), SHIPPED_TEMPLATE\n"
        "assert 'Otwarty' in page\n")
    run = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                         text=True, check=False, env={"PYTHONPATH": str(site), "PATH": ""})
    assert run.returncode == 0, run.stderr
