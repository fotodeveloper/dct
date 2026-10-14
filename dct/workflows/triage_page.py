"""Triage page builder (#930) — the page is derived from export_triage only.

build_triage_page fills a COPY of the owner's triage template (dct ships no
template; `dct triage page` resolves it from --template or the config's
[triage] template) with a cycle's export: no database, no I/O, a pure
function of (export, template, metadata).

Contracts (spec #930 §2, §8 K-3 / K-4 / K-5, rev 4 K-33 / K-34 / K-39):
- The three page constants — `const CARDS = …;`, `const CLOSED = …;`,
  `const SUGGESTED = …;` — are located in the ORIGINAL template, at code
  level of its inline <script> elements (text inside a JS string, comment,
  regex or template literal, or inside an HTML comment, never counts).
  Exactly one declaration each; its initializer must be `null` or one
  balanced `[…]` / `{…}` literal followed by `;` — anything else (a missing
  or duplicate declaration, an unterminated string, another initializer) is
  refused with ValueError naming it. Each declaration is replaced by the
  export's value through page_constants_json (CLOSED / SUGGESTED `null`
  when absent).
- Placeholders are validated in the ORIGINAL template too, outside the
  three declarations: {{TEMAT}}, {{PROJEKT}}, {{KONTEKST}}, {{DATA}}, the
  whole {{LEDE …}} token, {{projekt}} and {{temat}}; any other {{…}} there is
  refused naming it. Every span of the template is substituted ONCE and the
  inserted data is never re-scanned: a finding quoting `{{TEMAT}}` or
  `const CLOSED` stays literal.
- Metadata placeholders are plain text, in one of two contexts (K-39):
  in HTML (outside <script>) they are HTML-escaped (& < > " ') and kept as
  given, line breaks included; inside a <script> they must sit in a
  single- or double-quoted JS string literal — a placeholder in script
  code, a comment, a regex literal or a template literal is refused (a
  value there would change the code, e.g. `/{{TEMAT}}/` with "a/b" gives
  `/a/b/`) — and are written as JS string escapes (\\uXXXX for quotes,
  `<`, `>`, `&`, `$`, backtick and control characters; `\\` doubled), so
  the string reads back exactly and cannot close the script.
- K-33: in that JS string context — the template's generate() header line
  "Triage {{TEMAT}} ({{projekt}}) — decyzje ownera (…)" — every run of
  whitespace, line separators included (\\n, \\r, U+2028, U+2029,
  U+0085, …: everything str.split() and str.splitlines() break on), is
  collapsed to one space and the ends are trimmed, AT BUILD TIME: the
  generated decision text keeps its one-line header, so the parser
  (parse_decision_batch) never meets a topic split over lines. The cycle's
  topic and a --topic override alike; the stored topic and the HTML
  context are untouched.
- {{projekt}} / {{temat}} are the STORAGE_KEY slugs (K-34):
  `triage-{{projekt}}-{{temat}}-v1` becomes
  `triage-<project slug>-<code slug>-c<cycle id>-v1` (`…-c<cycle id>-v1`
  when the code folds to nothing) — the immutable cycle id at the
  unambiguous end makes the key injective (slugs may contain '-' and
  'c<digits>', so with the id in the middle `p`/1/"c2-x" and `p-c1`/2/"x"
  collided), the slugs only make it readable. Changing the order changed
  the key of every existing page: a draft kept in the browser under an
  older key is not migrated and may be lost — the ledger's decisions and
  proposals are not affected.
- Defaults (deterministic — no clock): topic = the cycle's topic, project =
  the cycle's project, context = the cycle's scope, else its code, date =
  CLOSED.date of a closed cycle, else the cycle's opening date, lede =
  default_lede(export).
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping

from dct.workflows.triage import page_constants_json

CONSTANTS = ("CARDS", "CLOSED", "SUGGESTED")
# The export key each constant is filled from.
_CONSTANT_SOURCE = {"CARDS": "cards", "CLOSED": "closed", "SUGGESTED": "suggested"}
# Metadata placeholders (plain text) and the STORAGE_KEY slug placeholders;
# {{LEDE …}} is matched as a whole token (_LEDE).
TEXT_PLACEHOLDERS = ("TEMAT", "PROJEKT", "KONTEKST", "DATA")
SLUG_PLACEHOLDERS = ("projekt", "temat")
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_LEDE = re.compile(r"LEDE(?:\s.*)?", re.DOTALL)

# The default lede (the page speaks the template's language, Polish): the
# card count, rubric version and cycle code, then one sentence of legend.
LEDE_LEGEND = (
    "Klasa karty jest rekomendacją wyliczoną z pól rubryki, nie decyzją; "
    "decyzja (Fix teraz / Zweryfikuj / Backlog → dct / Odrzuć / Odnotuj / "
    "Do dyskusji) należy do ownera."
)

_SCRIPT_OPEN = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script", re.IGNORECASE)
_DECLARATION = re.compile(r"(?<![A-Za-z0-9_$])const\s+(CARDS|CLOSED|SUGGESTED)\s*=")
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")
# A `/` after one of these code characters / keywords starts a regex literal.
_REGEX_AFTER_CHARS = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = {
    "return", "typeof", "case", "do", "else", "in", "of", "new", "delete",
    "void", "throw", "instanceof", "yield", "await",
}
# _mask_js stands these in for every character of a literal (K-39): a
# single- / double-quoted string — the one place a placeholder may sit in a
# script — versus a template or regex literal, where one is refused.
_MASK = "\x00"
_MASK_OTHER = "\x01"
_JS_ESCAPED = set("'\"`$<>&  ")


def _plural_cards(n: int) -> str:
    if n == 1:
        return "karta"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "karty"
    return "kart"


def default_lede(export: Mapping) -> str:
    """The lede when none is given: '<n> kart · rubric v<v> · cykl <code>. '
    + LEDE_LEGEND (the noun takes its Polish plural form)."""
    cycle = export["cycle"]
    n = len(export["cards"])
    return (f"{n} {_plural_cards(n)} · rubric v{cycle['rubric_version']} · "
            f"cykl {cycle['code']}. {LEDE_LEGEND}")


def storage_slug(text: str) -> str:
    """`text` as a STORAGE_KEY slug: accents folded (NFKD → ASCII), lowercase,
    every run of other characters one '-', no leading / trailing '-'; may be
    empty."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def storage_key_parts(export: Mapping) -> dict[str, str]:
    """The STORAGE_KEY placeholders of a cycle (K-34): projekt = the project
    slug ('project' when it folds to nothing), temat = '<code slug>-' (when
    the code folds to something) + 'c<cycle id>' — the id last."""
    cycle = export["cycle"]
    code = storage_slug(str(cycle["code"]))
    return {
        "projekt": storage_slug(str(cycle["project"])) or "project",
        "temat": (f"{code}-" if code else "") + f"c{cycle['id']}",
    }


def _js_string_text(value: str) -> str:
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch in _JS_ESCAPED or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


# ── template scan ────────────────────────────────────────────────────────────


def _script_bodies(template: str) -> list[tuple[int, int]]:
    """(start, end) of every inline <script> body, HTML comments skipped."""
    bodies = []
    i = 0
    while True:
        comment = template.find("<!--", i)
        opening = _SCRIPT_OPEN.search(template, i)
        if opening is None:
            return bodies
        if 0 <= comment < opening.start():
            end = template.find("-->", comment + 4)
            if end < 0:
                raise ValueError("template: an HTML comment is never closed (<!-- without -->)")
            i = end + 3
            continue
        closing = _SCRIPT_CLOSE.search(template, opening.end())
        if closing is None:
            raise ValueError("template: a <script> element is never closed")
        bodies.append((opening.end(), closing.start()))
        i = closing.end()


def _mask_js(src: str, where: str) -> str:
    """`src` with every character of a quoted string literal replaced by
    _MASK, of a template or regex literal by _MASK_OTHER, and every comment
    character by a space (newlines kept) — same length, so positions carry
    over; only code stays readable."""
    out = list(src)
    n = len(src)
    i = 0
    last = ""  # last significant code character
    word = ""  # last identifier / keyword before it

    def mask(a: int, b: int, char: str) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = char

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = src.find("\n", i)
            end = n if end < 0 else end
            mask(i, end, " ")
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"{where}: a /* comment is never closed")
            mask(i, end + 2, " ")
            i = end + 2
            continue
        if ch in "'\"`" or (ch == "/" and (last == "" or last in _REGEX_AFTER_CHARS
                                          or (last.isalnum() and word in _REGEX_AFTER_WORDS))):
            j = i + 1
            in_class = False
            while True:
                if j >= n:
                    raise ValueError(f"{where}: a {ch} literal is never closed")
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n" and ch != "`":
                    raise ValueError(f"{where}: a {ch} literal is never closed on its line")
                if ch == "/" and c == "[":
                    in_class = True
                elif ch == "/" and c == "]":
                    in_class = False
                elif c == ch and not in_class:
                    break
                j += 1
            if ch == "/":  # regex flags belong to the literal
                j += 1
                while j < n and _IDENT_CHAR.match(src[j]):
                    j += 1
                mask(i, j, _MASK_OTHER)
                i = j
            else:
                mask(i, j + 1, _MASK if ch in "'\"" else _MASK_OTHER)
                i = j + 1
            last, word = _MASK, ""
            continue
        if not ch.isspace():
            if _IDENT_CHAR.match(ch):
                word = word + ch if last and _IDENT_CHAR.match(last) else ch
            else:
                word = ""
            last = ch
        i += 1
    return "".join(out)


def _initializer_end(masked: str, start: int, name: str) -> int:
    """End (after `;`) of the initializer of `const NAME =` starting at
    `start` in the masked script: `null` or one balanced [...] / {...}."""
    where = f"template: const {name}"
    i = start
    while i < len(masked) and masked[i].isspace():
        i += 1
    if masked.startswith("null", i) and not _IDENT_CHAR.match(masked[i + 4:i + 5] or " "):
        i += 4
    elif i < len(masked) and masked[i] in "[{":
        pairs = {"[": "]", "{": "}", "(": ")"}
        stack = []
        while i < len(masked):
            c = masked[i]
            if c in pairs:
                stack.append(pairs[c])
            elif c in ")]}":
                if not stack or stack.pop() != c:
                    raise ValueError(f"{where}: unbalanced {c!r} in its initializer")
                if not stack:
                    i += 1
                    break
            i += 1
        else:
            raise ValueError(f"{where}: its initializer is never closed")
    else:
        raise ValueError(
            f"{where}: unsupported initializer — expected null, [...] or {{...}}")
    while i < len(masked) and masked[i].isspace():
        i += 1
    if i >= len(masked) or masked[i] != ";":
        raise ValueError(f"{where}: its initializer must end with ';'")
    return i + 1


def _constant_spans(template: str) -> tuple[dict[str, tuple[int, int]], list[tuple[int, int, str]]]:
    """→ ({NAME: (start, end)} of the three declarations, [(start, end,
    masked body)] of the script bodies)."""
    scripts = []
    found: dict[str, list[tuple[int, int]]] = {name: [] for name in CONSTANTS}
    for start, end in _script_bodies(template):
        masked = _mask_js(template[start:end], "template <script>")
        scripts.append((start, end, masked))
        for m in _DECLARATION.finditer(masked):
            name = m.group(1)
            found[name].append((start + m.start(),
                                start + _initializer_end(masked, m.end(), name)))
    spans = {}
    for name, hits in found.items():
        if not hits:
            raise ValueError(f"template: no 'const {name} = …;' declaration in a <script>")
        if len(hits) > 1:
            raise ValueError(
                f"template: 'const {name}' is declared {len(hits)} times — exactly one expected")
        spans[name] = hits[0]
    return spans, scripts


# ── build ────────────────────────────────────────────────────────────────────


def _text(value, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"build_triage_page: {label} must be a string, got {value!r}")  # noqa: TRY004 — every refusal is a ValueError
    return value


def build_triage_page(
    export: Mapping,
    template_html: str,
    *,
    topic: str | None = None,
    project_slug: str | None = None,
    context: str | None = None,
    lede: str | None = None,
    date: str | None = None,
) -> str:
    """The triage page of one cycle: `template_html` with its three
    constants and its placeholders filled from `export` (export_triage's
    dict) → the page HTML. None metadata takes the defaults of the module
    docstring; every given value must be a string (shown as plain text).
    ValueError on a template the module docstring's contract refuses or an
    export without its cycle / cards."""
    if not isinstance(template_html, str):
        raise ValueError("build_triage_page: the template must be a string")  # noqa: TRY004 — every refusal is a ValueError
    if not isinstance(export, Mapping) or not isinstance(export.get("cycle"), Mapping) \
            or not isinstance(export.get("cards"), list):
        raise ValueError("build_triage_page: export must be export_triage's dict "  # noqa: TRY004 — every refusal is a ValueError
                         "(cycle, cards, closed, suggested)")
    cycle = export["cycle"]
    closed = export.get("closed")
    values = {
        "TEMAT": _text(cycle["topic"] if topic is None else topic, "topic"),
        "PROJEKT": _text(cycle["project"] if project_slug is None else project_slug,
                         "project_slug"),
        "KONTEKST": _text((cycle.get("scope") or cycle["code"]) if context is None else context,
                          "context"),
        "DATA": _text((closed["date"] if closed else str(cycle["opened_at"])[:10])
                      if date is None else date, "date"),
        "LEDE": _text(default_lede(export) if lede is None else lede, "lede"),
    }
    slugs = storage_key_parts(export)

    spans, scripts = _constant_spans(template_html)
    constant_at = sorted((start, end, name) for name, (start, end) in spans.items())

    def placeholder(m: re.Match, offset: int) -> str:
        """The replacement of one placeholder of the ORIGINAL template."""
        inner = m.group(1)
        pos = offset + m.start()
        if inner in SLUG_PLACEHOLDERS:
            return slugs[inner]
        key = "LEDE" if _LEDE.fullmatch(inner) else inner
        if key not in values:
            raise ValueError(f"template: unknown placeholder {m.group(0)!r}")
        for start, end, masked in scripts:
            if start <= pos < end:
                at = masked[pos - start]
                if at == _MASK_OTHER:
                    raise ValueError(
                        f"template: placeholder {m.group(0)!r} sits in a regex or template "
                        "literal — inside a <script> it must be within a '…' or \"…\" "
                        "string literal")
                if at != _MASK:
                    raise ValueError(
                        f"template: placeholder {m.group(0)!r} sits in script code or a comment — "
                        "inside a <script> it must be within a '…' or \"…\" string literal")
                return _js_string_text(" ".join(values[key].split()))
        return html.escape(values[key], quote=True)

    pieces = []
    cursor = 0
    for start, end, name in constant_at:
        segment = template_html[cursor:start]
        pieces.append(_PLACEHOLDER.sub(lambda m, o=cursor: placeholder(m, o), segment))
        value = export.get(_CONSTANT_SOURCE[name])
        pieces.append(f"const {name} = {page_constants_json(value)};")
        cursor = end
    pieces.append(_PLACEHOLDER.sub(lambda m, o=cursor: placeholder(m, o),
                                   template_html[cursor:]))
    return "".join(pieces)
