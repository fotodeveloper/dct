"""Fewer questions in the everyday skills (#877) — the owner's decisions,
pinned in the prose of the skills and of the authoritative fallback doc
BEFORE the rewrite.

- K-7: /track, /clog, /track-update and /track-resolve write on an
  UNAMBIGUOUS instruction without a confirmation question and acknowledge
  in ONE line — what was created / changed and how to correct it. A
  question stays only where the skill genuinely cannot tell: the type
  (item type / changelog category), the project, which item.
- H-6 (b): /track does not force checkpoints at creation. When work on an
  item STARTS (/track-update "start working on #N", status → in_progress)
  the skill proposes a plan of AT LEAST 3 checkpoints — 3-7 is a minimum,
  never a cap.
- H-7: /pickup on an unambiguous "continue" / "pickup" with exactly ONE
  matching handoff consumes it without asking and names its id in the
  report; several matching handoffs still ask which.
- /commit: ONE screen (files, message, entries it carries) with
  Commit / Change / Stop; a staging question only when there is a real
  choice (changed files that do not all belong to the entries). Every
  safety rule stays (blacklist, never `git add -A`, trailer selected after
  staging, never `--amend` / `--no-verify`).
- /handoff: one question — Save / Change / Cancel — with "sprint
  unchanged" and "no scope" shown as lines of the draft, not asked as
  separate questions.

Round 2 of the triage (the K-52 … K-67 section at the end): closing goes
through the gate check and no gate is auto-rejected; the gate stop speaks
plainly; no type cue → todo; the plan of at least 3 checkpoints only for
non-trivial feature/improvement work or a breakdown request; /commit reads
the shared secret-path predicate, treats new files like any other and
applies the project's stored trailer preferences; /handoff keeps add / cut
additive and saves every field of the approved draft; /pickup consumes only
on an instruction to continue, counts a scope's own handoffs, reads a chosen
handoff in full before consuming it and starts a still-current next step
without a question. The text helpers respect word boundaries and negation.

The fallback doc (docs/assistant-fallbacks.md) is the authoritative prose:
every rule pinned for a skill is pinned for its fallback section too.
"""

from __future__ import annotations

import re

import pytest
from test_skill_prose_changelog import _fallbacks, _skill, _snippet

# ── text helpers ─────────────────────────────────────────────────────────────

NEGATION = re.compile(r"\b(never|not|no|without|nothing)\b|n't\b", re.IGNORECASE)
# Where a clause ends: a negation before one of these does not govern what
# follows it ("Ask once for the batch — do NOT ask per item" is no negation
# of the first ask).
CLAUSE_BREAK = re.compile(r"[;:—(]|\s-\s|,\s*(?:then|and|but)\b", re.IGNORECASE)


def _negated_at(sentence: str, start: int) -> bool:
    """A negation word governs the phrase that starts at `start`: it stands
    before it in the SAME clause. A "not" elsewhere in the sentence does not
    turn a request to confirm into its opposite."""
    prefix = sentence[:start]
    cut = max((m.end() for m in CLAUSE_BREAK.finditer(prefix)), default=0)
    return bool(NEGATION.search(prefix[cut:]))


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    """Sentences, list items and paragraphs of `text`, each flattened."""
    parts = re.split(r"(?<=[.!?])\s+|\n\s*\n|\n\s*(?:[-*]|\d+\.)\s+", text)
    return [_flat(p) for p in parts if p.strip()]


def _paragraphs(text: str) -> list[str]:
    return [_flat(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


def _fallback(command: str) -> str:
    """The `## `/<command>` … fallback` section of the authoritative doc."""
    text = _fallbacks()
    start = text.index(f"## `/{command}`")
    ends = [i for i in (text.find("\n## ", start + 1),
                        text.find("These prose descriptions", start)) if i != -1]
    return text[start:min(ends)] if ends else text[start:]


def _heading_section(text: str, title_pattern: str) -> str:
    """The section under the first heading whose title matches
    `title_pattern`, up to the next heading of the same or a higher level."""
    m = re.search(rf"^(#{{2,4}}) [^\n]*(?:{title_pattern})[^\n]*$", text,
                  re.MULTILINE | re.IGNORECASE)
    assert m, f"no heading matching {title_pattern!r}"
    level = len(m.group(1))
    nxt = re.search(rf"^#{{1,{level}}} ", text[m.end():], re.MULTILINE)
    return text[m.start(): m.end() + nxt.start()] if nxt else text[m.start():]


def _ack_part(text: str) -> str:
    """Where the dialog acknowledges the write: the Report / Acknowledge
    heading of a skill, or the numbered Report / Acknowledge step of a
    fallback section."""
    if re.search(r"^#{2,4} [^\n]*(Report|Acknowledge)", text, re.MULTILINE | re.IGNORECASE):
        return _heading_section(text, "Report|Acknowledge")
    steps = re.findall(r"^\d+\. (?:Report|Acknowledge)[^\n]*(?:\n(?!\d+\. |\n)[^\n]*)*",
                       text, re.MULTILINE | re.IGNORECASE)
    assert steps, "no Report / Acknowledge step"
    return "\n".join(steps)


def _option_offered(flat: str, label: str) -> bool:
    """`label` is offered as a dialog option: a quoted / bold label, or one
    member of an `A / B / C` option row."""
    return bool(re.search(rf"[\"“`*]{label}\b|\b{label}\s*/\s*[\"“`*]?[A-Z]|"
                          rf"[a-zA-Z\"”`*]\s*/\s*[\"“`*]?{label}\b", flat))


# ═════════════════════════════════════════════════════════════════════════════
# K-7 — cheap reversible writes: no confirmation question, one-line ack
# ═════════════════════════════════════════════════════════════════════════════

CHEAP_WRITES = [
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(lambda: _skill("clog"), id="clog-skill"),
    pytest.param(lambda: _fallback("clog"), id="clog-fallback"),
    pytest.param(lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param(lambda: _skill("track-resolve"), id="track-resolve-skill"),
    pytest.param(lambda: _fallback("track-resolve"), id="track-resolve-fallback"),
]

# The write each cheap dialog performs on an unambiguous instruction.
WRITE_TOOL = {"track": "create_item", "clog": "add_changelog",
              "track-update": "update_item", "track-resolve": "resolve_item"}

CHEAP_WRITES_BY_NAME = [
    pytest.param("track", lambda: _skill("track"), id="track-skill"),
    pytest.param("track", lambda: _fallback("track"), id="track-fallback"),
    pytest.param("clog", lambda: _skill("clog"), id="clog-skill"),
    pytest.param("clog", lambda: _fallback("clog"), id="clog-fallback"),
    pytest.param("track-update", lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param("track-resolve", lambda: _skill("track-resolve"), id="track-resolve-skill"),
    pytest.param("track-resolve", lambda: _fallback("track-resolve"),
                 id="track-resolve-fallback"),
]

# A request to confirm, phrased any of the ways the old prose did (or a
# rewrite plausibly could). A sentence carrying one is allowed only when a
# negation governs the phrase in its own clause ("never wait for
# confirmation") or when the question is tied to an ambiguity.
CONFIRMATION_ASKS = [
    r"\bwait(s|ing)? for\b[^.;]{0,40}\b(confirm\w*|approval|go-ahead|the user'?s? (ok|answer))",
    r"\b(ask|asks|asking)\b[^.;]{0,40}\bconfirm\w*",
    r"\bconfirm (with|by) (the )?(user|owner)\b",
    r"\b(user|owner) (confirms|approves)\b",
    r"\b(present|show)\b[^.;]{0,80}\bconfirm\b",
    r"\bget (the )?(user'?s? |owner'?s? )?(approval|confirmation)\b",
    r"\bconfirmations? (are|is) one-shot\b",
    r"\bnever silently create\b",
]


def _confirmation_requests(body: str) -> list[str]:
    hits = []
    for s in _sentences(body):
        if _ambiguity_condition(s):
            continue  # a question tied to an ambiguity is allowed (K-7)
        for p in CONFIRMATION_ASKS:
            m = re.search(p, s, re.IGNORECASE)
            if m and not _negated_at(s, m.start()):
                hits.append(s)
                break
    return hits


@pytest.mark.parametrize("text", CHEAP_WRITES)
def test_unambiguous_write_has_no_confirmation_question(text):
    body = text()
    offending = _confirmation_requests(body)
    assert not offending, f"the dialog still asks for confirmation: {offending}"
    assert not re.search(r"^#{2,4} (Step \d+: )?Confirm\b", body, re.MULTILINE), \
        "an unconditional Confirm step is still there"
    assert "Track this?" not in body, "the 'Track this?' confirmation draft is still there"


WITHOUT_CONFIRMATION = re.compile(r"\b(without|no) (a |any )?confirmation\b", re.IGNORECASE)


@pytest.mark.parametrize(("name", "text"), CHEAP_WRITES_BY_NAME)
def test_unambiguous_write_states_it_writes_without_a_confirmation(name, text):
    """The positive side of the pin above — deleting the old question is not
    enough: the dialog SAYS that an unambiguous instruction performs the
    write (its tool named) without a confirmation question."""
    tool = WRITE_TOOL[name]
    hits = []
    for p in _paragraphs(text()):
        m = WITHOUT_CONFIRMATION.search(p)
        if (m and re.search(r"\bunambiguous\b", p, re.IGNORECASE) and tool in p
                and not re.search(r"\b(never|not|n't)\s+(\w+\s+){0,3}" + re.escape(m.group(0)), p,
                                  re.IGNORECASE)):
            hits.append(p)
    assert hits, (f"no rule says an unambiguous instruction calls {tool} "
                  "without a confirmation question")


# The tool (or skill) that corrects what was just written — the hint the
# one-line acknowledgement carries.
CORRECTION_TOOLS = {
    "track": r"/track-update|update_item|wont_fix",
    "clog": r"update_changelog|delete_changelog",
    "track-update": r"/track-update|update_item",
    "track-resolve": r"/track-update|update_item",
}
CORRECTION_VERB = r"\b(correct|undo|revert|reopen|change it|to fix it)"

ACKS = [
    pytest.param("track", lambda: _skill("track"), id="track-skill"),
    pytest.param("track", lambda: _fallback("track"), id="track-fallback"),
    pytest.param("clog", lambda: _skill("clog"), id="clog-skill"),
    pytest.param("clog", lambda: _fallback("clog"), id="clog-fallback"),
    pytest.param("track-update", lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param("track-resolve", lambda: _skill("track-resolve"), id="track-resolve-skill"),
    pytest.param("track-resolve", lambda: _fallback("track-resolve"),
                 id="track-resolve-fallback"),
]


@pytest.mark.parametrize(("name", "text"), ACKS)
def test_acknowledgement_is_one_line_with_a_correction_hint(name, text):
    ack = _ack_part(text())
    flat = _flat(ack)
    assert re.search(r"\bone[- ]line\b|\bin one line\b", flat, re.IGNORECASE), \
        f"the acknowledgement is not one line: {flat[:300]}"
    tool = CORRECTION_TOOLS[name]
    hint = re.search(rf"{CORRECTION_VERB}[^\n]{{0,200}}({tool})|({tool})[^\n]{{0,200}}"
                     rf"{CORRECTION_VERB}", flat, re.IGNORECASE)
    assert hint, f"the acknowledgement does not say how to correct the write ({tool}): {flat}"
    # The example of a single write IS one line (a "one line" claim over a
    # two-line example would be a contradiction, not an acknowledgement).
    examples = re.findall(r"```[a-z]*\n(.*?)```", ack, re.DOTALL)
    if examples:
        lines = [ln for ln in examples[0].splitlines() if ln.strip()]
        assert len(lines) == 1, f"the single-write acknowledgement is {len(lines)} lines: {lines}"


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track-resolve"), id="skill"),
    pytest.param(lambda: _fallback("track-resolve"), id="fallback"),
])
def test_track_resolve_acknowledgement_says_how_to_reopen(text):
    """A resolve is corrected by reopening (status back to open), which
    /track-update / update_item does — not by another resolve. ("left open"
    about task checkpoints is not that hint.)"""
    flat = _flat(_ack_part(text()))
    assert re.search(r"\breopen|status=[\"']?open\b", flat, re.IGNORECASE), flat


def test_track_update_acknowledgement_shows_before_and_after():
    """Without a confirmation screen the before → after values move into the
    acknowledgement, so a wrong change is visible and reversible at once."""
    ack = _ack_part(_skill("track-update"))
    examples = re.findall(r"```\n?(.*?)```", ack, re.DOTALL)
    assert examples, "no acknowledgement example"
    assert any(re.search(r"\S+ → \S+", e) for e in examples), \
        f"the acknowledgement does not show the previous value: {examples}"


# ── the questions that stay: only where the skill cannot tell ───────────────

# Word-bounded: "unambiguous" is the OPPOSITE of an ambiguity, so it must not
# match (`\bambiguous` has no word boundary inside "unambiguous").
AMBIGUITY = (r"\bambiguous\b|\bunclear\b|\bcannot tell\b|\bcan't tell\b|\bnot registered\b|"
             r"\bunregistered\b|\bdoes not resolve\b|\bdoesn't resolve\b|\bno id\b|"
             r"\bnot given\b|\bseveral\b|\bmore than one\b|\bgenuinely\b|"
             r"\btwo (different )?types\b")


def _ambiguity_condition(sentence: str) -> bool:
    """The sentence ties what it says to an ambiguity: an ambiguity word
    stands in it and no negation governs that word ("not ambiguous" and
    "unambiguous" say the opposite)."""
    return any(not _negated_at(sentence, m.start())
               for m in re.finditer(AMBIGUITY, sentence, re.IGNORECASE))


def _affirmed(body: str, pattern: str) -> list[str]:
    """Sentences of `body` that state `pattern` without a negation governing
    it in its own clause ("No checkpoints are mandatory" does not state that
    checkpoints are mandatory)."""
    return [s for s in _sentences(body)
            for m in [re.search(pattern, s, re.IGNORECASE)]
            if m and not _negated_at(s, m.start())]


def _asks_when(body: str, subject: str) -> list[str]:
    return [s for s in _sentences(body)
            if re.search(r"\bask|AskUserQuestion", s, re.IGNORECASE)
            and re.search(subject, s, re.IGNORECASE)
            and _ambiguity_condition(s)]


# ── the helpers themselves: word boundaries and negation ─────────────────────

@pytest.mark.parametrize(("sentence", "expected"), [
    ("Ask only when the type is genuinely ambiguous.", True),
    ("Ask which item only when it is ambiguous (no id given).", True),
    ("Ask when several projects fit.", True),
    ("On an unambiguous instruction, wait for confirmation.", False),
    ("When the phrase is not ambiguous, ask to confirm.", False),
    ("Wait for the user's go-ahead on every write.", False),
])
def test_ambiguity_condition_respects_word_boundaries_and_negation(sentence, expected):
    assert _ambiguity_condition(sentence) is expected


@pytest.mark.parametrize("sentence", [
    "On an unambiguous instruction, wait for confirmation.",
    "On an unambiguous request, ask the user to confirm the write.",
])
def test_a_confirmation_on_an_unambiguous_instruction_is_caught(sentence):
    """The hole the old pattern had: "unambiguous" exempted the very
    sentence the K-7 pin exists to catch."""
    assert _confirmation_requests(sentence) == [sentence]


@pytest.mark.parametrize(("text", "expected"), [
    ("No checkpoints are mandatory at creation.", []),
    ("Checkpoints are never needed here; checkpoints are mandatory at creation.",
     ["Checkpoints are never needed here; checkpoints are mandatory at creation."]),
    ("Checkpoints are mandatory at creation.", ["Checkpoints are mandatory at creation."]),
])
def test_affirmed_respects_negation(text, expected):
    assert _affirmed(text, r"\bcheckpoints are mandatory\b") == expected


@pytest.mark.parametrize(("text", "subject"), [
    pytest.param(lambda: _skill("track"), r"\btype\b", id="track-skill-type"),
    pytest.param(lambda: _skill("track"), r"\bproject\b", id="track-skill-project"),
    pytest.param(lambda: _fallback("track"), r"\b(item_)?type\b", id="track-fallback-type"),
    pytest.param(lambda: _fallback("track"), r"\bproject\b", id="track-fallback-project"),
    pytest.param(lambda: _skill("clog"), r"\bcategory\b", id="clog-skill-category"),
    pytest.param(lambda: _skill("clog"), r"\bproject\b", id="clog-skill-project"),
    pytest.param(lambda: _fallback("clog"), r"\bcategory\b", id="clog-fallback-category"),
    pytest.param(lambda: _fallback("clog"), r"\bproject\b", id="clog-fallback-project"),
    pytest.param(lambda: _skill("track-update"), r"\b(item|id)s?\b",
                 id="track-update-skill-item"),
    pytest.param(lambda: _skill("track-resolve"), r"\b(item|id)s?\b",
                 id="track-resolve-skill-item"),
    pytest.param(lambda: _fallback("track-resolve"), r"\b(item|id)s?\b",
                 id="track-resolve-fallback-item"),
])
def test_a_question_stays_only_where_the_skill_cannot_tell(text, subject):
    assert _asks_when(text(), subject), \
        f"no conditional question for an ambiguous {subject!r}"


@pytest.mark.parametrize("text", CHEAP_WRITES)
def test_every_question_of_a_cheap_write_is_conditional(text):
    """Every AskUserQuestion these skills still name is tied to an
    ambiguity — none is a standing step of the unambiguous path."""
    unconditional = [s for s in _sentences(text())
                     if "AskUserQuestion" in s
                     and not _ambiguity_condition(s)
                     and not re.search(r"\bonly\b|\bwhen\b|\bif\b", s, re.IGNORECASE)]
    assert not unconditional, unconditional


# ═════════════════════════════════════════════════════════════════════════════
# H-6 (b) — checkpoints when work starts, at least three, never a cap
# ═════════════════════════════════════════════════════════════════════════════

CHECKPOINT_TEXTS = [
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param(_snippet, id="claude-md-snippet"),
]

# "3-7" read as a range caps the plan at seven; the owner's rule reads it
# as a MINIMUM — so the range may appear only where the same sentence says
# so. An explicit cap is never allowed.
RANGE = re.compile(r"\b3\s*[-–]\s*7\b|three to seven", re.IGNORECASE)
RANGE_AS_MINIMUM = re.compile(r"\bminimum\b|\bat least\b|never a cap", re.IGNORECASE)
CAP = re.compile(r"\bat most\b[^.]{0,40}checkpoint|checkpoint[^.]{0,60}\bat most\b|"
                 r"\bup to (7|seven)\b|no more than[^.]{0,40}checkpoint|"
                 r"\bmaximum of\b[^.]{0,40}checkpoint", re.IGNORECASE)


@pytest.mark.parametrize("text", CHECKPOINT_TEXTS)
def test_checkpoint_count_is_a_minimum_never_a_range_or_cap(text):
    for s in _sentences(text()):
        assert not CAP.search(s), f"a checkpoint cap is stated: {s!r}"
        if RANGE.search(s):
            assert RANGE_AS_MINIMUM.search(s), f"a checkpoint range reads as a cap: {s!r}"


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(_snippet, id="claude-md-snippet"),
])
def test_creating_an_item_does_not_force_checkpoints(text):
    body = text()
    flat = _flat(body)
    assert not re.search(r"^#{2,4} Step \d+: Sketch checkpoints", body, re.MULTILINE), \
        "/track still has a checkpoint step at creation"
    assert not _affirmed(body, r"\bcheckpoints are mandatory\b"), "checkpoints are still mandatory"
    assert not re.search(r"immediately after `?(mcp__dct__)?create_item`?[^.]{0,120}checkpoint",
                         flat, re.IGNORECASE), "checkpoints are still forced right after the create"
    assert not re.search(r"(for|if) [`\w ]*feature[ /,]+improvement[^.]{0,120}"
                         r"(add_checkpoint|break (the )?work into)", flat, re.IGNORECASE), \
        "feature / improvement items still get checkpoints at creation"


# The start of work on an item. "Before starting any code" is the OLD
# creation-time wording — it does not say the plan waits for the start.
START_OF_WORK = (r"\b(when|once|as) (the )?(work|you|the user|someone)[^.]{0,40}\bstart|"
                 r"\bstart(s|ing)? (the )?work\b|work (on [^.]{0,30})?starts\b|"
                 r"in_progress|/track-update")


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(_snippet, id="claude-md-snippet"),
])
def test_creation_points_the_plan_to_the_start_of_work(text):
    """The plan is not dropped, it moves: the text says checkpoints are
    proposed when work on the item starts."""
    hits = [s for s in _sentences(text())
            if re.search(r"checkpoint", s, re.IGNORECASE)
            and re.search(START_OF_WORK, s, re.IGNORECASE)]
    assert hits, "nothing says the checkpoint plan comes when work starts"


AT_LEAST_THREE = r"\bat least (3|three)\b|≥\s*3\b|>=\s*3\b"


def _start_work_paragraphs() -> list[str]:
    return [p for p in _paragraphs(_skill("track-update"))
            if re.search(r"in_progress|\bstart", p, re.IGNORECASE)
            and re.search(r"checkpoint", p, re.IGNORECASE)]


def test_track_update_start_work_proposes_at_least_three_checkpoints():
    paragraphs = _start_work_paragraphs()
    assert paragraphs, "the start-work path says nothing about checkpoints"
    proposing = [p for p in paragraphs
                 if re.search(r"propos", p, re.IGNORECASE)
                 and re.search(AT_LEAST_THREE, p, re.IGNORECASE)]
    assert proposing, f"no proposal of at least 3 checkpoints on start: {paragraphs}"
    assert any(re.search(r"add_checkpoint", p) for p in paragraphs), \
        "the proposed plan is not written with add_checkpoint"


def test_track_update_start_work_respects_an_existing_plan():
    """An item that already carries its checkpoints is not given a second,
    duplicate plan when work on it starts."""
    paragraphs = _start_work_paragraphs()
    existing = (r"\b(already|existing)\b[^.]{0,60}checkpoint|"
                r"checkpoint[^.]{0,60}\b(already|existing)\b")
    assert any(re.search(existing, p, re.IGNORECASE) for p in paragraphs), \
        f"the start-work proposal ignores the item's existing checkpoints: {paragraphs}"


def test_snippet_proposes_at_least_three_checkpoints_when_work_starts():
    """The snippet lands in every user's CLAUDE.md: it must carry the same
    rule as the skills — at least 3, and at the start of work."""
    paragraphs = [p for p in _paragraphs(_snippet())
                  if re.search(r"checkpoint", p, re.IGNORECASE)
                  and re.search(AT_LEAST_THREE, p, re.IGNORECASE)]
    assert paragraphs, "the CLAUDE.md snippet does not state the at-least-3 plan"
    assert any(re.search(START_OF_WORK, p, re.IGNORECASE) for p in paragraphs), \
        f"the at-least-3 plan is not tied to the start of work: {paragraphs}"


# ═════════════════════════════════════════════════════════════════════════════
# H-7 — /pickup consumes the single matching handoff without asking
# ═════════════════════════════════════════════════════════════════════════════

PICKUP = [
    pytest.param(lambda: _skill("pickup"), id="skill"),
    pytest.param(lambda: _fallback("pickup"), id="fallback"),
]

SINGLE = r"\b(exactly one|exactly ONE|one matching|a single|single matching|only one)\b"
NO_QUESTION = r"\b(without (a |any )?(question|asking|confirmation)|no question)\b"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_consumes_the_single_matching_handoff_without_a_question(text):
    hits = [p for p in _paragraphs(text())
            if re.search(SINGLE, p, re.IGNORECASE)
            and re.search(r"consum", p, re.IGNORECASE)
            and re.search(NO_QUESTION, p, re.IGNORECASE)
            and re.search(r"\bunambiguous\b|\bcontinue\b|\"pickup\"|/pickup\b", p,
                          re.IGNORECASE)]
    assert hits, "a single matching handoff on an unambiguous request still waits for a question"


def _report_part(body: str) -> str:
    """The skill's Report section, or the fallback's sentences that tell
    what to report."""
    if re.search(r"^#{2,4} [^\n]*Report", body, re.MULTILINE):
        return _flat(_heading_section(body, "Report"))
    return " ".join(s for s in _sentences(body)
                    if re.search(r"\b(report|tell|name|say|state)\w*", s, re.IGNORECASE))


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_report_names_the_consumed_handoff_id(text):
    """Without a question the consume is silent unless the report says it:
    the report names the consumed handoff by its id."""
    part = _report_part(text())
    assert re.search(r"consum\w*[^.]{0,60}(#N\b|#<id>|\bits id\b)|"
                     r"(#N\b|#<id>)[^.]{0,60}consum", part, re.IGNORECASE), \
        f"the report does not say which handoff was consumed: {part[:300]}"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_still_asks_which_among_several_handoffs(text):
    flat = _flat(text())
    assert re.search(r"(several|more than one|>= ?2|≥ ?2|two or more)[^.]{0,40}handoff"
                     r".{0,200}?\b(ask|which|AskUserQuestion)\b", flat, re.IGNORECASE), \
        "several matching handoffs are no longer a question"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_still_prints_the_handoff_prompt_as_plain_text(text):
    """No question about the handoff does not mean no reading: the prompt
    the user must read is printed as plain message text, never inside a
    dialog (the dialog truncates long text)."""
    hits = [s for s in _sentences(text())
            if re.search(r"prompt", s, re.IGNORECASE)
            and re.search(r"plain (message )?text", s, re.IGNORECASE)]
    assert hits, "the handoff prompt is no longer printed as plain text"


def test_pickup_skill_drops_the_single_handoff_dialog():
    body = _skill("pickup")
    flat = _flat(body)
    for old in ("Pick up handoff #N shown above?", '"Consume and follow"',
                "Consumption happens ONLY after the dialog"):
        assert old not in flat, f"the single-handoff confirmation is still there: {old!r}"
    assert not re.search(r"consume only after the user says so", flat, re.IGNORECASE)
    assert not re.search(r"never makes that call on its own", flat, re.IGNORECASE)
    assert not re.search(r'NEVER pass `consume="true"` without a user-chosen', flat), \
        "the consume still waits for a user-chosen handoff_id"


def test_pickup_fallback_drops_the_single_handoff_choice():
    flat = _flat(_fallback("pickup"))
    assert not re.search(r"let the user decide: consume and follow", flat, re.IGNORECASE)
    assert not re.search(r"only if the user says so", flat, re.IGNORECASE)


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_consume_path_does_not_contradict_its_own_rules(text):
    """The no-question consume may be the server's AUTO mode (`consume=""`,
    which consumes only when exactly one handoff matches) or an explicit
    `consume="true", handoff_id=N` of the handoff just read. Either is
    fine; what is not is using auto mode while still reserving it for
    unattended flows only."""
    flat = _flat(text())
    uses_auto = re.search(r'pickup_work\([^)]*consume=""', flat)
    by_id = re.search(r'consume="true",\s*handoff_id=N', flat)
    assert uses_auto or by_id, "no consume call is named"
    if uses_auto:
        reserved = re.search(r'consume=""[^.]{0,200}(never this skill|belongs? to AUTONOMOUS)',
                             flat, re.IGNORECASE)
        assert not reserved, f"auto consume is used AND reserved for unattended flows: {reserved}"


# ═════════════════════════════════════════════════════════════════════════════
# /commit — one screen: Commit / Change / Stop
# ═════════════════════════════════════════════════════════════════════════════


def _commit_fallback() -> str:
    return _fallback("commit")


COMMIT = [
    pytest.param(lambda: _skill("commit"), id="skill"),
    pytest.param(_commit_fallback, id="fallback"),
]


STOP_OPTION = re.compile(r"[\"“`*]Stop\b|/\s*[\"“`*]?Stop\b")


def _screen_unit(body: str) -> str:
    """The step that offers the screen's Stop option: the skill's heading
    section around it, or — in the fallback, whose steps are one numbered
    line each — that numbered step."""
    m = STOP_OPTION.search(body)
    assert m, "no Stop option"
    pos = m.start()
    headings = [h.start() for h in re.finditer(r"^#{2,4} ", body, re.MULTILINE)
                if h.start() <= pos]
    start = headings[-1] if headings else 0
    if body.startswith("## `/", start):  # the fallback section: narrow to the step
        steps = [st.start() for st in re.finditer(r"^\d+\. ", body, re.MULTILINE)
                 if st.start() <= pos]
        start = steps[-1] if steps else start
        nxt = re.search(r"^(\d+\. |#{2,4} )", body[pos:], re.MULTILINE)
    else:
        level = len(re.match(r"#+", body[start:]).group(0)) if headings else 2
        nxt = re.search(rf"^#{{1,{level}}} ", body[pos:], re.MULTILINE)
    end = pos + nxt.start() if nxt else len(body)
    return _flat(body[start:end])


@pytest.mark.parametrize("text", COMMIT)
def test_commit_offers_one_screen_commit_change_stop(text):
    flat = _flat(text())
    for label in ("Commit", "Change", "Stop"):
        assert _option_offered(flat, label), f"the screen does not offer {label!r}"
    assert re.search(r"\bone (screen|dialog|confirmation)\b|\bONE (screen|dialog)\b", flat), \
        "the dialog is not described as one screen"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_screen_shows_files_message_and_entries(text):
    window = _screen_unit(text())
    assert re.search(r"\bfiles\b", window, re.IGNORECASE), "the screen does not list the files"
    assert re.search(r"\bmessage\b", window, re.IGNORECASE), "the screen does not show the message"
    assert re.search(r"\bentr(y|ies)\b|Changelog:", window), \
        "the screen does not name the entries the commit marks as done"


def test_commit_skill_has_no_separate_staging_and_message_approvals():
    flat = _flat(_skill("commit"))
    for old in (r"show the staging plan, get approval", r"show the message draft, get approval",
                r"approval of the staging plan AND message draft"):
        assert not re.search(old, flat, re.IGNORECASE), f"a second approval is still there: {old}"


def test_commit_screen_comes_after_the_selection_and_before_the_commit():
    """The trailer is chosen from the FINAL index, the screen shows it, and
    only then `git commit` runs — the screen cannot show entries chosen
    before the staging."""
    flat = _flat(_skill("commit"))
    candidates = flat.index("dct clog candidates --json")
    screen = STOP_OPTION.search(flat)
    assert screen, "no Stop option"
    commit = flat.index('git commit -m "$(cat')
    assert candidates < screen.start() < commit, (candidates, screen.start(), commit)


@pytest.mark.parametrize("text", COMMIT)
def test_commit_staging_question_only_on_a_real_choice(text):
    hits = [p for p in _paragraphs(text())
            if re.search(r"stag", p, re.IGNORECASE)
            and re.search(r"\bask|question|AskUserQuestion", p, re.IGNORECASE)
            and re.search(r"\bonly\b", p, re.IGNORECASE)
            and re.search(r"blacklist|suspicious|secret", p, re.IGNORECASE)
            and re.search(r"belong", p, re.IGNORECASE)]
    assert hits, "the staging question is not limited to a real choice " \
        "(files not belonging to the entries; a blacklisted path is never an option)"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_change_can_still_drop_a_trailer_for_one_commit(text):
    """K-55 replaced "Co-Authored-By on by default, removable through
    Change" with the stored preference; "Change" still edits the message of
    THIS commit, a trailer included, without touching what is stored."""
    hits = [p for p in _paragraphs(text())
            if re.search(r"\btrailer\b", p, re.IGNORECASE)
            and re.search(r"[\"“`*]Change\b|\bChange\b", p)
            and re.search(r"remov|drop|omit|without", p, re.IGNORECASE)]
    assert hits, "\"Change\" can no longer drop a trailer from one commit"


def test_commit_skill_no_longer_forces_co_authored_by():
    flat = _flat(_skill("commit"))
    assert not re.search(r"ALWAYS include `?Co-Authored-By", flat), \
        "Co-Authored-By is still unconditional"
    example = re.search(r'git commit -m "\$\(cat <<\'EOF\'(.*?)EOF', _skill("commit"), re.DOTALL)
    assert example and "Co-Authored-By:" in example.group(1), \
        "the default message no longer carries Co-Authored-By"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_change_that_touches_the_files_reselects_the_trailer(text):
    """"Change" may alter the staged files; the trailer attests the final
    index, so such a change redoes the selection before the commit."""
    hits = [p for p in _paragraphs(text())
            if re.search(r"[\"“`*]Change\b|\bChange\s*(→|->|:)", p)
            and re.search(r"\bfiles?\b|stag|index", p, re.IGNORECASE)
            and re.search(r"redo|re-?select|select(ion)? again|again|stale", p, re.IGNORECASE)]
    assert hits, "a Change of the files does not redo the trailer selection"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_stop_commits_nothing(text):
    hits = [s for s in _sentences(text())
            if re.search(r"[\"“`*]Stop\b|\bStop\s*(→|->|:)", s)
            and re.search(r"nothing|no commit|not commit|never commit", s, re.IGNORECASE)]
    assert hits, "the Stop option does not say that nothing is committed"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_never_commits_without_the_owners_commit_answer(text):
    """The old safety rule ("never commit without approval of the staging
    plan AND the message") is not dropped, it moves onto the one screen:
    nothing is committed without the owner's "Commit" on it."""
    hits = [s for s in _sentences(text())
            if re.search(r"\bnever commit\b[^.]{0,40}\bwithout\b|\bcommit\w* only after\b",
                         s, re.IGNORECASE)
            and re.search(r"[\"“`*]Commit\b|\bscreen\b", s)]
    assert hits, "no rule says nothing is committed without the owner's Commit on the screen"


def test_commit_keeps_every_safety_rule():
    flat = _flat(_skill("commit"))
    assert re.search(r"NEVER stage `\.env`", flat) and "Blacklist" in flat
    assert "NEVER use `git add -A`" in flat
    assert re.search(r"NEVER select the `Changelog:` trailer before the staging is final", flat)
    assert "NEVER use `--amend`" in flat and "NEVER use `--no-verify`" in flat
    fallback = _flat(_commit_fallback())
    assert "Never `git add -A`" in fallback and "never `--amend`" in fallback
    assert "never `--no-verify`" in fallback


# ═════════════════════════════════════════════════════════════════════════════
# /handoff — one question: Save / Change / Cancel
# ═════════════════════════════════════════════════════════════════════════════

HANDOFF = [
    pytest.param(lambda: _skill("handoff"), id="skill"),
    pytest.param(lambda: _fallback("handoff"), id="fallback"),
]


@pytest.mark.parametrize("text", HANDOFF)
def test_handoff_asks_one_save_change_cancel_question(text):
    flat = _flat(text())
    for label in ("Save", "Change", "Cancel"):
        assert _option_offered(flat, label), f"the question does not offer {label!r}"


def test_handoff_fallback_shows_sprint_and_scope_as_lines_of_the_draft():
    """The defaults ("sprint unchanged", "no scope") are read in the draft
    and changed through Change — not asked as questions of their own."""
    hits = [x for x in _sentences(_fallback("handoff"))
            if re.search(r"\bdraft\b", x, re.IGNORECASE)
            and re.search(r"\bsprint\b", x, re.IGNORECASE)
            and re.search(r"\bscope\b", x, re.IGNORECASE)
            and re.search(r"\blines?\b", x, re.IGNORECASE)
            and re.search(r"unchanged|untouched", x, re.IGNORECASE)
            and re.search(r"no scope|scope:?\s*(none|—)", x, re.IGNORECASE)]
    assert hits, "the fallback does not show 'sprint unchanged' / 'no scope' as draft lines"


def test_handoff_skill_has_no_separate_sprint_and_scope_questions():
    flat = _flat(_skill("handoff"))
    for old in ("Sprint membership for the next session?",
                "Optional scope tag (disambiguates parallel sessions)?",
                "up to 4 questions"):
        assert old not in flat, f"a separate setup question is still there: {old!r}"


def test_handoff_skill_draft_block_carries_sprint_and_scope_lines():
    """Turn 1 prints the draft: the sprint and scope decisions are lines in
    it (read before the one question), not dialog options."""
    body = _skill("handoff")
    blocks = [b for b in re.findall(r"```\n?(.*?)```", body, re.DOTALL) if "BEGIN DRAFT" in b]
    assert blocks, "no printed draft block"
    assert any(re.search(r"^\s*Sprint:\s*(unchanged|untouched)", b, re.MULTILINE | re.IGNORECASE)
               and re.search(r"^\s*Scope:\s*(none|—|-)", b, re.MULTILINE | re.IGNORECASE)
               for b in blocks), \
        f"the draft does not carry the default lines 'Sprint: unchanged' / 'Scope: none': {blocks}"


def test_handoff_change_prints_the_revision_before_saving():
    """Change keeps the two-turn rule: the revised draft is printed again and
    only then asked about."""
    hits = [p for p in _paragraphs(_skill("handoff"))
            if re.search(r"[\"“`*]Change\b", p)
            and re.search(r"revis|again|repeat", p, re.IGNORECASE)
            and re.search(r"print|Turn 1", p, re.IGNORECASE)]
    assert hits, "a Change is saved without printing the revision first"


def test_handoff_cancel_writes_nothing():
    hits = [s for s in _sentences(_skill("handoff"))
            if re.search(r"[\"“`*]Cancel\b|\bCancel\s*(→|->|:)", s)
            and re.search(r"nothing|no (handoff|write)|not (written|saved)", s, re.IGNORECASE)]
    assert hits, "Cancel does not say that nothing is written"


def test_handoff_keeps_the_conditional_target_sprint_question():
    """The questions that remain are conditional: several active sprints and
    a membership change → which sprint; handoff_work refuses to guess."""
    flat = _flat(_skill("handoff"))
    assert re.search(r"active_sprint_count >= 2|several active sprints|≥ 2 active sprints", flat)
    assert re.search(r"handoff_work` REFUSES to guess|errors rather than guessing", flat)


def test_handoff_keeps_the_sprint_and_the_two_turn_safety_rules():
    """One question does not loosen what the question guards: the sprint
    changes only as the saved draft says, and the draft the user approves
    is printed before the dialog, never inside it."""
    flat = _flat(_skill("handoff"))
    assert re.search(r"never modify sprint membership without", flat, re.IGNORECASE)
    assert "Long text NEVER goes inside an `AskUserQuestion`" in flat
    assert "NEVER send `item_ids=\"\"`" in flat


# ═════════════════════════════════════════════════════════════════════════════
# Triage #877, round 2 — the everyday skills (K-52 … K-67)
# ═════════════════════════════════════════════════════════════════════════════

TERMINAL = r"\b(terminal|resolved|wont_fix|won'?t[ _]fix|close[sd]?|closing)\b"


# ── K-52: closing goes through the gate check; gates are never auto-rejected ──

def test_track_update_routes_closing_to_the_track_resolve_procedure():
    """"#42 resolved" / "close #42" / "wont fix #45" through /track-update
    follows /track-resolve (get_item → pending gate → stop → resolve_item);
    no sentence tells the assistant to write a terminal status with
    update_item."""
    body = _skill("track-update")
    routes = [s for s in _sentences(body)
              if re.search(TERMINAL, s, re.IGNORECASE)
              and re.search(r"/track-resolve", s)
              and re.search(r"resolve_item|gate", s)]
    assert routes, "closing an item is not routed to the /track-resolve procedure"
    writes = [s for s in _sentences(body)
              for m in [re.search(r"update_item", s)]
              if m and re.search(TERMINAL, s, re.IGNORECASE)
              and not _negated_at(s, m.start())
              and not re.search(r"status=[\"']?open|reopen", s, re.IGNORECASE)]
    assert not writes, f"a terminal status is still written with update_item: {writes}"


GATE_SKILLS = [
    pytest.param(lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param(lambda: _skill("track-resolve"), id="track-resolve-skill"),
    pytest.param(lambda: _fallback("track-resolve"), id="track-resolve-fallback"),
]


@pytest.mark.parametrize("text", GATE_SKILLS)
def test_a_gate_is_never_rejected_on_the_assistants_own_initiative(text):
    """reject_checkpoint drops a gate only on the user's own decision (the
    "skip it" answer, or a step the user removes from the plan) — never as a
    way around a refused close."""
    for s in _sentences(text()):
        if "reject_checkpoint" in s:
            assert re.search(r"\b(user|owner)\b|skip it", s, re.IGNORECASE), \
                f"reject_checkpoint without the user's decision: {s}"


# ── K-67 (2): the gate stop in plain words ───────────────────────────────────

GATE_STOP = [
    pytest.param(lambda: _skill("track-resolve"), id="skill"),
    pytest.param(lambda: _fallback("track-resolve"), id="fallback"),
]


def _stop_template(body: str) -> str:
    """The sentence the user reads at the stop: the quoted / fenced line that
    offers "done" and "skip it"."""
    candidates = re.findall(r"```[a-z]*\n(.*?)```|\"([^\"\n]{40,}?)\"", body, re.DOTALL)
    texts = [a or b for a, b in candidates]
    hits = [t for t in texts if re.search(r"\bdone\b", t) and re.search(r"\bskip it\b", t)]
    assert hits, "no plain-words stop template offering 'done' / 'skip it'"
    return hits[0]


@pytest.mark.parametrize("text", GATE_STOP)
def test_the_gate_stop_speaks_plainly(text):
    template = _stop_template(text())
    assert "mcp__" not in template and "_checkpoint" not in template, template
    assert not re.search(r"\b(dogfood|kind|gate)\b", template, re.IGNORECASE), \
        f"internal vocabulary in the user's sentence: {template}"


@pytest.mark.parametrize("text", GATE_STOP)
def test_the_answer_to_the_gate_stop_is_the_instruction(text):
    """"done" completes, "skip it" rejects — then resolve_item in the same
    turn, with no further question."""
    flat = _flat(text())
    assert re.search(r"\"?done\"?[^.]{0,80}complete_checkpoint", flat), flat
    assert re.search(r"\"?skip it\"?[^.]{0,80}reject_checkpoint", flat), flat
    assert re.search(r"resolve_item[^.]{0,80}same turn|same turn[^.]{0,80}resolve_item", flat), \
        "the answer does not close the item in the same turn"


@pytest.mark.parametrize("text", GATE_STOP)
def test_done_never_bypasses_evidence_or_closes_an_unnamed_gate(text):
    sentences = _sentences(text())
    only_named = [s for s in sentences
                  if re.search(r"\"?done\"?|\"?skip it\"?", s)
                  and re.search(r"\bonly\b|\bnever\b", s, re.IGNORECASE)
                  and re.search(r"\bnamed\b|\blisted\b|\bmarked\b", s, re.IGNORECASE)]
    assert only_named, "an answer may close a gate the stop did not name"
    evidence = [s for s in sentences
                if re.search(r"\bevidence\b|\bproof\b|\bsign-?off\b", s, re.IGNORECASE)
                and re.search(r"\bnever\b|\bnot\b|\bno\b", s, re.IGNORECASE)]
    assert evidence, "\"done\" may stand in for the evidence a gate requires"


# ── K-67 (1): no type cue → todo; a question only when it changes the scope ──

TYPE_DEFAULT = [
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(_snippet, id="claude-md-snippet"),
]


@pytest.mark.parametrize("text", TYPE_DEFAULT)
def test_no_type_cue_means_todo_without_a_question(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\b(no|without (a|any))\b[^.]{0,20}\b(type )?cue", s, re.IGNORECASE)
            and re.search(r"`?todo`?", s)
            and re.search(NO_QUESTION + r"|\bnot ask\b|\bnever ask", s, re.IGNORECASE)]
    assert hits, "a phrase without a type cue is not defaulted to todo without a question"


@pytest.mark.parametrize("text", TYPE_DEFAULT)
def test_the_type_question_needs_two_types_that_change_the_scope(text):
    asks = [s for s in _sentences(text())
            if re.search(r"\bask\w*\b", s, re.IGNORECASE)
            and re.search(r"\btype\b", s, re.IGNORECASE)
            and not _negated_at(s, re.search(r"\bask\w*\b", s, re.IGNORECASE).start())]
    assert asks, "no conditional type question"
    for s in asks:
        assert re.search(r"\btwo (different )?types\b", s, re.IGNORECASE) \
            and re.search(r"\bscope\b", s, re.IGNORECASE), \
            f"the type question is not limited to two types that change the scope: {s}"


# ── K-65: the plan of checkpoints — non-trivial feature/improvement or a breakdown ──

def _server_texts() -> dict[str, str]:
    from dct.mcp import server
    return {"instructions": server._INSTRUCTIONS,
            "create_item": server.create_item.__doc__ or "",
            "add_checkpoint": server.add_checkpoint.__doc__ or ""}


PLAN_RULE_TEXTS = [
    pytest.param(lambda: _server_texts()["instructions"], id="server-instructions"),
    pytest.param(lambda: _server_texts()["create_item"], id="create_item-doc"),
    pytest.param(lambda: _server_texts()["add_checkpoint"], id="add_checkpoint-doc"),
    pytest.param(lambda: _skill("track"), id="track-skill"),
    pytest.param(lambda: _fallback("track"), id="track-fallback"),
    pytest.param(lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param(_snippet, id="claude-md-snippet"),
]

NON_TRIVIAL_FEATURE = (r"\bnon-trivial\b[^.]{0,40}\bfeature[^.]{0,30}\bimprovement|"
                       r"\bnon-trivial\b[^.]{0,40}\bimprovement[^.]{0,30}\bfeature")
BREAKDOWN = r"\bbreak(s|ing)? (the |this |it |its )?(work )?down\b|\bbreakdown\b"


def _plan_rule_units(body: str) -> list[str]:
    """Each statement of the minimum: the paragraph carrying "at least 3"
    with "checkpoint" (a paragraph, because a rule may wrap a line list)."""
    return [p for p in _paragraphs(body)
            if re.search(AT_LEAST_THREE, p, re.IGNORECASE)
            and re.search(r"checkpoint", p, re.IGNORECASE)]


@pytest.mark.parametrize("text", PLAN_RULE_TEXTS)
def test_the_minimum_of_three_has_one_shared_qualifier(text):
    """Every place that states the at-least-3 plan ties it to the start of
    NON-TRIVIAL feature/improvement work or to an explicit request to break
    the work down — never to every item, never to the creation."""
    units = _plan_rule_units(text())
    assert units, "the at-least-3 plan is not stated here"
    for unit in units:
        assert re.search(NON_TRIVIAL_FEATURE, unit, re.IGNORECASE), \
            f"the minimum is not limited to non-trivial feature/improvement work: {unit}"
        assert re.search(BREAKDOWN, unit, re.IGNORECASE), \
            f"the explicit breakdown request is missing: {unit}"
        assert not re.search(r"\b(every|any|each) item\b|an item without checkpoints gets",
                             unit, re.IGNORECASE), f"the minimum still applies to any item: {unit}"


@pytest.mark.parametrize("text", [
    pytest.param(lambda: _skill("track-update"), id="track-update-skill"),
    pytest.param(_snippet, id="claude-md-snippet"),
])
def test_a_small_fix_gets_no_artificial_plan(text):
    """A one-line fix or a small bug starts without checkpoints; a complex
    issue or todo may get some, with no minimum taken from its type."""
    sentences = _sentences(text())
    small = [s for s in sentences
             if re.search(r"\b(small|one-line|trivial)\b", s, re.IGNORECASE)
             and re.search(r"\b(no|none|without)\b[^.]{0,40}checkpoints?\b|"
                           r"checkpoints?\b[^.]{0,40}\b(none|not needed)\b", s, re.IGNORECASE)]
    assert small, "a small fix is still given a plan of checkpoints"
    no_minimum = [s for s in sentences
                  if re.search(r"\bno (fixed )?minimum\b", s, re.IGNORECASE)
                  and re.search(r"\b(issue|todo|other)\b", s, re.IGNORECASE)]
    assert no_minimum, "other items still get a minimum taken from their type"


# ── K-53: the snippet points to /release, no third copy of the CLI ───────────
# (in test_skill_prose_changelog.py, next to the other snippet / release pins)


# ── K-54 / K-56: /commit — one secret-path predicate, new files like any other ─

def test_commit_secret_paths_come_from_the_shared_predicate():
    """/commit reads `secret_like` from `dct clog candidates --json` — the
    release preview's predicate — and, where the CLI cannot run, the hand
    list it names is exactly the code's."""
    from dct.workflows.release.plan import (
        CANDIDATE_KEYS,
        SECRET_LIKE_PATTERNS,
        SECRET_LIKE_TEMPLATES,
    )
    assert "secret_like" in CANDIDATE_KEYS
    for body in (_skill("commit"), _commit_fallback()):
        flat = _flat(body)
        assert "`secret_like`" in flat and "dct clog candidates --json" in flat
        for pattern in SECRET_LIKE_PATTERNS + SECRET_LIKE_TEMPLATES:
            assert f"`{pattern}`" in flat, f"the hand list misses {pattern}"
        assert re.search(r"\b(not|never) a (full )?content scanner\b|"
                         r"\bnames? only\b|\bby (its|the file) name\b", flat, re.IGNORECASE), \
            "the predicate is presented as more than a path heuristic"


@pytest.mark.parametrize("path, expected", [
    (".env", True), ("config/.env.production", True), ("certs/server.pem", True),
    (".env.example", False), (".env.sample", False), ("config/settings.toml", False),
    ("credentials.json", True), ("docs/credentials.md", False),
])
def test_the_predicate_flags_secrets_and_keeps_example_files(path, expected):
    """The policy the prose promises: templates and ordinary config files are
    ordinary changes — the heuristic is no ban on every configuration."""
    from dct.workflows.release.plan import secret_like
    assert secret_like(path) is expected


@pytest.mark.parametrize("text", COMMIT)
def test_commit_never_offers_a_secret_like_path_for_staging(text):
    hits = [s for s in _sentences(text())
            if re.search(r"secret", s, re.IGNORECASE)
            and re.search(r"\b(never|not)\b[^.]{0,60}\b(offer|stage)", s, re.IGNORECASE)
            and re.search(r"\bunstaged\b|\buntracked\b|\bnot staged\b", s, re.IGNORECASE)]
    assert hits, "an unstaged secret-like path may still be offered for staging"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_stops_on_a_staged_secret_without_touching_the_index(text):
    paragraphs = [p for p in _paragraphs(text())
                  if re.search(r"\bstaged\b", p) and re.search(r"secret", p, re.IGNORECASE)
                  and re.search(r"\bSTOP|\bstops?\b", p)]
    assert paragraphs, "a staged secret-like path does not stop the commit"
    unit = " ".join(paragraphs)
    assert "`unstage`" in unit, "the stop does not give the quoted unstage command"
    assert re.search(r"\bnever (runs?|changes?|touch\w*)\b[^.]{0,60}\b(index|it|command)", unit,
                     re.IGNORECASE), "the stop may change the user's index itself"


def test_the_unstage_command_the_stop_shows_is_quoted_with_the_separator():
    """Bidirectional: the command the skill tells the assistant to show is
    the code's `unstage`, which carries `--` and survives any path."""
    import shlex

    from dct.workflows.release.plan import secret_like_changes
    for path in (".env", "a dir/.env", "-x/.env", "it's/.env"):
        state = {"staged": [path], "unstaged": [], "untracked": []}
        [row] = secret_like_changes("/nonexistent", state, "0" * 40)
        argv = shlex.split(row["unstage"])
        assert argv == ["git", "restore", "--staged", "--", path], row
        [first] = secret_like_changes("/nonexistent", state, None)
        assert shlex.split(first["unstage"])[-2:] == ["--", path], first
    for body in (_skill("commit"), _commit_fallback()):
        for cmd in re.findall(r"`(git (?:restore --staged|rm --cached)[^`]*)`", body):
            assert " -- " in cmd, f"an unstage command without the -- separator: {cmd}"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_new_files_follow_the_same_scope_rules(text):
    """K-56: a commit of new files only (a new module, a first commit) gets
    the one screen like any other — no standing "track them?" question. A
    question only on a real choice."""
    sentences = _sentences(text())
    same = [s for s in sentences
            if re.search(r"\buntracked\b|\bnew files?\b", s, re.IGNORECASE)
            and re.search(r"\b(like any other|same (scope )?rules|same as)\b", s, re.IGNORECASE)]
    assert same, "new files are not said to follow the same staging rules"
    for s in sentences:
        m = re.search(r"\bask\w*\b|AskUserQuestion|\bquestion\b", s, re.IGNORECASE)
        if (m and re.search(r"\buntracked\b|\bnew files?\b|\btrack them\b", s, re.IGNORECASE)
                and not _negated_at(s, m.start())):
            assert re.search(r"real choice|not all belong|do not all|ambigu", s, re.IGNORECASE), \
                f"an unconditional question about new files: {s}"


# ── K-55: stored commit-trailer preferences ─────────────────────────────────

COMMIT_AND_SNIPPET = [
    *COMMIT,
    pytest.param(lambda: _section_between(_snippet(), "**Committing**", "**Releasing**"),
                 id="claude-md-snippet"),
]


def _section_between(body: str, start: str, end: str) -> str:
    return body[body.index(start): body.index(end, body.index(start))]


@pytest.mark.parametrize("text", COMMIT_AND_SNIPPET)
def test_commit_applies_the_stored_trailer_preferences_without_asking(text):
    flat = _flat(text())
    assert re.search(r"\bstored\b[^.]{0,120}\b(without (a |any )?(question|asking)|"
                     r"no question|never asks?)\b", flat, re.IGNORECASE), \
        "a stored trailer preference is not applied without a question"
    assert not re.search(r"Co-Authored-By`?[^.]{0,40}\bon by default\b", flat), \
        "Co-Authored-By is still a per-commit default instead of the stored answer"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_reads_both_stored_preferences(text):
    flat = _flat(text())
    assert "commit_co_authored_by" in flat and "commit_session_link" in flat


@pytest.mark.parametrize("text", COMMIT)
def test_commit_asks_once_when_not_answered_and_stores_the_answer(text):
    """null = not answered yet (never "no"): asked ONCE, stored with the
    project through the real CLI, never asked again."""
    from dct import cli
    flat = _flat(text())
    assert re.search(r"\b(null|not answered)\b[^.]{0,200}\bonce\b", flat, re.IGNORECASE), \
        "a missing answer is not asked once"
    commands = re.findall(r"`(dct project trailers [^`]*)`", flat)
    assert commands, "the answer is not stored with `dct project trailers`"
    for command in commands:
        argv = [t for t in command.split()[1:] if not t.startswith("<")]
        argv = [a.split("|")[0] for a in argv]
        args = cli.build_parser().parse_args(argv[:2] + ["proj-a"] + argv[2:])
        assert args.project_command == "trailers", command


@pytest.mark.parametrize("text", COMMIT)
def test_the_projects_own_rules_forbidding_a_trailer_win(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\bproject'?s? own rules\b|\bits own rules\b", s, re.IGNORECASE)
            and re.search(r"forbid", s, re.IGNORECASE)
            and re.search(r"\b(never|not)\b", s, re.IGNORECASE)]
    assert hits, "a stored yes could override a project's ban on a trailer"
    assert re.search(r"dct'?s own repository[^.]{0,120}session link|"
                     r"session link[^.]{0,120}dct'?s own repository", _flat(text()),
                     re.IGNORECASE), "dct's own ban on a session link is not stated"


@pytest.mark.parametrize("text", COMMIT)
def test_commit_session_link_only_on_a_stored_yes(text):
    body = text()
    hits = [s for s in _sentences(body)
            if re.search(r"session link", s, re.IGNORECASE)
            and re.search(r"\bonly\b", s) and re.search(r"\b(true|yes)\b", s, re.IGNORECASE)]
    assert hits, "a session link is not limited to a stored yes"
    for block in re.findall(r"```.*?```", body, re.DOTALL):
        assert "Claude-Session" not in block and "claude.ai/" not in block, block


# ── K-57: /handoff "sprint only" — add / cut stay additive ──────────────────

def _sprint_only(body: str) -> str:
    """The branch for a saved draft that changes only the sprint: from its
    bold header (`**"sprint only, no prompt"**`) to the next step."""
    m = re.search(r"\*\*\"?sprint only, no prompt\"?\*\*", body, re.IGNORECASE)
    assert m, "no 'sprint only, no prompt' branch"
    start = m.start()
    end_markers = [i for i in (body.find("Remember the returned", start),
                               body.find("\n6. ", start), body.find("\n## ", start)) if i != -1]
    return body[start:min(end_markers)]


HANDOFF_SPRINT_ONLY = [
    pytest.param(lambda: _sprint_only(_skill("handoff")), id="skill"),
    pytest.param(lambda: _sprint_only(_fallback("handoff")), id="fallback"),
]


@pytest.mark.parametrize("text", HANDOFF_SPRINT_ONLY)
def test_handoff_sprint_only_maps_add_and_cut_to_the_additive_tools(text):
    body = text()
    flat = _flat(body)
    for tool in ("add_item_to_current_sprint", "remove_item_from_current_sprint",
                 "assign_item_to_sprint", "remove_item_from_sprint"):
        assert tool in flat, f"{tool} is not used for an add / cut"
    for s in _sentences(body):
        if "set_sprint" in s and not _negated_at(s, s.index("set_sprint")):
            assert re.search(r"\breplace", s, re.IGNORECASE), \
                f"set_sprint is used for more than an accepted replace: {s}"
    assert re.search(r"\b(never|not)\b[^.]{0,80}\b(full list|whole list|union)\b", flat,
                     re.IGNORECASE), "nothing forbids computing a full list for set_sprint"


@pytest.mark.parametrize("text", HANDOFF_SPRINT_ONLY)
def test_handoff_sprint_only_stops_on_a_missing_or_ambiguous_target(text):
    flat = _flat(text())
    assert re.search(r"\bno active sprint\b", flat, re.IGNORECASE), "0 active sprints is unhandled"
    assert re.search(r"\b(one|1|single) active sprint\b|≤ 1 active", flat, re.IGNORECASE)
    assert re.search(r"(several|≥ 2|two or more) active sprints", flat, re.IGNORECASE)
    assert re.search(r"\bstop\w*\b[^.]{0,120}\b(target|sprint)\b", flat, re.IGNORECASE), \
        "a missing or ambiguous target does not stop readably"


# ── K-58: every field of the approved draft is saved ────────────────────────

DRAFT_FIELD_ARGS = {
    "BEGIN DRAFT": ("prompt",),
    "BEGIN CONTEXT SUMMARY": ("context_summary",),
    "Scope:": ("scope",),
    "Sprint:": ("item_ids", "add_item_ids", "remove_item_ids"),
    "Target sprint": ("sprint",),
}


def test_handoff_saves_every_field_of_the_printed_draft():
    body = _skill("handoff")
    draft = next(b for b in re.findall(r"```\n?(.*?)```", body, re.DOTALL) if "BEGIN DRAFT" in b)
    call = re.search(r"mcp__dct__handoff_work\((.*?)\n\)", body, re.DOTALL)
    assert call, "no handoff_work call"
    args = set(re.findall(r"^\s*(\w+)=", call.group(1), re.MULTILINE))
    for field, names in DRAFT_FIELD_ARGS.items():
        if field in draft or field in body:
            assert set(names) <= args, f"the draft's {field!r} is not saved ({names})"
    assert re.search(r"context_summary[^.]{0,160}\bverbatim\b", _flat(body))
    assert re.search(r"\bnever (shorten\w*|regenerat\w*)\b[^.]{0,40}(or )?(regenerat\w*)?",
                     _flat(body), re.IGNORECASE), "the approved summary may be rewritten"


def test_handoff_fallback_saves_the_context_summary_through_mcp_and_cli():
    from dct import cli
    flat = _flat(_fallback("handoff"))
    # (the call's own text carries parenthesised notes such as "(default)")
    assert re.search(r"handoff_work\(prompt=.{0,600}?context_summary=", flat), \
        "the fallback's handoff_work call drops context_summary"
    commands = re.findall(r"`dct (handoff create [^`]*)`", flat)
    assert commands and any("--context-summary" in c for c in commands), \
        "the CLI line does not pass the context summary"
    for command in commands:
        argv = [a for a in command.split() if not a.startswith("<")]
        known = {o for a in cli.build_parser()._subparsers._group_actions[0]
                 .choices["handoff"]._subparsers._group_actions[0].choices["create"]._actions
                 for o in a.option_strings}
        for flag in (a for a in argv if a.startswith("--")):
            assert flag in known, f"unknown flag {flag} in `dct {command}`"


# ── K-59 / K-60 / K-61: /pickup — what consumes, which handoffs, first step ──

def _quoted(sentence: str) -> set[str]:
    return {q.lower() for q in re.findall(r"\"([^\"]+)\"|“([^”]+)”", sentence) for q in q if q}


def _consume_triggers(body: str) -> set[str]:
    """The quoted phrases of the sentence that names what consumes the single
    matching handoff without a question (the AUTO consume)."""
    hits = [s for s in _sentences(body)
            if re.search(r"\bunambiguous\b", s, re.IGNORECASE)
            and re.search(r"\"continue\"", s)
            and re.search(r"consum|pickup_work|AUTO", s, re.IGNORECASE)]
    assert hits, "no sentence names the phrases that consume"
    return _quoted(hits[0])


def test_pickup_skill_and_fallback_consume_on_the_same_phrases():
    skill, fallback = _consume_triggers(_skill("pickup")), _consume_triggers(_fallback("pickup"))
    assert skill == fallback, (skill, fallback)
    assert {"continue", "pickup", "resume"} <= skill, skill
    assert not {"where were we", "what's next", "what’s next"} & skill, \
        f"a status question consumes: {skill}"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_a_status_question_is_a_pure_read(text):
    hits = [s for s in _sentences(text())
            if re.search(r"where were we", s, re.IGNORECASE)
            and re.search(r'consume="false"', s)]
    assert hits, "\"where were we\" is not a pure read"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_names_resume_as_a_proposed_refinement(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\"resume\"", s)
            and re.search(r"\bpropos\w*\b[^.]{0,40}\brefinement\b|\brefinement\b[^.]{0,40}\bpropos",
                          s, re.IGNORECASE)]
    assert hits, "\"resume\" consumes without saying it is a proposed refinement"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_a_quoted_word_is_not_a_command(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\bquot\w*\b", s, re.IGNORECASE)
            and re.search(r"\bnot (a command|an instruction)\b", s, re.IGNORECASE)]
    assert hits, "a single quoted word could still trigger a consume"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_with_a_scope_counts_and_offers_only_matching_handoffs(text):
    flat = _flat(text())
    assert re.search(r"`?scope_matched`?", flat), "the scope branch does not read scope_matched"
    assert re.search(r"\bonly (the )?(matching|in-scope)\b|\bonly handoffs? of (that|the) scope\b",
                     flat, re.IGNORECASE), "foreign handoffs may still be offered"
    assert re.search(r"pending_count[^.]{0,80}\b(without|no) (a )?scope\b|"
                     r"\b(without|no) (a )?scope\b[^.]{0,80}pending_count", flat, re.IGNORECASE), \
        "without a scope the global count is not named"
    assert re.search(r"truncat\w*[^.]{0,160}list_handoffs|list_handoffs[^.]{0,160}truncat", flat,
                     re.IGNORECASE), "a truncated `others` list is taken as complete"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_several_reads_the_chosen_handoff_in_full_before_any_consume(text):
    """Choosing from a 200-char preview is no consent to run an unread plan:
    the chosen id is fetched in full WITHOUT consuming and printed; the
    consume waits for an unambiguous instruction to continue THAT handoff."""
    body = text()
    flat = _flat(body)
    reads = [s for s in _sentences(body)
             if "get_handoff" in s
             and re.search(r"\bwithout consum\w*|\bno consume\b|\bnot (yet )?consum", s,
                           re.IGNORECASE)]
    assert reads, "the chosen handoff is not read in full without consuming"
    assert flat.index("get_handoff") < flat.index('consume="true", handoff_id=N'), \
        "the consume by id comes before the full read"
    consumes = [s for s in _sentences(body)
                if 'consume="true", handoff_id=N' in s
                and re.search(r"\bcontinue\b", s, re.IGNORECASE)]
    assert consumes, "the consume by id is not tied to an instruction to continue it"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_showing_an_older_handoff_stays_a_read(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\bshow\w*\b", s, re.IGNORECASE)
            and re.search(r"\bolder\b|\bin full\b", s, re.IGNORECASE)
            and re.search(r"\b(read|nothing (is )?consumed|not consum\w*)\b", s, re.IGNORECASE)]
    assert hits, "showing an older handoff may consume it"


def test_pickup_never_asks_the_first_action_together_with_the_handoff_choice():
    flat = _flat(_skill("pickup"))
    assert not re.search(r"Question 2 — first action\*\* \(always present\)", flat)
    assert re.search(r"first action[^.]{0,120}\b(after|only once)\b[^.]{0,80}"
                     r"(read|consumed|chosen)", flat, re.IGNORECASE), \
        "the first action is still asked before the chosen handoff was read"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_continue_with_a_current_next_step_starts_without_a_question(text):
    paragraphs = [p for p in _paragraphs(text())
                  if re.search(r"Starting with", p)
                  and re.search(NO_QUESTION, p, re.IGNORECASE)]
    assert paragraphs, "a single consumed handoff with a next step still asks the first action"
    unit = " ".join(paragraphs)
    assert re.search(r"get_item|checkpoint", unit), "the step's current state is not checked"
    assert re.search(r"\bgates?\b", unit, re.IGNORECASE), "pending gates are not checked first"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_an_old_handoff_is_no_consent_for_risky_steps(text):
    hits = [p for p in _paragraphs(text())
            if re.search(r"\bcommit", p, re.IGNORECASE) and re.search(r"\bpush", p, re.IGNORECASE)
            and re.search(r"\bpublish", p, re.IGNORECASE)
            and re.search(r"\bdestructive\b", p, re.IGNORECASE)
            and re.search(r"\b(no|not|never)\b[^.]{0,40}\bconsent\b", p, re.IGNORECASE)]
    assert hits, "a handoff's old text may stand in for a new consent"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_a_stale_or_unclear_step_still_asks(text):
    hits = [s for s in _sentences(text())
            if re.search(r"\b(stale|outdated|contradict\w*|unclear)\b", s, re.IGNORECASE)
            and re.search(r"\bask", s, re.IGNORECASE)]
    assert hits, "a stale or unclear next step is started without a question"


# ── B3 (K-60): the auto-start path prints the handoff and starts in ONE turn ──

# A demand that the printed handoff stand alone: a turn of its own, or no
# tool call while it is printed.
OWN_TURN = re.compile(r"\b(its own turn|own turn|separate turn|no tool calls?)\b", re.IGNORECASE)
# What justifies that turn: a question / dialog the user answers after reading.
A_QUESTION_FOLLOWS = re.compile(r"\b(question|dialog|AskUserQuestion|asks?|decide)\b",
                                re.IGNORECASE)


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_auto_start_prints_the_handoff_and_starts_in_the_same_turn(text):
    """K-60: an unambiguous "continue", ONE matching handoff and a concrete,
    current next step → the handoff is printed and the work starts in the
    SAME turn, after the item / checkpoint and gates check — no turn that
    only waits for a reply nobody has to give."""
    hits = [s for s in _sentences(text())
            if re.search(r"\bsame turn\b", s, re.IGNORECASE)
            and re.search(r"\bprint", s, re.IGNORECASE)
            and re.search(r"\b(start|continu)\w*", s, re.IGNORECASE)
            and not _negated_at(s, re.search(r"\bsame turn\b", s, re.IGNORECASE).start())]
    assert hits, "the auto-start path still stops after printing the handoff"


@pytest.mark.parametrize("text", PICKUP)
def test_pickup_a_turn_of_its_own_only_where_the_user_must_decide(text):
    """The separate print turn exists for a dialog that follows (several
    handoffs, a stale / contradictory / unclear step): every sentence that
    demands it is tied to that question — an unconditional "its own turn,
    no tool call" blocks the auto-start of K-60."""
    loose = [s for s in _sentences(text())
             if (m := OWN_TURN.search(s))
             # "no tool call" carries its own "no": only "own turn" can be negated
             and (m.group(0).lower().startswith("no ") or not _negated_at(s, m.start()))
             and not A_QUESTION_FOLLOWS.search(s)]
    assert not loose, f"a turn of its own is demanded where no question follows: {loose}"
