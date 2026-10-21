"""The triage page's string table (#991) — one source for the page and the parser.

`dct/assets/triage-strings.json` holds every user-visible text of the shipped
triage page and every word of the decision text the owner pastes back, per
language: {"en": {key: text}, "pl": {key: text}}. The page receives the table
of its cycle's language as `const STRINGS` (triage_page.build_triage_page);
the decision-text parser (triage.parse_decision_batch) reads the words of
EVERY language from the same table, so a text in any shipped language — and
every Polish text written before the table existed — parses.

Texts are plain text. `{name}` marks a parameter (fmt); `**…**` marks bold in
the legend texts the page renders with its own escaping. A table is validated
whole on load (load_table): the same keys in every language, no empty text,
the required keys present, the decision-text words consistent, and no
spelling that means two different things to the parser once normalized
(label_key) — across languages, the canonical decision tokens and the
no-decision label (spec #991 R-3).
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from dct.workflows import rubric

TABLE_PATH = Path(__file__).resolve().parent.parent / "assets" / "triage-strings.json"
DEFAULT_LANGUAGE = "en"
_LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{1,4})?")

# Keys the parser, the acceptance and the lede read — a table without one of
# them is refused (the page's own keys are covered by the same-keys rule).
REQUIRED_KEYS: tuple[str, ...] = (
    *(f"decision.{d}" for d in rubric.DECISIONS),
    "decision.none", "text.header", "text.prefill_prefix", "text.prefill", "text.variant",
    "text.notes", "response.accept_tag", "lede.text", "lede.card_one", "lede.card_few",
    "lede.card_many",
)

# The meaning the parser gives the no-decision label (never a decision token).
NO_DECISION = "\x00none"

# Separators a label may be written with: → / -> / — / – / "/" are one.
_LABEL_SEPARATOR = re.compile(r"\s*(?:→|->|—|–|/)\s*")
_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class StringTableError(ValueError):
    """The string table is malformed — named key, language and reason."""


def label_key(label: str) -> str:
    """A decision label as the parser compares it: case-folded, whitespace
    collapsed, every separator (→ -> — – /) one."""
    return " | ".join(
        " ".join(part.split()).casefold() for part in _LABEL_SEPARATOR.split(label.strip()))


def fmt(text: str, **values: object) -> str:
    """`text` with each `{name}` replaced by str(values[name]); a parameter
    without a value stays as written (the table is data, never code)."""
    return _PARAMETER.sub(
        lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def _vocabulary(table: dict[str, dict[str, str]]) -> dict[str, str]:
    """{label_key(spelling): meaning} over every language's decision labels,
    the canonical tokens and the no-decision labels; StringTableError when one
    spelling means two things."""
    meanings: dict[str, str] = {}
    sources: dict[str, str] = {}

    def add(spelling: str, meaning: str, source: str) -> None:
        key = label_key(spelling)
        if not key.replace("|", "").strip():
            raise StringTableError(f"triage strings: {source} is empty once normalized")
        if key in meanings and meanings[key] != meaning:
            raise StringTableError(
                f"triage strings: {source} ({spelling!r}) reads the same as {sources[key]} "
                "but means something else to the decision-text parser")
        meanings.setdefault(key, meaning)
        sources.setdefault(key, source)

    for token in rubric.DECISIONS:
        add(token, token, f"decision token {token!r}")
    for lang, texts in table.items():
        for token in rubric.DECISIONS:
            add(texts[f"decision.{token}"], token, f"{lang}: decision.{token}")
        add(texts["decision.none"], NO_DECISION, f"{lang}: decision.none")
    return meanings


def load_table(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Read and validate a string table (default: the shipped one) →
    {language: {key: text}}. StringTableError names what is wrong."""
    source = path or TABLE_PATH
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StringTableError(f"triage strings: cannot read {source}: {exc}") from None
    if not isinstance(data, dict) or not data:
        raise StringTableError("triage strings: the table must map languages to texts")
    if DEFAULT_LANGUAGE not in data:
        raise StringTableError(f"triage strings: the default language {DEFAULT_LANGUAGE!r} "
                               "is missing")
    keys: set[str] | None = None
    for lang, texts in data.items():
        if not isinstance(lang, str) or not _LANGUAGE_CODE.fullmatch(lang):
            raise StringTableError(f"triage strings: {lang!r} is not a language code")
        if not isinstance(texts, dict):
            raise StringTableError(f"triage strings: {lang} must map keys to texts")
        for key, text in texts.items():
            if not isinstance(text, str) or not text.strip():
                raise StringTableError(f"triage strings: {lang}: {key} is empty or not text")
        if keys is None:
            keys = set(texts)
        elif set(texts) != keys:
            missing = sorted(keys - set(texts))
            extra = sorted(set(texts) - keys)
            raise StringTableError(
                f"triage strings: {lang} differs from the other languages — "
                f"missing {missing or 'none'}, extra {extra or 'none'}")
    absent = [k for k in REQUIRED_KEYS if k not in (keys or set())]
    if absent:
        raise StringTableError(f"triage strings: required keys missing: {', '.join(absent)}")
    for lang, texts in data.items():
        if not texts["text.prefill"].startswith(texts["text.prefill_prefix"]):
            raise StringTableError(
                f"triage strings: {lang}: text.prefill must start with text.prefill_prefix")
        if "{by}" not in texts["response.accept_tag"]:
            raise StringTableError(f"triage strings: {lang}: response.accept_tag needs {{by}}")
        for word in ("text.notes", "text.variant"):
            if any(ch in texts[word] for ch in "·:()[]"):
                raise StringTableError(
                    f"triage strings: {lang}: {word} may not contain · : ( ) [ ]")
    _vocabulary(data)
    return data


@cache
def table() -> dict[str, dict[str, str]]:
    """The shipped table, validated once per process."""
    return load_table()


def languages() -> tuple[str, ...]:
    """The shipped language codes, the default first."""
    codes = sorted(table())
    codes.remove(DEFAULT_LANGUAGE)
    return (DEFAULT_LANGUAGE, *codes)


def check_language(value: object, where: str = "language") -> str:
    """A shipped language code (stripped, lower-cased) → itself; ValueError
    naming the shipped ones otherwise."""
    if isinstance(value, str) and value.strip().lower() in table():
        return value.strip().lower()
    raise ValueError(f"{where}: unknown language {value!r} — "
                     f"shipped: {', '.join(languages())}")


def strings(language: str) -> dict[str, str]:
    """The texts of one shipped language (a copy)."""
    return dict(table()[check_language(language)])


@cache
def vocabulary() -> dict[str, str]:
    """The parser's label map over every shipped language:
    {label_key(spelling): decision token or NO_DECISION}."""
    return _vocabulary(table())


def text_words(key: str) -> tuple[str, ...]:
    """The distinct texts of `key` across the shipped languages, longest
    first (so a longer spelling is tried before a prefix of it)."""
    return tuple(sorted({texts[key] for texts in table().values()}, key=len, reverse=True))


def decision_labels() -> tuple[str, ...]:
    """Every decision label of every language plus the canonical tokens —
    for error messages."""
    seen: list[str] = []
    for texts in table().values():
        for token in rubric.DECISIONS:
            if texts[f"decision.{token}"] not in seen:
                seen.append(texts[f"decision.{token}"])
    return (*seen, *rubric.DECISIONS)


def accepted_prefix(language: str, by: str) -> str:
    """The note prefix accept_suggestions writes, in the cycle's language."""
    return fmt(strings(language)["response.accept_tag"], by=by)


def _plural_key(language: str, n: int) -> str:
    if language == "pl":
        if n == 1:
            return "lede.card_one"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "lede.card_few"
        return "lede.card_many"
    return "lede.card_one" if n == 1 else "lede.card_many"


def lede(language: str, n: int, rubric_version: object, code: str) -> str:
    """The page's default lede: card count, rubric version, cycle code and one
    sentence of legend, in the cycle's language."""
    texts = strings(language)
    return fmt(texts["lede.text"], n=n, cards=texts[_plural_key(language, n)],
               rubric=rubric_version, code=code)
