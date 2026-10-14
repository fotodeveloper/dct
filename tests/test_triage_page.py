"""#930 — the triage page builder (dct/workflows/triage_page.py).

Contract under test (spec #930 §2, §8 K-3 / K-4 / K-5):
- the three constants CARDS / CLOSED / SUGGESTED are located in the ORIGINAL
  template, at code level of its <script> (never inside a string, comment,
  regex literal or HTML comment), exactly one declaration each with a
  `null` / [...] / {...} initializer ending in `;` — anything else refused;
  they are replaced by page_constants_json of the export, which reads back
  exactly (round trip) and holds no `</` or `<!--`;
- placeholders are validated in the original template only: an unknown one
  is refused, while ledger data quoting `{{TEMAT}}`, `{{UNKNOWN}}` or
  `const CLOSED = …` stays literal (inserted data is never re-scanned);
- metadata is plain text: HTML-escaped in markup (kept as given), JS-string-
  escaped inside a quoted script string (refused in script code, comments,
  regex and template literals — K-39), reading back exactly;
- K-33: inside a script string whitespace and line separators collapse to
  one space at build time, so the generated decision text keeps a one-line
  header the parser skips (producer → parser, LF / CRLF / Unicode);
- STORAGE_KEY = triage-<project slug>-<code slug>-c<cycle id>-v1 (K-34:
  injective through the cycle id at the end);
- the page's own generate() (a verbatim copy of the template's, run in
  node) produces the text decide_findings_from_text reads: skipped cards
  (K-31) and the prefill line (K-40).
- deterministic defaults (topic, project, context, date, lede).
The template here is a minimal skeleton of the owner's page (the real one is
private and not shipped): the same constants, placeholders and generated
header line, plus deliberate traps (a regex literal with quotes, declaration
text inside strings and comments, an HTML comment).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
import sqlalchemy as sa

from dct.core.projects import create_project
from dct.db import findings
from dct.workflows.triage import (
    close_triage,
    decide_findings,
    decide_findings_from_text,
    export_triage,
    open_triage,
    parse_decision_batch,
    record_findings,
    suggest_findings,
)
from dct.workflows.triage_page import (
    LEDE_LEGEND,
    build_triage_page,
    default_lede,
    storage_key_parts,
)

SKELETON = """<!doctype html>
<meta charset="utf-8">
<!--
  Skeleton of the triage page. Fill CARDS; a <script> mentioned here and
  const CLOSED = 1; in this comment are not code.
-->
<title>Triage {{TEMAT}}</title>
<div class="eyebrow">{{PROJEKT}} · {{KONTEKST}} · {{DATA}}</div>
<h1>Triage {{TEMAT}}</h1>
<p class="lede">{{LEDE — co było sprawdzane, ile punktów}}</p>
<!-- optional: <section> sources </section> -->
<script>
'use strict';
const STORAGE_KEY = 'triage-{{projekt}}-{{temat}}-v1';
// null while open; const CLOSED = { … } once decided (a comment, not code)
const CLOSED = null;
/* const SUGGESTED = 'block comment'; */
const SUGGESTED = null;
const NOTE = "const CARDS = []; is only text here";
const DECISIONS = [
  { v: 'fix', label: 'Fix teraz' },
];
const CARDS = [
  {
    id: 'K-1', title: '{{Tytuł = skutek}}', area: '{{plik:linia}}', src: '{{panel | codex}}',
    S: 2, O: 1, E: 2, K: 'S', origin: 'diff', type: 'correctness',
    fp: '{{<niezmiennik> naruszany na <granicy>}}', ctx: [ '{{Kontekst; }}' ],
    opts: [ { k: 'a', rec: true, root: true, t: '{{Wariant [a]}}', why: '{{Dlaczego}}' } ],
  },
];
function escapeHtml(t) { return String(t || '').replace(/[&<>"']/g, function (ch) { return ch; }); }
function ratio(a, b) { return Math.round(100 * a / b) + '%'; }
function header(stamp) {
  return 'Triage {{TEMAT}} ({{projekt}}) — decyzje ownera (' + stamp + '), rubric v2:';
}
</script>
"""

HOSTILE = "</script><script>alert(1)</script> <!-- &     {{TEMAT}} const CLOSED = 1;"


def _export(**over):
    cycle = {"id": 7, "project": "proj-a", "code": "rev-1", "topic": "review of X",
             "scope": None, "base_commit": None, "rubric_version": 2, "artifact_url": None,
             "created_by": "me", "opened_at": "2026-10-12T08:00:00", "closed_at": None}
    cycle.update(over.pop("cycle", {}))
    card = {"id": "K-1", "title": "T", "area": "", "src": "codex", "S": 2, "O": 1, "E": 2,
            "K": "S", "origin": "diff", "type": "correctness", "fp": "fp", "ctx": [],
            "opts": [{"k": "a", "rec": True, "root": True, "t": "fix it", "why": ""}]}
    out = {"cycle": cycle, "cards": [card], "closed": None, "suggested": None}
    out.update(over)
    return out


def _constant(page: str, name: str):
    """The value of `const NAME = <json>;` in the built page, parsed back."""
    starts = [m.end() for m in re.finditer(rf"^const {name} = ", page, re.MULTILINE)]
    assert len(starts) == 1, (name, len(starts))
    value, end = json.JSONDecoder().raw_decode(page, starts[0])
    assert page[end] == ";"
    return value


def _script(page: str) -> str:
    start = page.rindex("<script>") + len("<script>")
    return page[start:page.rindex("</script>")]


# ── the constants ────────────────────────────────────────────────────────────


def test_the_three_constants_carry_the_export_and_read_back_exactly():
    export = _export(
        closed={"date": "2026-10-14", "source": "chat", "rubric": 2, "notes": ["n"],
                "answers": {"K-1": {"dec": "fix", "opt": "a", "note": HOSTILE}}},
        suggested={"by": "Codex", "date": "2026-10-13",
                   "answers": {"K-1": {"dec": "fix", "opt": "", "note": ""}}},
    )
    export["cards"][0]["title"] = HOSTILE
    page = build_triage_page(export, SKELETON)
    assert _constant(page, "CARDS") == export["cards"]
    assert _constant(page, "CLOSED") == export["closed"]
    assert _constant(page, "SUGGESTED") == export["suggested"]


def test_absent_closed_and_suggested_are_null():
    page = build_triage_page(_export(), SKELETON)
    assert _constant(page, "CLOSED") is None and _constant(page, "SUGGESTED") is None


def test_hostile_data_cannot_close_the_script_or_open_markup():
    export = _export()
    export["cards"][0]["ctx"] = [HOSTILE]
    page = build_triage_page(export, SKELETON)
    script = _script(page)
    assert "</" not in script and "<!--" not in script
    assert page.count("</script>") == SKELETON.count("</script>")


def test_ledger_text_quoting_placeholders_or_declarations_stays_literal():
    """K-3: inserted data is never re-scanned — not as a placeholder (a
    {{TEMAT}} or an unknown {{…}} in a finding is NOT substituted or
    refused) and not as a declaration (a finding quoting `const CLOSED`)."""
    export = _export()
    export["cards"][0]["title"] = "{{TEMAT}} {{UNKNOWN}} {{LEDE x}}"
    export["cards"][0]["why"] = "const CLOSED = null; const CARDS = [];"
    page = build_triage_page(export, SKELETON, topic="REPLACED")
    assert _constant(page, "CARDS")[0]["title"] == "{{TEMAT}} {{UNKNOWN}} {{LEDE x}}"
    assert _constant(page, "CARDS")[0]["why"] == "const CLOSED = null; const CARDS = [];"


def test_text_around_the_declarations_is_kept_verbatim():
    page = build_triage_page(_export(), SKELETON)
    for kept in ("const CLOSED = 1; in this comment are not code",
                 "// null while open; const CLOSED = { … } once decided",
                 "/* const SUGGESTED = 'block comment'; */",
                 'const NOTE = "const CARDS = []; is only text here";',
                 "replace(/[&<>\"']/g", "Math.round(100 * a / b)"):
        assert kept in page, kept


# ── refusals: the template ───────────────────────────────────────────────────


def _without(text: str, needle: str, replacement: str = "") -> str:
    assert needle in text, needle
    return text.replace(needle, replacement, 1)


@pytest.mark.parametrize(("template", "match"), [
    (_without(SKELETON, "const CLOSED = null;\n"), "no 'const CLOSED"),
    (_without(SKELETON, "const SUGGESTED = null;\n"), "no 'const SUGGESTED"),
    (SKELETON.replace("const CARDS = [\n", "const CARDZ = [\n", 1), "no 'const CARDS"),
    # only mentioned inside a string / comment → not a declaration
    (_without(SKELETON, "const CLOSED = null;\n",
              "const X = 'const CLOSED = null;';\n"), "no 'const CLOSED"),
    (SKELETON.replace("<script>\n'use", "<!-- <script>\n'use", 1).replace(
        "</script>\n", "</script> -->\n", 1), "no 'const"),
    (SKELETON.replace("const SUGGESTED = null;", "const SUGGESTED = null;\nconst SUGGESTED = {};", 1),
     "'const SUGGESTED' is declared 2 times"),
    (SKELETON + "<script>const CLOSED = null;</script>\n", "'const CLOSED' is declared 2 times"),
    (SKELETON.replace("const CLOSED = null;", "const CLOSED = makeClosed();", 1),
     "const CLOSED: unsupported initializer"),
    (SKELETON.replace("const CLOSED = null;", "const CLOSED = 'null';", 1),
     "const CLOSED: unsupported initializer"),
    (SKELETON.replace("const CLOSED = null;", "const CLOSED = nullish;", 1),
     "const CLOSED: unsupported initializer"),
    (SKELETON.replace("const CLOSED = null;", "const CLOSED = null", 1),
     "const CLOSED: its initializer must end with ';'"),
    (SKELETON.replace("const CLOSED = null;", "const CLOSED = { a: [1 };", 1),
     "unbalanced"),
    (SKELETON.replace("];\nfunction escapeHtml", "\nfunction escapeHtml", 1),
     "const CARDS"),
    (SKELETON.replace("const NOTE = \"const", "const NOTE = \"\nconst", 1),
     "never closed on its line"),
    (SKELETON.replace("/* const SUGGESTED", "/* const SUGGESTED */ /*", 1).replace(
        "comment'; */", "comment';", 1), "never closed"),
    (SKELETON.replace("</script>", "", 1), "<script> element is never closed"),
    (SKELETON.replace("<!-- optional: <section> sources </section> -->", "<!-- optional", 1),
     "HTML comment is never closed"),
])
def test_a_template_outside_the_contract_is_refused(template, match):
    with pytest.raises(ValueError, match=match):
        build_triage_page(_export(), template)


@pytest.mark.parametrize(("template", "match"), [
    (SKELETON.replace("{{DATA}}", "{{DATE}}", 1), "unknown placeholder '{{DATE}}'"),
    (SKELETON.replace("{{KONTEKST}}", "{{ TEMAT }}", 1), "unknown placeholder '{{ TEMAT }}'"),
    (SKELETON.replace("'use strict';", "'use strict'; const Q = '{{x}}';", 1),
     "unknown placeholder '{{x}}'"),
    (SKELETON.replace("{{LEDE", "{{LEDEX", 1), "unknown placeholder '{{LEDEX"),
    (SKELETON.replace("'use strict';", "'use strict'; const T = {{TEMAT}};", 1),
     "sits in script code"),
    # K-39: only a quoted string literal is a supported position — a value
    # in a regex (`/{{TEMAT}}/` with "a/b" gives `const rx = /a/b/`) or a
    # template literal would change the code; a comment is no string either
    (SKELETON.replace("'use strict';", "'use strict'; const rx = /{{TEMAT}}/;", 1),
     "sits in a regex or template literal"),
    (SKELETON.replace("'use strict';", "'use strict'; const rx = /[{{TEMAT}}]+/g;", 1),
     "sits in a regex or template literal"),
    (SKELETON.replace("'use strict';", "'use strict'; const t = `{{TEMAT}}`;", 1),
     "sits in a regex or template literal"),
    (SKELETON.replace("'use strict';", "'use strict'; const t = `${1} {{PROJEKT}}`;", 1),
     r"line 13: a template literal holds a '\$\{' substitution"),
    (SKELETON.replace("'use strict';", "'use strict'; // {{TEMAT}}", 1),
     "sits in script code or a comment"),
    (SKELETON.replace("'use strict';", "'use strict'; /* {{DATA}} */", 1),
     "sits in script code or a comment"),
])
def test_an_unknown_or_misplaced_placeholder_is_refused(template, match):
    with pytest.raises(ValueError, match=re.escape(match) if "{{" in match else match):
        build_triage_page(_export(), template)


# ── regex or division: the token before a `/` (K-42 / K-43) ────────────────


def _with(code: str) -> str:
    return SKELETON.replace("'use strict';", "'use strict'; " + code, 1)


# The reviewer's probe (K-42): a `)` closing an `if (…)` head is followed by
# a statement, so `/x'{{TEMAT}}'/` is a regex — it was read as division
# and its quotes as a string, and the topic `a/ + alert(1) + /b` closed the
# regex and ran code.
PROBE = _with("const ok=(function(){ if (true) /x'{{TEMAT}}'/.test('y'); return 1; })();")


@pytest.mark.parametrize("topic", ["a/ + alert(1) + /b", "innocent"])
def test_a_placeholder_in_a_regex_after_a_control_head_is_refused_for_any_topic(topic):
    with pytest.raises(ValueError, match="sits in a regex or template literal"):
        build_triage_page(_export(), PROBE, topic=topic)


@pytest.mark.parametrize("head", [
    "if (x)", "while (x)", "for (;;)", "for (const k of [1])", "with (o)",
    "for await (const k of g)", "if ((x))", "if (f(x))", "if (x) {} else",
    "do x(); while (x)",
])
def test_every_control_head_is_followed_by_a_regex(head):
    with pytest.raises(ValueError, match="sits in a regex or template literal"):
        build_triage_page(_export(), _with(f"{head} /'{{{{TEMAT}}}}'/.test(s);"))


@pytest.mark.parametrize("code", [
    "const q = (4) / 2; const D = '{{TEMAT}}';",
    "const q = f(1) / g('/') / 2; const D = '{{TEMAT}}';",
    "const q = o.if(1) / 2; const D = '{{TEMAT}}';",  # a property named `if`
    "const q = a[0] / 2 / x; const D = '{{TEMAT}}';",
    "const q = (a) / '{{TEMAT}}' / 2;",  # division: the quotes are a string
])
def test_division_after_a_call_or_a_grouping_still_builds(code):
    page = build_triage_page(_export(), _with(code), topic="a/b")
    assert "a\\u002fb" in page


@pytest.mark.parametrize(("code", "line"), [
    ("const q = {} / 2;", 13),
    ("if (x) {}\n/re/.test(s);", 14),
    ("const q = a./x/;", 13),
])
def test_a_slash_the_scan_cannot_classify_is_refused_naming_its_line(code, line):
    with pytest.raises(ValueError, match=rf"line {line}: cannot tell whether the '/'"):
        build_triage_page(_export(), _with(code))


# K-46: `of`, `await` and `yield` are keywords in some positions and plain
# identifiers in others, so a `/` after one may start a regex or divide. The
# reviewer's probe: `of / 2; var s = "/` was read as a regex, the `"` after
# it opened a "string" around `{{TEMAT}}` — which sits in code — and the
# topic `globalThis.PWNED=1` ran.
OF_PROBE = _with('var of = 4; var z = of / 2; var s = "/"; foo({{TEMAT}}); // "')


@pytest.mark.parametrize("topic", ["globalThis.PWNED=1", "innocent"])
def test_a_slash_after_of_is_refused_for_any_topic(topic):
    with pytest.raises(ValueError, match=r"line 13: cannot tell whether the '/' after 'of'"):
        build_triage_page(_export(), OF_PROBE, topic=topic)


@pytest.mark.parametrize(("code", "after"), [
    ("var z = await / 2;", "await"),
    ("var z = yield / 2;", "yield"),
    ("for (const x of /re/.source) f(x);", "of"),  # the keyword: refused all the same
    ("var of = 4; var z = of++ / 2;", r"of\+\+"),  # through the postfix path
    ("var z = await-- / 2;", "await--"),
    ("var z = yield /* c */ ++ / 2;", r"yield\+\+"),
])
def test_a_slash_after_a_word_that_may_be_an_identifier_is_refused(code, after):
    with pytest.raises(ValueError, match=rf"line 13: cannot tell whether the '/' after '{after}'"):
        build_triage_page(_export(), _with(code + " const D = '{{TEMAT}}';"))


@pytest.mark.parametrize("code", [
    # Division: read as a regex, `/ 2; const s = '/` would end the literal
    # and `{{TEMAT}}` would sit in code.
    "const q = o.of / 2; const s = '/'; const D = '{{TEMAT}}';",
    "const q = o.await++ / 2; const s = '/'; const D = '{{TEMAT}}';",
    # A regex: read as division, `'/; const D = '` would be the string.
    "function f() { return /'/; } const D = '{{TEMAT}}';",
    "const t = typeof /'/; const D = '{{TEMAT}}';",
    "of\n++/'/.lastIndex; const D = '{{TEMAT}}';",  # ASI: `of; ++/'/…`
])
def test_a_property_named_of_divides_and_real_keywords_still_start_a_regex(code):
    page = build_triage_page(_export(), _with(code), topic="a/b")
    assert "a\\u002fb" in page


# K-47: the scan does not follow the code of a `${…}` substitution — the
# reviewer's probe: the backtick inside `${"`"}` ended the literal early,
# `{{TEMAT}}` became a "string" and the topic `globalThis.PWNED=1` ran.
TEMPLATE_PROBE = _with('const t = `${"`"}`; foo({{TEMAT}}); // "')


@pytest.mark.parametrize("topic", ["globalThis.PWNED=1", "innocent"])
def test_a_template_literal_with_a_substitution_is_refused_for_any_topic(topic):
    with pytest.raises(ValueError, match=r"line 13: a template literal holds a '\$\{' substitution"):
        build_triage_page(_export(), TEMPLATE_PROBE, topic=topic)


@pytest.mark.parametrize(("code", "line"), [
    ("const t = `${x}`;", 13),
    ("const t = `a\n b ${x}`;", 14),
    ("const t = `\\\\${x}`;", 13),  # `\\${`: the backslash is escaped, `${` is not
    ("const t = `\\${a} ${b}`;", 13),  # the second one is real
])
def test_an_unescaped_substitution_is_refused_naming_its_line(code, line):
    with pytest.raises(ValueError, match=rf"line {line}: a template literal holds a '\$\{{' substitution"):
        build_triage_page(_export(), _with(code + " const D = '{{TEMAT}}';"))


@pytest.mark.parametrize("code", [
    "const t = `'`;",  # a plain template literal: its quote is no string
    "const t = `a\n'b' $ {x} $`;",
    "const t = `\\${'`;",  # `\${` is plain text
    "const t = `$\\{'`;",
])
def test_a_template_literal_without_a_substitution_still_builds(code):
    page = build_triage_page(_export(), _with(code + " const D = '{{TEMAT}}';"), topic="a/b")
    assert "a\\u002fb" in page


def test_an_unbalanced_closing_paren_is_refused_naming_its_line():
    with pytest.raises(ValueError, match=r"line 13: unbalanced '\)'"):
        build_triage_page(_export(), _with("x()); const D = '{{TEMAT}}';"))


# K-43: a postfix ++ / -- (right after an operand, same line) is followed by
# division; a prefix one (after a line break or an operator) by an operand.
POSTFIX = [
    "let i = 6; const q1 = i++ / 2;",
    "let i = 6; const q1 = i-- / 2;",
    "let i = 6; const q1 = (i)++ / 2;",
    "const arr = [6]; const q1 = arr[0]++ / 2;",
    "let i = 6; const q1 = i /* c */ ++ / 2;",
]


@pytest.mark.parametrize("code", POSTFIX)
def test_division_after_a_postfix_increment_builds(code):
    build_triage_page(_export(), _with(code + " const D = '{{TEMAT}}';"))


@pytest.mark.parametrize("code", [
    "let i = 1; const q = i\n++/'{{TEMAT}}'/.lastIndex;",  # ASI: `i; ++/…/`
    "let i = 1; const q = i /*\n*/ ++/'{{TEMAT}}'/.lastIndex;",  # a comment's line break
    "const q = 1 + ++/'{{TEMAT}}'/.lastIndex;",  # prefix after an operator
    "const q = + +/'{{TEMAT}}'/.source.length;",  # two unary operators
    "let a = 1; const q = a +++/'{{TEMAT}}'/.lastIndex;",  # `a ++ +`, then a regex
])
def test_a_prefix_increment_or_unary_operators_are_followed_by_a_regex(code):
    with pytest.raises(ValueError, match="sits in a regex or template literal"):
        build_triage_page(_export(), _with(code))


def test_placeholders_inside_the_cards_example_are_replaced_with_it():
    """The template's example CARDS hold {{…}} hints; the whole declaration
    is replaced, so they are neither validated nor left in the page."""
    page = build_triage_page(_export(), SKELETON)
    assert "{{" not in page.replace(_script(page), "")
    assert "Tytuł = skutek" not in page


def test_an_export_without_its_cycle_or_cards_is_refused():
    for export in ({}, {"cycle": {}}, {"cards": []}, None):
        with pytest.raises(ValueError, match="export must be"):
            build_triage_page(export, SKELETON)


# ── metadata ─────────────────────────────────────────────────────────────────

TOPIC = "Orders <b>&</b> \"quotes\" 'single' \\ back $x `tick`\nline2"


def test_metadata_is_escaped_as_html_text_in_markup():
    page = build_triage_page(_export(), SKELETON, topic=TOPIC, project_slug="p<1>",
                             context="ctx & more", lede="<i>lede</i>", date="2026-10-14")
    escaped = ("Orders &lt;b&gt;&amp;&lt;/b&gt; &quot;quotes&quot; &#x27;single&#x27; "
               "\\ back $x `tick`\nline2")
    assert f"<title>Triage {escaped}</title>" in page
    assert f"<h1>Triage {escaped}</h1>" in page
    assert '<div class="eyebrow">p&lt;1&gt; · ctx &amp; more · 2026-10-14</div>' in page
    assert '<p class="lede">&lt;i&gt;lede&lt;/i&gt;</p>' in page


def test_metadata_inside_a_script_string_is_a_js_escape_that_cannot_break_out():
    page = build_triage_page(_export(), SKELETON, topic=TOPIC)
    line = next(ln for ln in _script(page).splitlines() if "decyzje ownera" in ln)
    assert line == (
        "  return 'Triage Orders \\u003cb\\u003e\\u0026\\u003c\\u002fb\\u003e \\u0022quotes\\u0022 "
        "\\u0027single\\u0027 \\\\ back \\u0024x \\u0060tick\\u0060 line2 (proj-a) "
        "— decyzje ownera (' + stamp + '), rubric v2:';")


def _js_string(literal: str) -> str:
    """The value of a JS string literal body the builder wrote: its only
    escapes are \\uXXXX and \\\\, so JSON reads it back exactly."""
    return json.loads(f'"{literal}"')


def test_a_double_quoted_script_string_is_supported_and_reads_back():
    template = SKELETON.replace("'use strict';", "'use strict'; const D = \"[{{DATA}}]\";", 1)
    page = build_triage_page(_export(), template, date="it's \"x\" </script>")
    literal = re.search(r'const D = "([^"]*)";', page).group(1)
    assert _js_string(literal) == "[it's \"x\" </script>]"


def _generated_header(page: str, stamp: str = "2026-10-14 09:30") -> str:
    """What the page's header(stamp) returns: its two string literals
    around the stamp, read back as JS would."""
    m = re.search(r"return '((?:[^'\\]|\\.)*)' \+ stamp \+ '((?:[^'\\]|\\.)*)';", page)
    return _js_string(m.group(1)) + stamp + _js_string(m.group(2))


SEPARATORS = ["\n", "\r\n", "\r", "\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c",
              "\t \n  "]


@pytest.mark.parametrize("sep", SEPARATORS)
@pytest.mark.parametrize("via", ["cycle topic", "--topic"])
def test_a_topic_over_several_lines_gives_a_one_line_header_the_parser_skips(sep, via):
    """K-33 (b), producer → parser: the header the page generates holds the
    topic on ONE line, so the pasted text parses; HTML keeps the topic as
    given and the export (the stored topic) is untouched."""
    topic = f"review{sep}of X{sep}"
    export = _export(cycle={"topic": topic})
    page = build_triage_page(export, SKELETON, **({"topic": topic} if via == "--topic" else {}))
    header = _generated_header(page)
    assert header == "Triage review of X (proj-a) — decyzje ownera (2026-10-14 09:30), rubric v2:"
    assert len(header.splitlines()) == 1
    batch = parse_decision_batch(f"{header}\nK-1: Fix teraz · wariant (a)", cards=export["cards"])
    assert batch["decisions"] == {"K-1": {"dec": "fix", "opt": "a", "note": None}}
    assert f"<h1>Triage {topic}</h1>" in page  # HTML context: as given
    assert export["cycle"]["topic"] == topic


def test_without_the_collapse_the_header_would_break_the_parser():
    """The guard the collapse removes: a topic line break inside the header
    leaves neither line a header."""
    with pytest.raises(ValueError, match="line 1: not a decision line"):
        parse_decision_batch("Triage review\nof X (proj-a) — decyzje ownera (s), rubric v2:\n"
                             "K-1: fix", cards=_export()["cards"])


@pytest.mark.parametrize("field", ["topic", "project_slug", "context", "lede", "date"])
def test_metadata_must_be_text(field):
    with pytest.raises(ValueError, match=f"{field} must be a string"):
        build_triage_page(_export(), SKELETON, **{field: 5})


def test_defaults_come_from_the_export_not_the_clock():
    page = build_triage_page(_export(cycle={"scope": None}), SKELETON)
    assert "<title>Triage review of X</title>" in page
    assert '<div class="eyebrow">proj-a · rev-1 · 2026-10-12</div>' in page
    assert f'<p class="lede">1 karta · rubric v2 · cykl rev-1. {LEDE_LEGEND}</p>' in page
    scoped = build_triage_page(_export(cycle={"scope": "abc..def"}), SKELETON)
    assert '<div class="eyebrow">proj-a · abc..def · 2026-10-12</div>' in scoped


def test_a_closed_cycle_is_dated_by_its_close():
    export = _export(closed={"date": "2026-10-14", "source": "chat", "rubric": 2,
                             "notes": [], "answers": {}})
    assert "· 2026-10-14</div>" in build_triage_page(export, SKELETON)


@pytest.mark.parametrize(("n", "noun"), [
    (0, "kart"), (1, "karta"), (2, "karty"), (4, "karty"), (5, "kart"), (12, "kart"),
    (14, "kart"), (22, "karty"), (25, "kart"), (112, "kart"),
])
def test_the_default_lede_counts_the_cards(n, noun):
    export = _export(cards=[{}] * n)
    assert default_lede(export).startswith(f"{n} {noun} · rubric v2 · cykl rev-1. ")


# ── STORAGE_KEY (K-5) ────────────────────────────────────────────────────────


def _storage_key(page: str) -> str:
    return re.search(r"const STORAGE_KEY = '([^']*)';", page).group(1)


def test_the_storage_key_names_project_cycle_id_and_code():
    assert _storage_key(build_triage_page(_export(), SKELETON)) == "triage-proj-a-rev-1-c7-v1"


def test_codes_that_fold_alike_get_different_keys_through_the_cycle_id():
    a = build_triage_page(_export(cycle={"id": 3, "code": "Rev-A"}), SKELETON)
    b = build_triage_page(_export(cycle={"id": 4, "code": "rev a"}), SKELETON)
    assert (_storage_key(a), _storage_key(b)) == ("triage-proj-a-rev-a-c3-v1",
                                                  "triage-proj-a-rev-a-c4-v1")


@pytest.mark.parametrize(("one", "other"), [
    # (project, cycle id, code): with the id in the middle both pairs gave
    # one key — "triage-p-c1-c2-x-v1" and "triage-p-c1-c2-v1"
    (("p", 1, "c2-x"), ("p-c1", 2, "x")),
    (("p", 1, "c2"), ("p-c1", 2, "!!!")),
])
def test_the_pairs_that_collided_with_the_id_in_the_middle_get_different_keys(one, other):
    """K-34: the immutable id at the unambiguous end — slugs may hold '-'
    and 'c<digits>', the id is always the last segment before -v1."""
    keys = [
        _storage_key(build_triage_page(
            _export(cycle={"project": project, "id": cycle_id, "code": code}), SKELETON))
        for project, cycle_id, code in (one, other)
    ]
    assert keys[0] != keys[1], keys
    for key, (_project, cycle_id, _code) in zip(keys, (one, other), strict=True):
        assert re.fullmatch(rf"triage-[a-z0-9-]+-c{cycle_id}-v1", key), key
    assert keys == (["triage-p-c2-x-c1-v1", "triage-p-c1-x-c2-v1"] if one[2] == "c2-x"
                    else ["triage-p-c2-c1-v1", "triage-p-c1-c2-v1"])


@pytest.mark.parametrize(("project", "code", "parts"), [
    ("proj-a", "Przegląd ŻÓŁW #2", {"projekt": "proj-a", "temat": "przeglad-zow-2-c7"}),
    ("proj-a", "!!!", {"projekt": "proj-a", "temat": "c7"}),
    ("My Project", "r1", {"projekt": "my-project", "temat": "r1-c7"}),
    ("łłł", "r1", {"projekt": "project", "temat": "r1-c7"}),
])
def test_storage_key_parts_are_slugs(project, code, parts):
    got = storage_key_parts(_export(cycle={"project": project, "code": code}))
    assert got == parts
    assert all(re.fullmatch(r"[a-z0-9-]+", v) for v in got.values())


# ── from the ledger ──────────────────────────────────────────────────────────


def test_a_page_built_from_a_real_export_round_trips_the_ledger(engine):
    """Bidirectional: what export_triage produces, the builder embeds and
    a reader parses back to the same values — open, then closed."""
    create_project(engine, "proj-a", "A")
    cycle_id = open_triage(engine, "proj-a", "rev-1", HOSTILE, "me")["cycle_id"]
    record_findings(engine, cycle_id, [{
        "title": HOSTILE, "fp": "x violated at y by z", "S": 2, "O": 2, "E": 2, "K": "S",
        "origin": "diff", "type": "correctness", "src": "codex", "reviewers": ["codex"],
        "opts": [{"k": "a", "t": "{{TEMAT}}", "rec": True, "root": True}],
    }])
    suggest_findings(engine, cycle_id, "Codex", {"K-1": {"dec": "fix", "note": HOSTILE}})
    open_page = build_triage_page(export_triage(engine, cycle_id), SKELETON)
    export = export_triage(engine, cycle_id)
    assert _constant(open_page, "CARDS") == export["cards"]
    assert _constant(open_page, "SUGGESTED") == export["suggested"]
    assert _constant(open_page, "CLOSED") is None
    assert _storage_key(open_page) == f"triage-proj-a-rev-1-c{cycle_id}-v1"

    decide_findings(engine, cycle_id, {"K-1": {"dec": "fix", "opt": "a", "note": HOSTILE}},
                    decided_by="owner", decision_source="chat")
    close_triage(engine, cycle_id, source="chat")
    closed = export_triage(engine, cycle_id)
    closed_page = build_triage_page(closed, SKELETON)
    assert _constant(closed_page, "CLOSED") == closed["closed"]
    assert "</" not in _script(closed_page)


# ── producer → parser: the page's own generate() (node) ─────────────────────

# A page around a VERBATIM copy of the template's generate() (the producer of
# the "Tekst do wklejenia"), its DECISIONS / CLASSES and its state helpers;
# only the class derivation (classify) and the DOM are stubbed. The state is
# set by the test, the output is what the owner pastes.
GENERATOR = """<!doctype html>
<meta charset="utf-8">
<title>Triage {{TEMAT}}</title>
<script>
(function () {
  'use strict';
  const STORAGE_KEY = 'triage-{{projekt}}-{{temat}}-v1';
  const RUBRIC_VERSION = 2;
  const CLOSED = null;
  const SUGGESTED = null;
  const DECISIONS = [
    { v: 'fix', label: 'Fix teraz' },
    { v: 'verify', label: 'Zweryfikuj' },
    { v: 'backlog', label: 'Backlog → dct' },
    { v: 'reject', label: 'Odrzuć / zaakceptuj stan' },
    { v: 'note', label: 'Odnotuj' },
    { v: 'discuss', label: 'Do dyskusji' },
  ];
  const CLASSES = {
    fix:     { label: 'Fix now',   dec: 'fix',     chip: 'high' },
    verify:  { label: 'Verify',    dec: 'verify',  chip: 'med' },
    quick:   { label: 'Quick win', dec: 'fix',     chip: 'ok' },
    decide:  { label: 'Decide',    dec: 'discuss', chip: 'owner' },
    backlog: { label: 'Backlog',   dec: 'backlog', chip: 'low' },
    reject:  { label: 'Reject',    dec: 'reject',  chip: 'pre' },
    note:    { label: 'Note',      dec: 'note',    chip: 'pre' },
  };
  const CLASS_ORDER = ['fix', 'verify', 'quick', 'decide', 'backlog', 'reject', 'note'];
  const CARDS = [];
  function classify(c) { return 'decide'; } // stub: the page derives it from the rubric fields
  function cls(c) { return c.cls || classify(c); }
  function recDec(c) { return CLASSES[cls(c)].dec; }
  const SUGG = !CLOSED && SUGGESTED ? SUGGESTED : null;
  const state = globalThis.STATE;
  function s(id) { state[id] = state[id] || { opt: '', dec: '', note: '' }; return state[id]; }
  function byClass() { return CARDS.slice().sort(function (a, b) { return CLASS_ORDER.indexOf(cls(a)) - CLASS_ORDER.indexOf(cls(b)); }); }
  const OUT = { value: '' };
  const document = { getElementById: function () { return OUT; } };
  function generate() {
    // A record carries the date of the decision, not the moment somebody opens the page.
    const stamp = CLOSED ? CLOSED.date : new Date().toISOString().slice(0, 16).replace('T', ' ');
    const lines = ['Triage {{TEMAT}} ({{projekt}}) — decyzje ownera (' + stamp + '), rubric v' + RUBRIC_VERSION + ':'];
    if (SUGG) lines.push('Punkt wyjścia: propozycje ' + SUGG.by + (SUGG.date ? ' z ' + SUGG.date : '') + ' — wstępnie wypełnione, to nie są decyzje; poniżej stan formularza.');
    byClass().forEach(function (c) {
      const st = s(c.id);
      const dec = DECISIONS.find(function (d) { return d.v === st.dec; });
      const opt = c.opts.find(function (o) { return o.k === st.opt; });
      let line = c.id + ': ' + (dec ? dec.label : '— (brak decyzji)');
      if (opt) line += ' · wariant (' + opt.k + ')' + (opt.rec ? '' : ' [inny niż rekomendacja]');
      if (st.dec && st.dec !== recDec(c)) line += ' [inna niż klasa ' + CLASSES[cls(c)].label + ']';
      if (st.note && st.note.trim()) line += ' · uwagi: ' + st.note.trim().replace(/\\s+/g, ' ');
      lines.push(line);
    });
    const out = document.getElementById('out'); out.value = lines.join('\\n'); return out.value;
  }
  globalThis.generate = generate;
})();
</script>
"""


def _generate(page: str, state: dict, tmp_path) -> str:
    """Run the built page's generate() in node with the form `state`
    ({"K-n": {dec, opt, note}}) → the decision text it emits."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = tmp_path / "generate.js"
    script.write_text(f"globalThis.STATE = {json.dumps(state)};\n" + _script(page)
                      + "\nprocess.stdout.write(generate());\n", encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    return run.stdout


def _ledger_cycle(engine, topic="review of X"):
    """proj-a / rev-1 with K-1 (options a, b), K-2 (option a) and K-3 (none)."""
    create_project(engine, "proj-a", "A")
    cycle_id = open_triage(engine, "proj-a", "rev-1", topic, "me")["cycle_id"]
    base = {"S": 1, "O": 1, "E": 1, "K": "S", "origin": "diff", "type": "correctness",
            "src": "codex"}
    record_findings(engine, cycle_id, [
        {**base, "title": "one", "fp": "f1",
         "opts": [{"k": "a", "t": "a", "rec": True, "root": True}, {"k": "b", "t": "b"}]},
        {**base, "title": "two", "fp": "f2", "opts": [{"k": "a", "t": "a", "rec": True}]},
        {**base, "title": "three", "fp": "f3"},
    ])
    return cycle_id


def _stored(engine, cycle_id):
    with engine.connect() as conn:
        rows = conn.execute(sa.select(findings).where(findings.c.cycle_id == cycle_id)
                            .order_by(findings.c.seq)).mappings().all()
    return {f"K-{r['seq']}": (r["decision"], r["decision_opt"], r["decision_note"],
                              r["decided_by"], r["decision_text_origin"]) for r in rows}


OWNER = {"decided_by": "owner", "decision_source": "chat 2026-10-14"}
EMPTY = {"dec": "", "opt": "", "note": ""}


@pytest.mark.slow
class TestGeneratedTextIsDecided:
    def test_an_untouched_card_is_skipped_and_keeps_its_earlier_decision(self, engine, tmp_path):
        """K-31: K-2 was decided earlier; the owner leaves it blank in the
        form — its line reads "— (brak decyzji)", it is skipped, its
        stored decision survives; K-1 / K-3 are written."""
        cycle_id = _ledger_cycle(engine)
        decide_findings(engine, cycle_id, {"K-2": {"dec": "backlog", "note": "earlier"}},
                        decided_by="owner", decision_source="chat 2026-10-13")
        page = build_triage_page(export_triage(engine, cycle_id), GENERATOR)
        text = _generate(page, {"K-1": {"dec": "fix", "opt": "b", "note": "  tak,\n (b)  "},
                                "K-2": EMPTY, "K-3": {**EMPTY, "dec": "note"}}, tmp_path)
        assert "K-2: — (brak decyzji)" in text.splitlines()

        out = decide_findings_from_text(engine, cycle_id, text, **OWNER)

        assert (out["decided"], out["undecided"], out["skipped"]) == (2, [], ["K-2"])
        stored = _stored(engine, cycle_id)
        assert stored["K-1"] == ("fix", "b", "tak, (b)", "owner", None)
        assert stored["K-2"] == ("backlog", None, "earlier", "owner", None)
        assert stored["K-3"][:2] == ("note", None)

    def test_a_note_without_a_decision_refuses_the_whole_text(self, engine, tmp_path):
        cycle_id = _ledger_cycle(engine)
        page = build_triage_page(export_triage(engine, cycle_id), GENERATOR)
        text = _generate(page, {"K-1": {"dec": "fix", "opt": "a", "note": ""},
                                "K-2": {**EMPTY, "opt": "a", "note": "jeszcze nie wiem"},
                                "K-3": EMPTY}, tmp_path)
        line = text.splitlines().index("K-2: — (brak decyzji) · wariant (a) · uwagi: "
                                       "jeszcze nie wiem") + 1
        with pytest.raises(ValueError, match=rf"line {line} \(K-2\): K-2 has a note but no"):
            decide_findings_from_text(engine, cycle_id, text, **OWNER)
        assert all(v[0] is None for v in _stored(engine, cycle_id).values())

    def test_an_untouched_form_decides_nothing(self, engine, tmp_path):
        cycle_id = _ledger_cycle(engine)
        page = build_triage_page(export_triage(engine, cycle_id), GENERATOR)
        text = _generate(page, {}, tmp_path)
        out = decide_findings_from_text(engine, cycle_id, text, **OWNER)
        assert out == {"decided": 0, "undecided": [1, 2, 3], "parsed": 0,
                       "skipped": ["K-1", "K-2", "K-3"]}

    def test_a_prefilled_form_stores_its_origin_line_and_the_notes_verbatim(
        self, engine, tmp_path,
    ):
        """K-40: the page was seeded from Codex's proposals; K-1 is accepted
        as proposed, K-2's note edited, K-3 left blank. The generated
        "Punkt wyjścia" line lands verbatim in decision_text_origin of K-1 and
        K-2 only; the notes stay exactly as pasted."""
        cycle_id = _ledger_cycle(engine, topic="review\r\nof X")
        suggest_findings(engine, cycle_id, "Codex", {
            "K-1": {"dec": "fix", "opt": "a", "note": "propozycja"},
            "K-2": {"dec": "backlog", "note": "później"}})
        page = build_triage_page(export_triage(engine, cycle_id), GENERATOR)
        text = _generate(page, {"K-1": {"dec": "fix", "opt": "a", "note": "propozycja"},
                                "K-2": {"dec": "reject", "opt": "",
                                        "note": "Nie — [x] · uwagi: inaczej"},
                                "K-3": EMPTY}, tmp_path)
        lines = text.splitlines()
        assert lines[0].startswith("Triage review of X (proj-a) — decyzje ownera (")  # K-33
        prefill = lines[1]
        assert prefill.startswith("Punkt wyjścia: propozycje Codex z ")

        out = decide_findings_from_text(engine, cycle_id, text, **OWNER)

        assert (out["decided"], out["skipped"]) == (2, ["K-3"])
        stored = _stored(engine, cycle_id)
        assert stored["K-1"] == ("fix", "a", "propozycja", "owner", prefill)
        assert stored["K-2"] == ("reject", None, "Nie — [x] · uwagi: inaczej", "owner", prefill)
        assert stored["K-3"] == (None, None, None, None, None)


# ── the script still parses and reads back (node) ────────────────────────────


@pytest.mark.slow
def test_the_built_script_parses_and_its_values_read_back_in_node(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    export = _export()
    export["cards"][0]["title"] = HOSTILE
    page = build_triage_page(export, SKELETON, topic=TOPIC)
    script = tmp_path / "page.js"
    script.write_text(
        _script(page) + "\nconsole.log(JSON.stringify("
        "{cards: CARDS, closed: CLOSED, key: STORAGE_KEY, header: header('S')}));\n",
        encoding="utf-8")
    check = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                           check=False)
    assert check.returncode == 0, check.stderr
    run = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout)
    assert out["cards"] == export["cards"] and out["closed"] is None
    assert out["key"] == "triage-proj-a-rev-1-c7-v1"
    one_line = " ".join(TOPIC.split())
    assert out["header"] == f"Triage {one_line} (proj-a) — decyzje ownera (S), rubric v2:"


@pytest.mark.slow
def test_placeholders_in_both_string_quotes_keep_the_script_valid_in_node(tmp_path):
    """K-39: a supported position — a '…' or "…" literal — stays valid JS
    for any value and reads back as the value (whitespace collapsed, K-33)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    template = SKELETON.replace(
        "'use strict';", "'use strict'; const D = \"{{TEMAT}}\"; const R = /['\"]/;", 1)
    page = build_triage_page(_export(), template, topic=HOSTILE + " a/b/ `x` " + TOPIC)
    script = tmp_path / "page.js"
    script.write_text(_script(page) + "\nprocess.stdout.write(JSON.stringify(D));\n",
                      encoding="utf-8")
    check = subprocess.run([node, "--check", str(script)], capture_output=True, text=True,
                           check=False)
    assert check.returncode == 0, check.stderr
    run = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == " ".join((HOSTILE + " a/b/ `x` " + TOPIC).split())


def _run_in_node(tmp_path, page: str, tail: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = tmp_path / "page.js"
    script.write_text(_script(page) + "\n" + tail + "\n", encoding="utf-8")
    run = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# A topic that would end or reshape a regex if it ever landed in one.
REGEX_BREAKER = "a/[b]/ + alert(1) + /c] \\/ [x"


@pytest.mark.slow
@pytest.mark.parametrize("code", POSTFIX)
def test_division_after_a_postfix_increment_runs_in_node(tmp_path, code):
    """K-43: the accepted scripts are what node runs — 6 / 2 — and the
    string next to them reads back exactly (`/`, `[`, `]` escaped, K-42)."""
    page = build_triage_page(_export(), _with(code + " const D = '{{TEMAT}}';"),
                             topic=REGEX_BREAKER)
    assert _run_in_node(tmp_path, page, "process.stdout.write(JSON.stringify([q1, D]));") == [
        3, REGEX_BREAKER]


@pytest.mark.slow
def test_division_after_a_call_and_regexes_after_control_heads_run_in_node(tmp_path):
    """K-42: the scan's classification is node's — a `)` of a call or a
    grouping divides, a `)` of an if head is followed by a regex — and a
    string holding `/`, `[`, `]` reads back exactly."""
    code = ("function f(n) { return n; } let hits = 0;"
            " const q1 = f(12) / (2) / 2;"
            " if (true) /x/.test('x') && hits++;"
            " while (hits < 2) /y/.test('y') && hits++;"
            " const D = \"{{TEMAT}}\";")
    page = build_triage_page(_export(), _with(code), topic=REGEX_BREAKER)
    assert _run_in_node(tmp_path, page, "process.stdout.write(JSON.stringify([q1, hits, D]));") \
        == [3, 2, REGEX_BREAKER]
