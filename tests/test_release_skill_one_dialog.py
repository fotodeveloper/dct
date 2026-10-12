"""The ONE-dialog `/release` (phase 2, contract section H): the prose of
dct/skills/release/SKILL.md AND of the `/release` section of
docs/assistant-fallbacks.md must describe the single screen the owner
approves, and the real CLI must keep what that prose promises.

What the owner decided, and what these tests pin (numbers = the job's
requirements):

1. Before the question, in this order and silently ("Checking proj-a…"):
   `dct release plan --json` (routing), `dct release status --version` (a
   report sentence only), on a dirty tree `dct clog candidates --json` and
   the diff (the message drafted into a scratch file), then
   `dct release prepare --preview` with `--message-file` /
   `--include-untracked`, then the publishing-workflow detection.
2. A typical release asks ONE AskUserQuestion with two questions: the
   action (Release / Release with all N / Change something / Stop —
   "Release with all N" never first, never for reverted entries or more
   than 4 waiting) and the version, the suggestion pre-selected ("Keep …
   (recommended)", built from the version the applied variant releases),
   omitted for a first version (the screen shows the proposed version;
   another first version goes through "Change something").
3. The screen is rendered only from the preview's variant
   (`variants[…].consent`) and its `display`.
4. Each answer maps to the CLI calls of the H3 table (apply --consent,
   preview again on another version, Change → at most 4 questions then
   preview again, Stop → --discard-preview, the listed refusals → preview
   again with "Something changed since you said Release", a rejecting hook
   → stop, never --no-verify, exit 75 → one automatic re-run).
5. A commit already made under the owner's consent
   (`pending_consent.consent_digest`, `adoptable`) → the Finish question,
   never the Legacy path.
6. No internal vocabulary in anything shown to the user; identifiers only
   under a final "Technical details" line.
7. The question-count table of H4, counted in DECISIONS (answers), with the
   invariants between the paths.
8. The old Steps 2 (Legacy question), 3 (work-in-progress question), 5
   (per-entry questions), 7 (bind question), 8 (prepare question) and 9
   (apply question) are gone.

Option labels and messages the user sees are written in double quotes in
both documents (`"Stop"`); that convention is what requirement 6 scans.
The last tests run the DOCUMENTED commands through the real CLI: what the
prose tells the agent to run must do what the prose says it does."""

from __future__ import annotations

import ast
import json
import re
import shutil
from pathlib import Path

import pytest
from conftest import git, run_cli
from test_release_dialog_routing import BARE_FLAGS, documented
from test_release_effects import (
    SLUG,
    make_release_repo,
    proven,
    remote_refs,
    use_cli,
)
from test_skill_prose_changelog import (
    _commands,
    _dct_executes,
    _effects_detail_keys,
    _in_order,
    _release_fallback,
    _skill,
)

from dct.core.changelog import add_entry, list_entries
from dct.core.releases import get_release
from dct.workflows.release import plan as planning

ROOT = Path(__file__).resolve().parent.parent

RELEASE_DIALOGS = [
    pytest.param(lambda: _skill("release"), id="skill"),
    pytest.param(_release_fallback, id="fallback"),
]

# ── reading the prose ───────────────────────────────────────────────────────


def _flat(text: str) -> str:
    return " ".join(text.split())


def _plain(text: str) -> str:
    """Flat text without backticks — `"Stop"` and "Stop" read the same."""
    return _flat(text).replace("`", "")


def _new_release(body: str) -> str:
    """The dialog of a NEW release: everything before the Resume path (the
    resume / takeover / legacy paths keep their own consents)."""
    for marker in ("\n## Resume path", "\n**Resume path** ("):
        if marker in body:
            return body[:body.index(marker)]
    raise AssertionError("the dialog has no Resume path section")


def _release_calls(text: str, sub: str) -> list[list[str]]:
    return [flags for group, name, flags in _commands(text)
            if (group, name) == ("release", sub)]


def _ordered(text: str, *markers: str) -> None:
    """Every marker present, their FIRST occurrences in this order."""
    missing = [m for m in markers if m not in text]
    assert not missing, f"missing: {missing}"
    _in_order(text, *markers)


def _from_step0(text: str) -> str:
    """From the first `dct release plan --json` on (an overview before the
    probe may name later commands)."""
    assert "dct release plan --json" in text, "no `dct release plan --json`"
    return text[text.index("dct release plan --json"):]


def _windows(text: str, needle: str, radius: int) -> list[str]:
    return [text[max(0, m.start() - radius):m.end() + radius]
            for m in re.finditer(re.escape(needle), text)]


def _q1_block(body: str) -> str:
    """The action question of the screen: from the first "Q1" after the
    preview call to the "Q2" that follows it."""
    part = _plain(_new_release(body))
    assert "dct release prepare --preview" in part, "no `dct release prepare --preview`"
    start = part.find("Q1", part.index("dct release prepare --preview"))
    assert start != -1, "the screen's action question is not labelled Q1"
    end = part.find("Q2", start)
    assert end != -1, "the screen's version question is not labelled Q2"
    return part[start:end]


def _q2_block(body: str) -> str:
    part = _plain(_new_release(body))
    start = part.find("Q2", part.find("Q1", part.index("dct release prepare --preview")))
    assert start != -1, "the screen's version question is not labelled Q2"
    return part[start:start + 1200]


def _labels(text: str) -> list[str]:
    """Double-quoted strings, in order."""
    return re.findall(r'"([^"]{1,200})"', text)


def _q1_options(body: str) -> list[str]:
    """The option LABELS of the action question, in order: the first quoted
    string after each option number ("1." in the skill, "(1)" in the
    fallback) that follows the "in this order" marker — never the
    question's own text, never a quoted description."""
    return _options_of(_q1_block(body))


def _options_of(q1: str) -> list[str]:
    at = re.search(r"[Oo]ptions,? in this order", q1)
    assert at, "Q1 does not list its options in order"
    found = re.findall(r'(?:^|\s)\(?([1-9])[.)]\s+"([^"]+)"', q1[at.end():])
    assert [int(n) for n, _ in found] == list(range(1, len(found) + 1)), \
        f"Q1's options are not numbered 1..n: {found}"
    return [label for _, label in found]


def _assert_q1_order(options: list[str]) -> None:
    """H-4: "Release" (or its local / version-only form) first, then
    "Release with all N", "Change something", "Stop" — four options."""
    assert len(options) == 4, options
    assert options[0].startswith("Release") and not options[0].startswith("Release with all"), \
        f"the first option of Q1 is not Release: {options}"
    assert options[1].startswith("Release with all"), options
    assert options[2:] == ["Change something", "Stop"], options


_NEGATION = re.compile(r"(?<![\w-])(not|never|no|don't|doesn't|isn't)(?![\w-])", re.IGNORECASE)


def _affirmed(text: str, pattern: str) -> bool:
    """`pattern` occurs in `text` at least once with no negation in the
    match or in the few words before it, within its clause ("Q2 is
    omitted" — not "Q2 is never omitted", "it is not omitted")."""
    for m in re.finditer(pattern, text, re.IGNORECASE):
        before = re.split(r"[.;:—]", text[max(0, m.start() - 40):m.start()])[-1]
        if not _NEGATION.search(before + m.group(0)):
            return True
    return False


def test_affirmed_reads_negation_and_word_boundaries():
    assert _affirmed("Q2 is omitted for first_version.", r"\bomitted\b[^.]{0,80}first_version")
    assert not _affirmed("Q2 is never omitted for first_version.",
                         r"\bomitted\b[^.]{0,80}first_version")
    assert not _affirmed("for first_version it is not omitted.",
                         r"first_version[^.]{0,80}\bomitted\b")
    assert not _affirmed("Q2 is unomitted for first_version.", r"\bomitted\b[^.]{0,80}first_version")
    assert _affirmed("drop every earlier --no-entries", r"\bdrop\b[^.]{0,40}--no-entries")
    assert _affirmed("No question. Q2 is omitted for first_version.",
                     r"\bomitted\b[^.]{0,80}first_version")


def _dialog_b(body: str) -> str:
    plain = _plain(_new_release(body))
    start = plain.index("Change something — Dialog B")
    return plain[start:plain.index("The changelog file", start)]


def _push_question(body: str) -> str:
    part = _dialog_b(body)
    start = part.index('"Where is it pushed?"')
    return part[start:part.index('"Anything else?', start)]


# ── 1. the CLI calls before the question, in the contract's order ───────────


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_1_the_cli_calls_before_the_screen_run_in_the_contract_order(text):
    """H1: plan → status (report only) → candidates and the diff (dirty tree)
    → prepare --preview → the publishing-workflow detection → the screen's
    question (identified by its "Change something" option)."""
    part = _from_step0(_new_release(text()))
    _ordered(part, "dct release plan --json", "dct release status --version",
             "dct clog candidates --json", "dct release prepare --preview",
             ".github/workflows", '"Change something"')


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_1_the_preview_call_carries_the_message_and_the_included_untracked_files(text):
    """H1.5: ONE documented preview invocation names `--json`,
    `--message-file` and `--include-untracked` — the flags the production
    parser takes (checked against `dct.cli.build_parser` by
    test_every_named_workflow_command_and_flag_exists_in_the_cli)."""
    previews = [set(f) for f in _release_calls(_new_release(text()), "prepare")
                if "--preview" in f]
    assert previews, "no `dct release prepare --preview` invocation"
    assert any({"--json", "--message-file", "--include-untracked"} <= f for f in previews), \
        f"no preview invocation with --json --message-file --include-untracked: {previews}"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_1_the_message_file_is_for_a_dirty_tree_and_drafted_by_the_assistant(text):
    """H1.3: only a dirty tree gets a message (the CLI refuses one on a
    clean tree, `nothing_to_commit`): conventional format, `(v<current>)`,
    a `Changelog: <ids>` line from the candidates matching the diff, written
    to a scratch file; the nested /commit questions are not asked."""
    plain = _plain(_new_release(text()))
    near = " ".join(_windows(plain, "--message-file", 400))
    assert re.search(r"dirty|uncommitted", near, re.IGNORECASE), \
        "the --message-file passage does not say it is for a dirty tree only"
    assert "git diff" in plain, "the diff the message is drafted from is not read"
    assert re.search(r"\(v<current[^>]*>\)", plain), "no `(v<current>)` suffix in the draft"
    assert re.search(r"Changelog: <[^>]+>", plain), "no `Changelog: <ids>` line in the draft"
    assert re.search(r"conventional", plain, re.IGNORECASE)
    assert re.search(r"scratch file", plain, re.IGNORECASE), "the message is not written to a file"
    assert re.search(r"/commit[^.]{0,160}(question|ask)[^.]{0,80}"
                     r"(not asked|never asked|suppress|skipp)|"
                     r"(suppress|skip|never ask|not ask)[^.]{0,80}/commit", plain, re.IGNORECASE), \
        "the nested /commit questions are not suppressed"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_1_the_status_call_feeds_only_a_report_sentence(text):
    """H1.2 / K-2: `dct release status --version <current>` is read for one
    sentence in the report — no question, no Legacy path offered."""
    sentences = re.split(r"(?<=[.;])\s+", _plain(_new_release(text())))
    at = [i for i, s in enumerate(sentences) if "dct release status --version" in s]
    assert at, "no `dct release status --version`"
    around = " ".join(sentences[at[0]:at[0] + 2])
    assert re.search(r"\breport\b", around), f"status is not used for the report: {around}"
    assert not re.search(r"\boffer\b|Legacy path|AskUserQuestion", around), around


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_1_the_owner_sees_only_a_checking_line_before_the_screen(text):
    plain = _plain(_new_release(text()))
    assert re.search(r'"Checking [^"]{1,40}(…|\.\.\.)"', plain), \
        'no "Checking <project>…" line before the screen'


# ── 2. ONE AskUserQuestion: the action and the version ──────────────────────


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_screen_is_one_ask_user_question_with_two_questions(text):
    plain = _plain(_new_release(text()))
    assert re.search(r"\b(one|single)\b[^.]{0,60}AskUserQuestion[^.]{0,120}"
                     r"\btwo questions\b", plain, re.IGNORECASE), \
        "the screen is not described as one AskUserQuestion with two questions"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_action_question_offers_release_first_and_all_n_never_first(text):
    """H2 Q1: "Release" (or its local / version-only form) first, then
    "Release with all N", "Change something", "Stop"."""
    q1 = _q1_block(text())
    _assert_q1_order(_q1_options(text()))
    assert re.search(r"never (the )?first|never option 1", q1), \
        'Q1 does not say "Release with all N" is never first'


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_option_order_check_reads_the_options_not_the_question(text):
    """S17: the order check must fail when the OPTIONS are swapped — the
    question's own text ("Release proj-a and push it …?") is no option."""
    q1 = _q1_block(text())
    at = re.search(r"[Oo]ptions,? in this order", q1).end()
    first, second = _options_of(q1)[:2]
    tail = q1[at:].replace(f'"{first}"', "\0", 1).replace(f'"{second}"', f'"{first}"', 1) \
        .replace("\0", f'"{second}"', 1)
    swapped = q1[:at] + tail
    assert _options_of(swapped)[0] == second
    with pytest.raises(AssertionError):
        _assert_q1_order(_options_of(swapped))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_release_label_follows_the_bound_push_and_version_only(text):
    """`push.mode == "local"` → "Release locally (no push)"; a version-only
    variant → "Release V (version only)" — the label IS the consent."""
    q1 = _q1_block(text())
    assert '"Release locally (no push)"' in q1
    assert re.search(r"push\.mode\s*(=|==)\s*\"?local", q1), "the local label is not tied to push.mode"
    assert re.search(r'"Release [^"]*\(version only\)"', q1)
    assert "version_only" in q1


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_release_with_all_n_is_not_offered_for_reverted_or_more_than_four(text):
    """H-4 (a): the variant exists only when 1-4 entries wait and none is
    reverted — the dialog reads the CLI's `all_unavailable`, it never
    builds the option itself."""
    plain = _plain(_new_release(text()))
    assert "all_unavailable" in plain, "the dialog does not read all_unavailable"
    windows = _windows(plain, "Release with all", 500)
    assert any(re.search(r"revert", w) and re.search(
        r"more than 4|more than four|> ?4|at most 4|4 or fewer|≤ ?4|up to 4", w)
        for w in windows), "no passage limits Release with all N (reverted, more than 4)"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_version_question_keeps_the_suggestion_pre_selected(text):
    """H2 Q2: "Keep V (recommended)", then the alternatives, then Other.
    K-63 (contract B / H2 / H4 / I.34): for `first_version` Q2 is OMITTED —
    the screen shows the proposed version; this test pinned the opposite
    before the owner's decision of 2026-10-12."""
    q2 = _q2_block(text())
    assert re.search(r'"Keep [^"]+\(recommended\)"', q2), 'no "Keep <V> (recommended)" option'
    assert "suggested" in q2 and "alternatives" in q2 and "Other" in q2
    assert _affirmed(q2, r"\b(omitted|left out|not asked)\b[^.]{0,80}first_version|"
                         r"first_version[^.]{0,80}\b(omitted|left out|not asked)\b"), \
        "Q2 is not omitted for first_version"
    assert not _affirmed(q2, r"\basked for `?first_version`? too\b"), \
        "Q2 is still asked for first_version"
    plain = _plain(_new_release(text()))
    assert re.search(r'Keep[^.]{0,160}(Release with all|V_all|"all")|'
                     r'(Release with all|V_all|"all")[^.]{0,160}Keep', plain), \
        "what Keep means after Release with all N is not said"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_every_version_label_comes_from_the_applied_variants_consent(text):
    """Review finding: Keep built from `suggested.target` names one version
    and releases another after `--version` (and V_all after "Release with
    all N"). Each label's version is the `consent.version.target` of the
    variant that label applies; Keep names V_all too when it differs; Q1's
    question names no version; a re-screen after a typed version keeps it."""
    plain = _plain(_new_release(text()))
    assert re.search(r"consent\.version\.target[^.]{0,200}never `?suggested\.target|"
                     r"never `?suggested\.target[^.]{0,200}consent\.version\.target", plain), \
        "the labels are not built from the applied variant's consent.version.target"
    assert "variants[all].consent.version.target" in plain
    assert re.search(r'"Keep the suggested version: [0-9.]+, or [0-9.]+ with all \d+ '
                     r'\(recommended\)"', plain), "Keep does not name V_all when it differs"
    assert re.search(r'"Release with all \d+ as [0-9.]+"', plain)
    assert re.search(r'"Keep [0-9.]+ \(your choice\)"', plain), \
        "a re-screen after another version does not keep the typed version"
    q1 = _q1_block(text())
    question = re.search(r'[Qq]uestion[^"]{0,60}"([^"]+)"', q1)
    assert question and not re.search(r"\d+\.\d+", question.group(1)), \
        f"Q1's question names a version: {question and question.group(1)}"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_version_only_option_speaks_plainly(text):
    """No `since=last_release` anchor in what the owner reads."""
    q1 = _q1_block(text())
    assert "since=last_release" not in q1
    assert re.search(r"Only the version number changes; the changelog gets no new section, "
                     r"and this version won't appear in the exported changelog", q1)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_the_typical_release_answer_goes_straight_to_apply(text):
    """Release + Keep IS the apply yes: the next call is apply --consent
    with the shown variant's digest — no question in between."""
    plain = _plain(_new_release(text()))
    match = re.search(r"Release\"?\W{0,4}(and|with|\+)?\W{0,4}Keep[^.]{0,300}?"
                      r"dct release apply --consent[^.]{0,80}variants\[\s*\"?shown", plain)
    assert match, "Release + Keep does not lead to apply --consent <variants[shown]…>"
    assert not re.search(r"\bask\b|AskUserQuestion", match.group(0)), match.group(0)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_a_first_version_gets_no_invented_alternative(text):
    """K-63: `suggest_version` returns no alternatives for `first_version`;
    the dialog never offers one of its own (no "1.0.0 — a first stable
    release" option) and the old "never inferred" sentence is gone."""
    plain = _plain(_new_release(text()))
    assert not re.search(r'"1\.0\.0\b[^"]*"', plain), "an invented 1.0.0 option is offered"
    assert not re.search(r"\bnever inferred\b", plain), 'the "never inferred" sentence is back'


def _change_something_description(body: str) -> str:
    """Q1 option 3's text: from its label to option 4."""
    q1 = _q1_block(body)
    start = q1.index('"Change something"')
    return q1[start:re.search(r'(?:^|\s)\(?4[.)]\s+"', q1[start:]).start() + start]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_2_another_first_version_is_chosen_under_change_something(text):
    """K-63 + K-66 (5): Q2 changes the version; a first version has no Q2,
    so "Change something" offers another version FOR A FIRST VERSION ONLY —
    and Dialog B really has that route (→ `--version`). Its description
    never promises a version change Dialog B does not offer."""
    desc = _change_something_description(text())
    quoted = _labels(desc)
    assert quoted[0] == "Change something" and not re.search(r"\bversion\b", quoted[1]), \
        f"the general description promises a version change: {quoted[1]!r}"
    assert re.search(r"first_version[^.]{0,120}\bversion\b|\bversion\b[^.]{0,120}first_version",
                     desc), "the first-version description names no version change"
    dialog_b = _dialog_b(text())
    assert re.search(r"first_version[^.]{0,300}--version <V>|--version <V>[^.]{0,300}first_version",
                     dialog_b), "Dialog B has no route to another first version (--version)"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_a_first_version_has_no_q2_and_release_applies_the_shown_screen(text):
    """Without Q2 the answer is Q1 alone: "Release" applies the shown
    variant's digest (the screen showed the proposed version)."""
    plain = _plain(_new_release(text()))
    table_end = plain.index('"Stop" + any version')
    after = plain[table_end:table_end + 900]
    assert re.search(r"first_version[^.]{0,200}\bKeep\b", after), \
        "the after-answer table does not say how a first version (no Q2) is read"


def _version_sentence(body: str) -> str:
    found = [s for s in re.split(r"(?<=[.])\s+", _plain(_new_release(body)))
             if re.search(r"\bpropos", s) and re.search(r"\bapprov", s)
             and re.search(r"\bversion\b", s) and "first_version" in s]
    assert len(found) == 1, f"not exactly one version sentence: {found}"
    return found[0]


def test_2_the_version_sentence_is_one_shared_wording():
    """K-66 (2) / contract: dct PROPOSES the version, the owner APPROVES it
    with Release — Q2 changes it, a first version is changed under "Change
    something". The same sentence in the skill and the fallback."""
    skill, fallback = _version_sentence(_skill("release")), _version_sentence(_release_fallback())
    assert skill == fallback, (skill, fallback)
    assert "Q2" in skill and "Change something" in skill


# ── 3. the screen is rendered only from the preview ─────────────────────────


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_3_the_screen_is_rendered_only_from_the_variant_and_its_display(text):
    plain = _plain(_new_release(text()))
    sentences = re.split(r"(?<=[.;:])\s+", plain)
    assert any(re.search(r"\bonly\b", s) and re.search(r"render|screen", s)
               and re.search(r"variants\[[^\]]+\]\.consent", s) and "display" in s
               for s in sentences), \
        "no sentence says the screen is rendered only from variants[…].consent and display"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_3_the_screen_names_each_block_of_h2(text):
    """The work block (whole file incl. the unstaged part, the message, what
    it marks as done, what is not sure), what goes in and waits, files and
    checks, the contract line only on a change, "Then:" or "not pushed:",
    the range line and the all-N delta line."""
    plain = _plain(_new_release(text()))
    for needle in ("all of it", "didn't stage", "marks as done", "might be part of this work",
                   "display.entries", "display.checks", "display.range", "display.delta",
                   "Then:", "not pushed:"):
        assert needle in plain, needle
    assert re.search(r"contract[^.]{0,160}only when[^.]{0,60}change|"
                     r"only when[^.]{0,60}change[^.]{0,60}(true)?[^.]{0,80}contract", plain), \
        "the contract line is not limited to a contract change"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_3_apply_passes_only_the_digest_of_the_rendered_variant(text):
    """The rule of H3, reworded: never a digest the screen did not show."""
    plain = _plain(text())
    assert re.search(r"only with the consent_digest of the variant whose content the screen "
                     r"rendered", plain), "the never-an-unseen-digest rule is not stated"


# ── 4. after each answer ─────────────────────────────────────────────────────

APPLY_CONSENT_FLAGS = {"--consent", "--confirm", "--json"}


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_every_apply_the_dialog_runs_is_the_consent_form(text):
    """Release and Release with all N both run `apply --consent C --confirm
    --json`; no binding flag (they would be refused, consent_flag_conflict)."""
    applies = [set(f) for f in _release_calls(text(), "apply") if len(f) > 1]
    assert applies, "no complete `dct release apply` invocation"
    for flags in applies:
        assert flags == APPLY_CONSENT_FLAGS, sorted(flags)
    plain = _plain(text())
    assert re.search(r"variants\[\s*\"?shown\"?\s*\]\.consent_digest", plain)
    assert re.search(r"variants\[\s*\"?all\"?\s*\]\.consent_digest", plain)
    assert re.search(r"Release with all[^.]{0,120}Keep[^.]{0,300}?dct release apply --consent"
                     r"[^.]{0,80}variants\[\s*\"?all", plain), \
        "Release with all N + Keep does not apply the all variant's digest"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_another_version_previews_again_and_shows_the_screen_again(text):
    """H-1: Q2 = another version → `prepare --preview --version V2` with the
    same message / untracked / attest / push flags, then the screen again
    headed "Version changed to V2" — never an apply of the old screen."""
    body = text()
    assert any({"--preview", "--version"} <= set(f) for f in _release_calls(body, "prepare")), \
        "no `prepare --preview --version` invocation"
    plain = _plain(_new_release(body))
    assert "Version changed to" in plain
    assert re.search(r"same (message|--message-file)", plain), \
        "the new preview does not keep the same message and flags"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_change_something_asks_at_most_four_questions_then_previews_again(text):
    body = text()
    plain = _plain(_new_release(body))
    assert '"Change something"' in plain, 'no "Change something" option'
    assert re.search(r"(at most|up to|no more than|≤) ?(4|four) questions", plain), \
        "dialog B is not limited to 4 questions"
    assert "update_changelog" in plain and "delete_changelog" in plain
    previews = [set(f) for f in _release_calls(body, "prepare") if "--preview" in f]
    named = set().union(*previews) if previews else set()
    for flag in ("--entries", "--no-entries", "--attest", "--no-push"):
        assert flag in named, f"no preview invocation with {flag}"
    assert any({"--push-remote", "--push-branch", "--push-url"} <= f for f in previews), \
        "no preview invocation naming the complete push target"
    assert re.search(r"screen again", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_stop_discards_the_preview_and_does_nothing_else(text):
    """K-d (a): a declined screen can never be applied by any agent."""
    body = text()
    discards = [set(f) for f in _release_calls(body, "prepare") if "--discard-preview" in f]
    assert discards and all(f == {"--discard-preview", "--json"} for f in discards), discards
    plain = _plain(_new_release(body))
    windows = _windows(plain, "dct release prepare --discard-preview", 250)
    assert any("Stop" in w for w in windows), "--discard-preview is not tied to Stop"


CONSENT_REFUSALS = ("base_moved", "work_changed", "index_changed", "hook_changed_tree",
                    "consent_mismatch", "preview_replaced", "date_changed")


def _code_keys() -> set[str]:
    """`details` keys the release code produces: the effects layer's
    (test_skill_prose_changelog._effects_detail_keys) plus the stamp
    transaction's refusals (core/changelog.py: R11, R12)."""
    keys = set(_effects_detail_keys())
    tree = ast.parse((ROOT / "dct" / "core" / "changelog.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys |= {k.value for k in node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        elif isinstance(node, ast.Call):
            keys |= {k.arg for k in node.keywords if k.arg}
    return keys


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_a_changed_state_previews_again_under_one_heading(text):
    """R4, R5, R6, R7, R9a, R9, R11, R12 → preview again (no message once W
    was made) and the screen headed "Something changed since you said
    Release: …", naming `made` — never a silent second apply. The keys are
    the code's own."""
    missing = set(CONSENT_REFUSALS) - _code_keys()
    assert not missing, f"not produced by the release code: {sorted(missing)}"
    plain = _plain(_new_release(text()))
    for key in CONSENT_REFUSALS:
        assert key in plain, key
    windows = _windows(plain, "Something changed since you said Release", 700)
    assert windows, 'no "Something changed since you said Release" heading'
    assert any("prepare --preview" in w and re.search(r"\bmade\b", w)
               and re.search(r"never[^.]{0,60}(apply again|re-?appl)", w, re.IGNORECASE)
               for w in windows), \
        "the changed-state passage does not preview again, name made and forbid re-applying"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_a_rejecting_hook_stops_with_its_output_never_no_verify(text):
    plain = _plain(_new_release(text()))
    windows = _windows(plain, "--no-verify", 300)
    assert any(re.search(r"hook", w) and re.search(r"\b[Ss]top", w) and "output" in w
               for w in windows), "a rejecting hook does not stop with its output"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_exit_75_before_the_stamp_reruns_the_same_apply_once(text):
    """Adoption makes the same `apply --consent C` safe to run again — ONE
    automatic re-run, then a plain sentence; never a loop."""
    plain = _plain(_new_release(text()))
    windows = _windows(plain, "75", 300)
    assert any("apply --consent" in w and re.search(
        r"\b(one|once)\b[^.]{0,40}re-?run|re-?run[^.]{0,40}\bonce\b", w, re.IGNORECASE)
        for w in windows), \
        "exit 75 does not re-run the same apply --consent once"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_every_other_refusal_is_one_plain_sentence_and_run_release_again(text):
    plain = _plain(text())
    assert "one plain sentence" in plain.lower()
    assert "Technical details" in plain
    assert "run /release again" in plain, 'the one phrase for coming back is "run /release again"'


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_4_nothing_proven_with_more_than_four_waiting_asks_one_multiselect_first(text):
    plain = _plain(_new_release(text()))
    assert '"Which of these are already in your commits?"' in plain
    assert "multiSelect" in plain
    assert any({"--preview", "--attest"} <= set(f)
               for f in _release_calls(text(), "prepare")), "no preview with --attest"


# ── 5. a commit made under the owner's consent: the Finish question ─────────


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_5_a_consent_with_a_commit_made_is_finished_not_previewed_again(text):
    """F: `pending_consent.consent_digest` set and `adoptable` → ONE
    question, Finish (the same apply --consent, adoption makes it safe) or
    Stop — routed before any new preview; never the Legacy path."""
    plain = _plain(_from_step0(_new_release(text())))
    _ordered(plain, "pending_consent.consent_digest", "dct release prepare --preview")
    assert "adoptable" in plain
    windows = _windows(plain, "pending_consent", 600)
    finish = [w for w in windows if re.search(r'"Finish[^"]*"', w)]
    assert finish, 'no "Finish …" option next to pending_consent'
    assert any("apply --consent" in w and re.search(r"\bStop\b", w) for w in finish), \
        "Finish does not run apply --consent, or Stop is not offered"
    for m in re.finditer(r'"Finish[^"]*"', plain):
        near = plain[max(0, m.start() - 300):m.end() + 300]
        assert "Legacy" not in near, f"the Legacy path is offered with Finish: {near}"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_5_a_preview_without_a_commit_is_only_a_draft(text):
    """`consent_digest` null → the slot is a draft: the next /release
    previews again (it replaces the draft); no Finish question."""
    plain = _plain(_new_release(text()))
    assert re.search(r"(consent_digest[^.]{0,160}null|without (a )?(commit|progress)|"
                     r"no progress)[^.]{0,200}(draft|preview again|replace)", plain,
                     re.IGNORECASE), "a preview without a commit is not treated as a draft"


# ── 6. plain language: no internal vocabulary in what the user sees ─────────

FORBIDDEN = re.compile(r"no_trailer|attest|digest|candidate|legacy path|snapshot", re.IGNORECASE)
COMMAND_LINE = re.compile(r"^\s*(dct |ToolSearch\(|git |\$ )")


def shown_texts(body: str) -> list[str]:
    """What the dialog shows the user: every double-quoted string of the
    prose (option labels, messages — also inside backticks: `"Stop"`) and
    every screen / report sample in a fenced block that is not a command
    block. Tables are the agent's reference (their quoted values are
    `details` values), and a line starting "Technical details" is the one
    place identifiers are allowed."""
    texts: list[str] = []
    prose = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("|"))
    for lang, block in re.findall(r"```(\w*)\n(.*?)```", prose, re.DOTALL):
        lines = [line for line in block.splitlines() if line.strip()]
        if lang in {"bash", "sh", "shell", "json"} or all(COMMAND_LINE.match(x) for x in lines):
            continue
        texts += [x.strip() for x in lines if not x.strip().startswith("Technical details")]
    prose = re.sub(r"```.*?```", " ", prose, flags=re.DOTALL)
    texts += re.findall(r'`"([^"`]+)"`', prose)
    outside = _flat(re.sub(r"`[^`]*`", " ", prose))
    texts += re.findall(r'"([^"]{2,300})"', outside) + re.findall(r"“([^”]{2,300})”", outside)
    return [t for t in texts if not t.startswith("Technical details")]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_6_nothing_shown_to_the_user_uses_internal_vocabulary(text):
    hits = sorted({t for t in shown_texts(text()) if FORBIDDEN.search(t)})
    assert not hits, f"internal words in user-facing text: {hits}"


def test_6_the_scanner_finds_labels_messages_and_samples():
    """The scanner itself: it must SEE what it is meant to judge (a
    scanner that sees nothing passes every document)."""
    sample = ('Ask: `"Attest it"` / "Use the candidate" and a table\n'
              '| `details.x` | "not shown" |\n'
              "```\nCandidate: abc1234\nTechnical details: digest abc\n```\n"
              "```bash\ndct release plan --json\n```\n")
    found = shown_texts(sample)
    assert "Attest it" in found and "Use the candidate" in found
    assert "Candidate: abc1234" in found
    assert not any("not shown" in t or "digest abc" in t or "dct release" in t for t in found)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_6_identifiers_go_only_under_a_final_technical_details_line(text):
    plain = _plain(text())
    assert re.search(r"(last|final)[^.]{0,20}Technical details|"
                     r"Technical details[^.]{0,120}(last|final) line", plain), \
        'the report does not keep identifiers under a final "Technical details" line'


# ── 7. the question count of H4, in decisions ───────────────────────────────

PATHS = {
    "typical": r"typical",
    "dirty": r"dirty|uncommitted",
    "all": r"with all",
    "another": r"another (bump|version)",
    "proven_le4": r"nothing proven.*(≤ ?4|4 or fewer|at most 4|up to 4)",
    "proven_gt4": r"nothing proven.*(> ?4|more than 4)",
    "first": r"first version",
}


def count_table(body: str) -> dict[str, dict[str, int]]:
    """{path: {"dialogs": n, "answers": n}} from the document's question-
    count table — the table whose header has a Dialogs and an Answers (or
    Decisions) column; each count is the first integer of its cell."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        header = [c.strip() for c in line.strip().strip("|").split("|")]
        dialogs = [j for j, c in enumerate(header) if re.search(r"dialogs?", c, re.IGNORECASE)]
        answers = [j for j, c in enumerate(header)
                   if re.search(r"answers?|decisions?", c, re.IGNORECASE)]
        if not dialogs or not answers:
            continue
        rows = {}
        for row in lines[i + 2:]:
            if not row.lstrip().startswith("|"):
                break
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            for name, pattern in PATHS.items():
                if re.search(pattern, cells[0], re.IGNORECASE):
                    assert name not in rows, f"two rows for {name}: {cells[0]}"
                    numbers = [re.search(r"\d+", cells[j]) for j in (dialogs[0], answers[0])]
                    assert all(numbers), f"no count in {cells}"
                    rows[name] = {"dialogs": int(numbers[0].group()),
                                  "answers": int(numbers[1].group())}
        return rows
    raise AssertionError("no question-count table (Dialogs / Answers columns)")


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_7_the_question_count_table_counts_decisions_per_path(text):
    """H4, counted in answers (one AskUserQuestion = one dialog, one
    question in it = one answer)."""
    rows = count_table(text())
    assert set(rows) == set(PATHS), sorted(set(PATHS) ^ set(rows))
    expected = {"typical": (1, 2), "dirty": (1, 2), "all": (1, 2), "another": (2, 4),
                "proven_le4": (1, 2), "proven_gt4": (2, 3), "first": (1, 1)}
    got = {name: (row["dialogs"], row["answers"]) for name, row in rows.items()}
    assert got == expected


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_7_the_counts_follow_from_the_one_screen(text):
    """The invariants behind the numbers: every path that ends on the
    screen once costs what the typical one costs; another version is the
    screen twice; the >4 question adds exactly one answer; a first version
    is the screen without its version question (K-63: Q2 omitted)."""
    rows = count_table(text())
    screen = rows["typical"]["answers"]
    assert rows["typical"]["dialogs"] == 1
    for name in ("dirty", "all", "proven_le4"):
        assert rows[name] == rows["typical"], name
    assert rows["first"] == {"dialogs": 1, "answers": screen - 1}
    assert rows["another"] == {"dialogs": 2, "answers": 2 * screen}
    assert rows["proven_gt4"] == {"dialogs": 2, "answers": screen + 1}
    plain = _plain(text())
    assert re.search(r"dialog is one AskUserQuestion", plain)
    assert re.search(r"answer is one question", plain)


# ── 8. the old steps are gone ────────────────────────────────────────────────


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_legacy_question_for_a_missing_older_record(text):
    """Old Step 2: a missing older record is one report sentence; the
    Legacy path is reached only on the owner's explicit request."""
    plain = _plain(_new_release(text()))
    assert not re.search(r"offer[^.]{0,20}(the )?\**legacy path", plain, re.IGNORECASE), \
        "the Legacy path is still offered"
    for sentence in re.split(r"(?<=[.;])\s+", plain):
        if "Legacy path" in sentence:
            assert re.search(r"explicit|asks|asked|only when the owner|on request", sentence), \
                f"the Legacy path is reachable without an explicit request: {sentence}"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_work_in_progress_question(text):
    """Old Step 3: a dirty tree is one screen (H-3 (b)); dct commits it
    inside apply --consent."""
    plain = _plain(text())
    assert '"Commit the work in progress now"' not in plain
    assert not re.search(r"\bask:? (whether to )?commit the work in progress", plain,
                         re.IGNORECASE), "the work-in-progress question is still asked"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_per_entry_or_per_group_questions(text):
    """Old Step 5: unproven entries wait by default and fold into "Release
    with all N"; there is no question per entry or per attestation group."""
    plain = _plain(text())
    for old in ('"Attest — the change is in the candidate"', '"Decide each entry"',
                '"Leave them unreleased"', '"Keep it unreleased"', '"Withdraw it"',
                '"Correct it"', "Each remaining entry without proof"):
        assert old not in plain, old
    # the fallback's unquoted forms of the same questions
    for old in (r"each remaining unproven entry", r"attest together / decide each",
                r"each attestation group"):
        assert not re.search(old, plain, re.IGNORECASE), old
    assert not re.search(r"one question per[^.]{0,20}attestation_groups", plain)
    assert not re.search(r"never inferred from earlier words", plain), \
        "the old version question text is still there"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_bind_question(text):
    """Old Step 7: no plan bound to a version and a selection, no summary
    question, no snapshot digest — the preview binds the content."""
    body = text()
    for flags in _release_calls(body, "plan"):
        assert not {"--version", "--entries", "--no-entries"} & set(flags), flags
    assert "snapshot_digest" not in _plain(_new_release(body))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_prepare_question(text):
    """Old Step 8: no separate approval of the files and the push target."""
    body = text()
    plain = _plain(body)
    for old in ('"Release with these files', '"Other push target"', '"Not these files',
                '"Local release — no push"'):
        assert old not in plain, old
    for flags in _release_calls(body, "prepare"):
        assert "--plan-digest" not in flags, flags
        assert {"--preview", "--discard-preview"} & set(flags) or len(flags) <= 1, flags


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_8_no_apply_question(text):
    """Old Step 9: "Release" on the screen IS the apply yes — no D/P apply,
    no separate apply step waiting for another yes."""
    body = text()
    for flags in _release_calls(body, "apply"):
        assert not {"--plan-digest", "--proposal-digest", "--version", "--entries",
                    "--no-entries"} & set(flags), flags
    assert not re.search(r"Apply — only on the owner's yes", body)


# ── 9. the review of the rewritten dialog (newcomer and safety lenses) ──────


def _table(body: str, header: str) -> list[list[str]]:
    """The rows (cells) of the first Markdown table whose header line starts
    with `header`."""
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(header))
    rows = []
    for line in lines[start + 2:]:
        if not line.strip().startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


def _keys(cell: str) -> set[str]:
    return set(re.findall(r"`(?:details\.)?([a-z_]+)`", cell))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_finish_question_says_what_is_released_and_that_the_commit_stays(text):
    """The Finish question names the version and entries the owner said
    Release to (`pending_consent.applied` — never a digest) and its Stop
    says the commit stays; an attestation-only progress is named as such."""
    plain = _plain(_new_release(text()))
    assert "pending_consent.applied" in plain
    assert re.search(r'"You said Release for proj-a [0-9.]+ \(#\d+, #\d+\) and dct committed '
                     r'your changes as [0-9a-f]+, but the release didn\'t finish\."', plain)
    assert re.search(r'"Finish releasing [0-9.]+"', plain)
    assert re.search(r'"Stop — nothing is released; [0-9a-f]+ stays on your branch"', plain)
    assert re.search(r"marked #\d+, #\d+ as done in [0-9a-f]+", plain), \
        "an attestation-only progress is not named"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_a_commit_made_under_a_consent_that_cannot_be_finished_is_named(text):
    """`consent_digest` null does NOT always mean a draft: a recorded commit
    that is not adoptable (HEAD moved, another checkout) is said before the
    next preview replaces the slot; another checkout stops with its path."""
    plain = _plain(_new_release(text()))
    assert re.search(r"consent_digest`? null but `?progress`? records a commit", plain)
    assert "pending_consent.checkout_root" in plain
    assert re.search(r'"Your changes are already committed as [0-9a-f]+; the release you '
                     r'approved earlier didn\'t finish, so here it is again\."', plain)
    assert re.search(r'was started in <checkout_root> — run /release again in that folder', plain)


def test_9_pending_consent_is_routed_before_the_changelog_file_in_all_three_documents():
    """One routing order everywhere: the Finish question (pending_consent)
    before the changelog-file question."""
    for body in (_skill("release"), _release_fallback()):
        plain = _plain(_from_step0(_new_release(body)))
        _ordered(plain, "pending_consent.consent_digest", "changelog_file.problem")
    workflow = _plain((ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8"))
    assert re.search(r"routing only, in this order: an unfinished record, the executor, "
                     r"pending_consent[^)]*\)[^.]{0,40}then the changelog file", workflow), \
        "release-workflow.md does not route pending_consent before the changelog file"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_after_answer_table_covers_every_answer_pair(text):
    """Release / Release with all N / Change something / Stop, each with
    Keep or another version: another version keeps EVERY earlier flag, and
    after "Release with all N" the re-preview carries the all variant's
    attested ids, so the new screen's first option is that release."""
    rows = {r[0]: r[1] for r in _table(text(), "| answer | what runs |")}
    for answer in ('"Release" + "Keep"', '"Release with all N" + "Keep"',
                   '"Release" + another version V2', '"Release with all N" + another version V2',
                   '"Change something" + any version', '"Stop" + any version'):
        assert answer in rows, f"no row for {answer}: {sorted(rows)}"
    other = rows['"Release" + another version V2']
    assert "every earlier flag" in other and "--entries" in other and "--no-entries" in other
    assert "--attest <variants[all].consent.attest.ids>" in \
        rows['"Release with all N" + another version V2']
    assert "--version <V2>" in rows['"Change something" + any version']


ALL_V2 = '"Release with all N" + another version V2'


def _all_v2_flags(body: str, earlier: list[str], values: dict[str, str]) -> list[str]:
    """The re-preview the "Release with all N" + V2 row describes, applied
    to the EARLIER preview's flags: an earlier flag the row says to drop or
    replace goes; the row's own flags are added (a flag the row adds only
    "when an --entries list was passed before" only then); every other
    earlier flag stays. `values` fills the placeholders."""
    row = _plain({r[0]: r[1] for r in _table(body, "| answer | what runs |")}[ALL_V2])
    dropped = {f for f in ("--entries", "--no-entries", "--attest")
               if re.search(rf"\b(drop|replac|remov)\w*\b[^.;]{{0,160}}{f}(?![\w-])", row)}
    conditional = bool(re.search(r"when an? --entries list was passed before", row))
    out = [f for f in earlier if f not in dropped]
    for flag in ("--version", "--entries", "--attest"):
        if not re.search(rf"{flag} <", row) or flag in out:
            continue
        if flag == "--entries" and conditional and "--entries" not in earlier:
            continue
        out.append(flag)
    return [x for f in out for x in ([f, values[f]] if f in values else [f])]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_release_with_all_and_another_version_replaces_the_entry_selection(text):
    """K-62: "Release with all N" + V2 REPLACES the earlier entry selection
    with exactly the "all" variant's (`--entries <selected> --attest
    <ids>`, `--no-entries` dropped — unconditionally) and keeps the version
    and the rest of the scope."""
    row = _plain({r[0]: r[1] for r in _table(text(), "| answer | what runs |")}[ALL_V2])
    assert _affirmed(row, r"\b(drop|replac|remov)\w*\b[^.;]{0,160}--no-entries"), row
    assert "--entries <variants[all].consent.entries.selected>" in row
    assert "--attest <variants[all].consent.attest.ids>" in row
    assert not re.search(r"when an? --entries list was passed before", row), \
        "the all variant's selection is still added only after an earlier --entries"
    assert "--version <V2>" in row and re.search(r"\b(keep|stay)", row)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_nothing_proven_says_how_an_entry_goes_in(text):
    plain = _plain(_new_release(text()))
    assert re.search(r'"None of your commits is linked to these entries yet \(a commit links one '
                     r"with a line like 'Changelog: \d+'\)\. If the work is already committed, "
                     r"choose 'Release with all \d+': dct adds an empty commit recording that\.\"",
                     plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_empty_commit_of_the_shown_variant_is_on_the_screen(text):
    """`--attest` on the shown variant makes an extra commit at Release: the
    screen states it whenever `consent.attest` is not null."""
    plain = _plain(_new_release(text()))
    assert re.search(r"variants\[shown\]\.consent\.attest[^.]{0,40}not null", plain)
    assert '"First an empty commit records that #2 is already done."' in plain


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_every_consent_mismatch_key_has_plain_words(text):
    """`consent_mismatch.changed` holds top-level keys of the consent view:
    each one has its plain words, so none reaches the owner raw."""
    from dct.core.releases import CONSENT_VIEW_KEYS
    said = set().union(*(_keys(row[0]) for row in _table(text(), "| key | say |")))
    assert set(CONSENT_VIEW_KEYS) <= said, sorted(set(CONSENT_VIEW_KEYS) - said)
    assert "consent_mismatch.changed" in _plain(text())


def test_9_the_worked_example_follows_the_suggestion_rules():
    """Fixes only → a patch; the "all" variant adds an Added entry → a minor
    version; the "all" line, the label and the Keep label agree."""
    body = _skill("release")
    [screen] = re.findall(r"^```\n(Ready to release.*?)^```", body, re.DOTALL | re.MULTILINE)
    target, now = re.search(r"Ready to release proj-a ([0-9.]+) \(now ([0-9.]+)\)",
                            screen).group(1, 2)
    goes_in = screen.split("Goes in")[1].split("Not included yet")[0]
    categories = re.findall(r"^\s+(Added|Changed|Fixed|Removed)\b", goes_in, re.MULTILINE)
    assert categories and set(categories) == {"Fixed"}, categories
    major, minor, patch = map(int, target.split("."))
    assert target == f"{major}.{minor}.{patch}" and now == f"{major}.{minor}.{patch - 1}"
    waiting = screen.split("Not included yet")[1].split("Files:")[0]
    assert "Added" in waiting
    v_all = re.search(r"With all (\d+): ([0-9.]+)", screen)
    assert v_all.group(2) == f"{major}.{minor + 1}.0"
    plain = _plain(body)
    assert f'"Release with all {v_all.group(1)} as {v_all.group(2)}"' in plain
    assert (f'"Keep the suggested version: {target}, or {v_all.group(2)} with all '
            f'{v_all.group(1)} (recommended)"') in plain


REFUSAL_HEADER = "| refusal | say (one plain sentence) | then |"
RELEASE_HEADER = "| details | what it means — say it in one plain sentence | next action |"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_each_refusal_key_has_one_row(text):
    """One key, one sentence, one next step: `tags` lives only in the
    release table (local tag, "not on origin"), `in_progress` /
    `conflicted` only in the first one; `checkout_changed` is split by
    `details.sync`."""
    first = _table(text(), REFUSAL_HEADER)
    seen: dict[str, int] = {}
    for row in first:
        for key in _keys(row[0]) - {"sync"}:
            seen[key] = seen.get(key, 0) + 1
    twice = {k for k, n in seen.items() if n > 1} - {"checkout_changed"}
    assert not twice, f"keys with two rows in the first table: {sorted(twice)}"
    assert "tags" not in seen
    split = [row for row in first if "checkout_changed" in row[0]]
    assert len(split) == 2 and any("with" in r[0] and "sync" in r[0] for r in split) \
        and any("without" in r[0] for r in split), split
    release = _table(text(), RELEASE_HEADER)
    tags = [row for row in release if "tags" in _keys(row[0])]
    assert len(tags) == 1 and "not on origin" in tags[0][1], tags
    staged = [row for row in release if "in_progress" in _keys(row[0])]
    assert staged and "in_progress" in staged[0][2] and "first table" in staged[0][2]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_every_stop_names_a_true_next_step(text):
    rows = {frozenset(_keys(r[0])): r for r in _table(text(), REFUSAL_HEADER)}

    def row(key: str) -> list[str]:
        [found] = [r for keys, r in rows.items() if key in keys]
        return found

    assert "--no-skip-worktree" in row("hidden_index_flags")[1]
    secret = row("secret_like")[1]
    assert "git restore" in secret and "git rm --cached" in secret and "TRACKED" in secret
    assert "/commit" in row("intent_to_add")[1] and "git rm --cached" in row("intent_to_add")[1]
    contract = row("message_contract")
    assert "/commit" in contract[1] and "stop" in contract[2]
    assert "IN FULL" in row("remote_tag_exists")[2]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_a_network_error_is_recognised_by_its_shape(text):
    """`git_error` + `url` (no hook, rewrite rule or remote name) = the push
    destination could not be read → the one automatic re-run; the `url` row
    and the hook row say they are not that case."""
    plain = _plain(text())
    assert re.search(r"git_error with (details\.)?url and none of hook, rewrite_rule, "
                     r"remote_name[^.]{0,120}ONE automatic re-run", plain), \
        "the network-error shape is not named next to the automatic re-run"
    release = _table(text(), RELEASE_HEADER)
    url = [r for r in release if r[0].startswith("`details.url` WITH")]
    assert url, "the url row does not require rewrite_rule / remote_name"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_retries_stop_after_the_one_automatic_re_run(text):
    plain = _plain(text())
    assert "then wait until it ends, then run the same command once" not in plain
    assert "wait for it, then run the same dct release apply again" not in plain
    assert re.search(r"only retry the (dialog|assistant) makes on its own", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_push_choice_passes_remote_and_branch_only(text):
    """The preview derives the address: `--push-url` only for an address the
    owner picked from `git remote get-url --push --all`."""
    previews = [set(f) for f in _release_calls(_new_release(text()), "prepare")
                if "--preview" in f]
    assert any({"--push-remote", "--push-branch"} <= f and "--push-url" not in f
               for f in previews), "no push choice without a derived --push-url"
    plain = _plain(_new_release(text()))
    assert re.search(r"--push-url[^.]{0,80}only for an address the owner picked", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_a_made_commit_is_named_in_every_refusal(text):
    plain = _plain(_new_release(text()))
    assert re.search(r"when details\.made holds a commit, the sentence names it", plain)
    windows = _windows(plain, "stopped the commit", 300)
    assert any("already committed as abc1234" in w for w in windows), \
        "a rejecting hook does not name the commit already made"
    assert re.search(r'"Stop — nothing is released; your changes stay committed as abc1234', plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_an_unread_remote_adds_one_line(text):
    plain = _plain(_new_release(text()))
    assert re.search(r"remote\.checked false[^.]{0,120}"
                     r"origin could not be reached now — it is checked again when you release",
                     plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_digest_rule_names_its_one_exception(text):
    plain = _plain(text())
    assert re.search(r"The one exception is the Finish question[^.]{0,80}"
                     r"pending_consent\.consent_digest", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_an_unfinished_release_asks_one_question(text):
    """A bare /release with an unfinished record: ONE question, Finish (with
    what will be done) or Not now — never a dead end."""
    section = _plain(_dct_executes(text()))
    branch = section[section.index("something else"):]
    assert re.search(r'"Finish [0-9.]+ \(push it to origin/main\)"', branch)
    assert '"Not now"' in branch and "dct release resume --json" in branch


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_version_script_is_named_by_its_path(text):
    shown_text = " ".join(shown_texts(text()))
    assert "scripts/dct-version-sync.sh" in shown_text
    assert re.search(r"No version file changes — dct changes them only through "
                     r"scripts/dct-version-sync\.sh", _plain(text()))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_untracked_release_artifacts_count_as_uncommitted_changes(text):
    plain = _plain(_new_release(text()))
    assert re.search(r"index\.clean[^.]{0,80}OR any index\.release_artifacts\[\]\.untracked",
                     plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_the_more_than_four_question_is_single_select_on_the_waiting_entries(text):
    plain = _plain(_new_release(text()))
    at = plain.index('"Which of these are already in your commits?"')
    before = plain[max(0, at - 700):at + 100]
    assert "single-select" in before
    assert "display.entries.waiting" in before and "whatever all_unavailable says" in before


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_uncertain_entries_point_at_release_with_all(text):
    plain = _plain(_new_release(text()))
    assert "might be part of this work too. If so, choose 'Release with all" in plain


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_a_missing_older_record_is_one_short_sentence(text):
    plain = _plain(_new_release(text()))
    assert "0.1.0 was released before you used dct — nothing to do." in plain
    assert 'say "record 0.1.0" if you ever want it' not in plain


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_an_old_cli_stop_names_the_upgrade(text):
    plain = _plain(text())
    assert re.search(r"older than this /release\. Upgrade it with dct upgrade \(or uv tool "
                     r"upgrade dctracker, or pipx upgrade dctracker", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_9_every_local_reason_has_plain_words(text):
    from dct.core.releases import LOCAL_PUSH_REASONS
    plain = _plain(_new_release(text()))
    at = plain.index("not pushed: <reason>")
    passage = plain[at:at + 900]
    for reason in LOCAL_PUSH_REASONS:
        assert re.search(rf"{reason} \"", passage), reason


def test_9_release_workflow_stops_on_the_commit_msg_hook():
    """R8 never previews again: a new screen meets the same hook."""
    flat = _flat((ROOT / "docs" / "release-workflow.md").read_text(encoding="utf-8"))
    assert "After R4-R12" not in flat and "a refusal of R4-R12" not in flat
    assert re.search(r"After R4-R7 and R9-R12[^.]+\. R8 \(`message_contract`\) stops", flat)


# ── 10. the final read: rare-case rows with a true next step ────────────────


def _release_row(body: str, key: str) -> list[str]:
    """The FIRST row of the release table carrying `key` in its first
    column — the row the first-match rule picks."""
    for row in _table(body, RELEASE_HEADER):
        if key in _keys(row[0]):
            return row
    raise AssertionError(f"no release-table row for {key}")


def _row_index(body: str, key: str) -> int:
    return next(i for i, row in enumerate(_table(body, RELEASE_HEADER)) if key in _keys(row[0]))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_a_foreign_staged_refusal_is_not_read_as_a_hook(text):
    """`_rewrite_index` refuses with `foreign_staged`, `commit` AND `head`:
    under the first-match rule its own row must come before the commit row
    that lists `head`, or the owner reads "Your pre-commit hook stopped …"."""
    body = text()
    assert _row_index(body, "foreign_staged") < _row_index(body, "head")
    assert "You have other staged changes in" in _release_row(body, "foreign_staged")[1]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_a_remote_tag_after_the_stamp_never_offers_another_version(text):
    """`_pushed` also refuses with `remote_tags` during the push step, when V
    is already stamped and tagged here: that case is half-done and goes to
    resume; another version is offered only before the stamp."""
    _, say, then = (_plain(c) for c in _release_row(text(), "remote_tags"))
    before, after = say.split("AFTER the stamp", 1)
    assert "BEFORE the stamp" in before and "already exists on origin" in before
    assert re.search(r"carries the release record", after)
    assert re.search(r'"Release [0-9.]+ is half-done: origin already has v[0-9.]+ at another '
                     r'commit\."', after)
    then_before, then_after = then.split("After the stamp", 1)
    assert "--version <V2>" in then_before
    assert "never another version" in then_after and "dct release resume" in then_after
    assert "--version" not in then_after


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_an_unconfirmed_push_after_the_stamp_resumes(text):
    """The post-push check ("does not show … — not recorded as pushed")
    carries only `push_target`: after the stamp it is half-done and goes to
    resume — never "Change something" or "Release locally"."""
    # The first row a refusal with `push_target` ALONE matches (the row
    # above it needs `git_error` beside it).
    [row] = [r for r in _table(text(), RELEASE_HEADER) if r[0].startswith("`details.push_target`")]
    _, say, then = (_plain(c) for c in row)
    assert re.search(r"AFTER the stamp[^.]{0,120}release record and push_target alone", say)
    assert '"Release 0.2.0 is half-done: the push ran but origin doesn\'t show it yet."' in say
    after = then[then.index("After the stamp with push_target alone"):]
    assert re.search(r'never "Change something" or "Release locally"', after)
    assert re.search(r"owner checks the remote; when the owner says[^.]{0,40}dct release resume", after)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_version_unresolved_is_asked_before_any_refusal_row(text):
    """`version_unresolved` is a refusal (exit 1), not an outcome: the
    dialog tests it — and outcome `failed` — BEFORE the generic refusal
    rule, and the first table has a row that asks the version question
    instead of the catch-all stop."""
    plain = _plain(_new_release(text()))
    assert re.search(r"test these two FIRST, before any refusal row: "
                     r"details\.version_unresolved[^→]{0,80}→ the \"Which version is this "
                     r"release\?\" question, and outcome failed[^→]{0,120}→ the stop"
                     r".{0,120}[Oo]nly then any other refusal → \"When a command refuses\"",
                     plain), "version_unresolved / failed are not tested before the refusals"
    [row] = [r for r in _table(text(), REFUSAL_HEADER) if "version_unresolved" in _keys(r[0])]
    assert '"Which version is this release?"' in row[1]
    assert "--version <V>" in row[2] and "never the CLI's message and stop" in row[2]
    failed = plain[plain.index("Outcome failed (the version script failed"):][:700]
    assert "details.sync" in failed and "a failed result has no display" in failed
    assert "display.sync" not in failed


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_a_version_only_consent_previews_again_with_no_entries(text):
    """`--entries ''` is refused and no flag selects every proven id: an
    empty `applied.entries` becomes `--no-entries`."""
    plain = _plain(_new_release(text()))
    assert re.search(r"applied\.entries empty → --no-entries: an empty --entries is refused, "
                     r"and leaving the flag out would select every proven entry", plain)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_dialog_b_fits_the_four_option_limit(text):
    """AskUserQuestion allows at most 4 options: "Which entries go in?" and
    "Where is it pushed?" say what replaces the per-item options."""
    plain = _plain(_new_release(text()))
    entries = plain[plain.index('"Which entries go in?"'):][:600]
    assert re.search(r"at most 4 options: more than 3 entries go in → \"Keep all\" and \"None — "
                     r"release only the version number\"", entries)
    assert "Other to type the ids" in entries
    push = plain[plain.index('"Where is it pushed?"'):][:1500]
    assert re.search(r"[Aa]t most 4 options: more than 2 other push addresses → only the first two "
                     r"options, the question text lists the other addresses", push)


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_the_finish_label_names_what_is_missing_for_every_status(text):
    """The label comes from the FIRST false status of `resume.effects` —
    every status after `stamped` has its label — and from the record's
    stored push target, `unfinished_release.consent.push_target`."""
    from dct.workflows.release.plan import _EFFECT_ORDER
    section = _plain(_dct_executes(text()))
    branch = section[section.index("something else"):]
    assert re.search(r"FIRST status of resume\.effects[^.]{0,120}that is false", branch)
    assert "unfinished_release.consent.push_target (null = a local release" in branch
    for status in _EFFECT_ORDER[1:]:
        assert re.search(rf"{status}( or [a-z_]+)? → \"Finish [0-9.]+ \(", branch), status
        assert re.search(rf"{status}( or [a-z_]+)? → \"Finish [0-9.]+ here \(", branch), status
    assert '"Finish 0.2.0 (tag and push to origin/main)"' in branch


def _refusal_section(body: str) -> str:
    plain = _plain(body)
    return plain[plain.index("When a command refuses"):]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_the_lookup_reads_the_bullets_first_and_each_key_once(text):
    """The prose cases come before the tables (a network error before the
    stamp matches no row); the network re-run is read before the hook case;
    `unproven` is in one table only, so its row can be reached."""
    section = _refusal_section(text())
    assert re.search(r"Find its row in this order: first the four (bullets|bold cases) below "
                     r"\(in their order\), then the table of preview and apply refusals, then "
                     r"the table of the release's own refusals", section)
    _ordered(section, "The one automatic re-run", "The same network error AFTER the stamp",
             "A hook rejects the commit")
    hook = section[section.index("A hook rejects the commit"):][:200]
    assert "details.hook, BEFORE the stamp" in hook and "git_error" not in hook
    first = set().union(*(_keys(r[0]) for r in _table(text(), REFUSAL_HEADER)))
    release = set().union(*(_keys(r[0]) for r in _table(text(), RELEASE_HEADER)))
    assert "unproven" in release and "unproven" not in first


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_the_commit_row_says_one_true_sentence_per_cause(text):
    """Signing, a moved branch and a missing identity are not a hook: each
    cause has its own sentence, and only the hook causes name a hook."""
    say = _plain(_release_row(text(), "signed")[1])
    causes = {
        "signed": r"signed → \"([^\"]+)\"",
        "moved": r"pinned_ref ≠ head_ref → \"([^\"]+)\"",
        "identity": r"git_error alone before the stamp → \"([^\"]+)\"",
    }
    for cause, pattern in causes.items():
        found = re.search(pattern, say)
        assert found, f"no sentence for {cause}"
        assert "hook" not in found.group(1).lower(), (cause, found.group(1))
    assert re.search(r"hook → \"Your pre-commit hook stopped the release commit", say)
    assert "signing: the owner fixes the signing setup" in _plain(
        _release_row(text(), "signed")[2])


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_10_an_unsafe_suggestion_says_its_own_reason(text):
    """`safe` is false for a taken tag, a major bump and a version counted
    from a tag: the headline is keyed on `suggested.reason`, never "already
    exists" for every case; `variants[shown]` is explained."""
    plain = _plain(_new_release(text()))
    at = plain.index("suggested.safe false")
    passage = plain[at:at + 400]
    assert "keyed on its suggested.reason" in passage
    for reason in ("tag_taken", "breaking", "after_tag"):
        assert re.search(rf"{reason} \"", passage), reason
    assert re.search(r"variants\[shown\] is the entry of the preview's variants list whose name "
                     r"is shown", plain)


# ── 11. the owner's decisions of 2026-10-12 (K-51, K-62, K-66, K-67) ────────


def _first_row(body: str, key: str) -> list[str]:
    for row in _table(body, REFUSAL_HEADER):
        if key in _keys(row[0]):
            return row
    raise AssertionError(f"no preview/apply-table row for {key}")


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_index_only_has_its_own_row_with_a_true_next_step(text):
    """K-66 (1): the preview refuses `index_only` BEFORE it reads a message,
    so "fix the draft, preview again" repeats the refusal. Its own row: the
    staged-only difference is said, the owner unstages (a quoted command
    with `--`) or makes the change, then runs /release again — stop; the
    dialog never unstages for them."""
    body = text()
    row = _first_row(body, "index_only")
    assert _keys(row[0]) == {"index_only"}, f"index_only shares a row: {row[0]}"
    say, then = _plain(row[1]), _plain(row[2])
    assert "git restore --staged -- <paths>" in say and "run /release again" in say
    assert re.search(r"\bstop\b", then) and not re.search(r"preview again", then), then
    assert re.search(r"\bnever\b[^.]{0,60}\bunstage", _plain(row[1] + row[2])), \
        "the row does not say the dialog never unstages for the owner"
    message_rows = [r for r in _table(body, REFUSAL_HEADER) if "message_required" in _keys(r[0])]
    assert "index_only" not in _plain(message_rows[0][2])


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_a_refused_version_leads_back_to_a_version_choice(text):
    """K-66 (3): `version_lower` / `version_taken` / `version_invalid` (the
    preview's and prepare's keys) → one sentence, then a correction: the
    previous screen again (Q2, or "Change something" for a first version)
    or the version question — then a new preview with `--version` and a new
    screen; never the catch-all stop."""
    keys = {"version_lower", "version_taken", "version_invalid"}
    assert keys <= _code_keys(), sorted(keys - _code_keys())
    body = text()
    row = _first_row(body, "version_lower")
    assert keys <= _keys(row[0]), row[0]
    say, then = _plain(row[1]), _plain(row[2])
    assert re.search(r"[Pp]ick another version", say)
    assert re.search(r"previous screen again", then) and "--version <V2>" in then
    assert '"Which version is this release?"' in then and re.search(r"new screen", then)
    assert not re.search(r"\bstop\b", then), then


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_attest_without_entries_drops_no_entries(text):
    """The CLI refuses `--attest` beside `--no-entries`; the dialog's own
    mistake, fixed without the owner: drop `--no-entries`, preview again."""
    assert "attest_without_entries" in _code_keys()
    row = _first_row(text(), "attest_without_entries")
    assert re.search(r"--no-entries", _plain(row[2])) and "preview again" in _plain(row[2])


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_the_push_question_follows_the_screens_push_mode(text):
    """K-66 (4): "Where is it pushed?" is keyed on the shown variant's
    `consent.push.mode`. A pushing screen keeps its own target (its
    remote/branch, never a literal origin/main); a local screen keeps "Keep
    it on this computer" first and offers the configured remotes (`git
    remote`, read-only) with the branch the preview read — never an invented
    target."""
    question = _push_question(text())
    assert '"Keep origin/main"' not in question, "the first option is a literal origin/main"
    assert re.search(r"push\.mode\b[^.]{0,40}\bpush\b", question)
    assert re.search(r"push\.remote\b.{0,30}push\.branch", question)
    local = question[re.search(r"push\.mode\b[^.]{0,20}\blocal\b", question).start():]
    assert _labels(local)[0] == "Keep it on this computer", _labels(local)[:3]
    assert re.search(r"git remote\b(?! get-url)", local), "the configured remotes are not read"
    assert "details.base.branch" in local
    assert "--push-remote <remote> --push-branch <branch>" in _plain(_dialog_b(text()))


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_the_plan_asks_only_about_a_changelog_problem_the_commit_does_not_settle(text):
    """K-51: the plan checks the changelog file against the tree the
    work-in-progress commit will have, so a problem that commit settles is
    never asked; the dialog never claims "the screen commits" a problem
    away, and a problem that comes back after its way was taken is not
    asked twice (the preview decides)."""
    plain = _plain(_new_release(text()))
    step1 = plain[plain.index("changelog_file.problem set"):][:700]
    assert re.search(r"work-in-progress commit", step1) and re.search(r"\bsettle", step1), step1
    assert re.search(r"comes back[^.]{0,200}(never|not)[^.]{0,40}ask[^.]{0,200}preview", step1), \
        "a returning changelog problem is asked again (loop)"
    assert not re.search(r"uncommitted edit of the tracked file is committed by the screen", plain)
    rare = plain[plain.index("not_baseline → by"):][:600]
    assert re.search(r"\bnever\b[^.]{0,80}uncommitted edit", rare), rare


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_new_untracked_files_are_shown_as_included_or_left_out(text):
    """K-67 (5): a new untracked file is never silently dropped: the Step 2
    gate also looks at the plan's `index.untracked`; a file plainly part of
    the work goes in (`--include-untracked`, with a drafted message), every
    other one is listed from the preview's `details.left_out` — also on a
    clean tree, where `details.work` is null — and a secret-like one is
    never included or offered; the dialog stages nothing itself."""
    plain = _plain(_new_release(text()))
    gate = plain[plain.index("Uncommitted changes — the work in progress"):][:2400]
    assert re.search(r"index\.untracked", gate), "the gate ignores new untracked files"
    assert "details.left_out" in plain
    left = " ".join(_windows(plain, "details.left_out", 300))
    assert re.search(r"(work is null|clean tree)", left), left
    assert re.search(r"secret_like", left) and re.search(r"\bnever\b[^.]{0,80}(includ|offer)", left)
    assert re.search(r"\bstages nothing\b[^.]{0,80}--include-untracked", plain), \
        "no rule that a file goes in only through --include-untracked"
    dialog_b = _dialog_b(text())
    assert re.search(r'"Also commit notes\.txt"[^"]{0,250}secret', dialog_b), \
        "Dialog B may offer a secret-like file"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_the_secret_like_row_quotes_its_commands_and_never_sends_to_commit(text):
    """K-54 policy in /release: the commands carry `--`, and the way to keep
    a secret-looking path is the owner's own git — /commit does not stage
    it either."""
    say = _plain(_first_row(text(), "secret_like")[1])
    assert "git rm --cached -- <path>" in say and "git restore --staged --worktree -- <path>" in say
    assert not re.search(r"commit it yourself with /commit", say)


# ── the documented commands on the real CLI ─────────────────────────────────

pytest_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
DIRTY_ONLY = {"--message-file", "--include-untracked"}  # requirement 1: dirty tree only
SAME_AS_BEFORE = DIRTY_ONLY | {"--attest", "--entries", "--no-entries", "--push-remote",
                               "--push-branch", "--push-url", "--no-push"}


@pytest.fixture(autouse=True)
def _hermetic(git_identity):
    """The documented runs make commits."""


@pytest.fixture
def cli(engine, tmp_path, monkeypatch, capsys):
    """(repo, run): the real CLI in proj-r's checkout (a bare `origin`
    upstream) on the test database; run(argv) → (exit code, JSON)."""
    repo, _ = use_cli(make_release_repo(engine, tmp_path), engine, monkeypatch)

    def run(args: list[str]) -> tuple[int, dict]:
        capsys.readouterr()
        code = 0
        try:
            run_cli(monkeypatch, args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, json.loads(capsys.readouterr().out)

    return repo, run


def build(sub: str, flags: list[str], values: dict[str, str], drop=frozenset()) -> list[str]:
    """`dct release <sub>` with the documented flags (minus `drop`): every
    value flag needs a value — a documented flag the test does not know
    fails the run."""
    out = ["release", sub]
    for flag in flags:
        if flag in drop:
            continue
        out.append(flag)
        if flag in values:
            out.append(values[flag])
        else:
            assert flag in BARE_FLAGS | {"--preview", "--discard-preview"}, \
                f"dct release {sub}: no value for {flag}"
    return out


def shown(preview: dict, name: str = "shown") -> dict:
    [found] = [v for v in preview["details"]["variants"] if v["name"] == name]
    return found


def documented_preview(body: str, run) -> dict:
    """The documented preview of a clean tree (no message: dirty only)."""
    flags = documented(body, "prepare", having=("--preview",),
                       lacking=("--discard-preview", "--version"))
    code, doc = run(build("prepare", flags, {}, drop=SAME_AS_BEFORE))
    assert code == 0 and doc["outcome"] == "preview", doc
    return doc


def documented_apply(body: str, run, consent: str) -> tuple[int, dict]:
    return run(build("apply", documented(body, "apply", having=("--consent",)),
                     {"--consent": consent}))


@pytest_git
@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_documented_stop_makes_the_declined_screen_unappliable(text, engine, cli):
    """Stop → the documented discard; afterwards the digest of the declined
    screen is refused (`no_preview`) and nothing is released."""
    body = text()
    repo, run = cli
    a = proven(engine, repo, "feature a")
    preview = documented_preview(body, run)
    declined = shown(preview)["consent_digest"]

    code, doc = run(build("prepare", documented(body, "prepare", having=("--discard-preview",)),
                          {}))
    assert code == 0 and doc["outcome"] == "noop"
    assert doc["details"]["discarded"] == preview["details"]["preview_id"]

    code, doc = documented_apply(body, run, declined)
    assert code == 1 and doc["outcome"] == "refused" and "no_preview" in doc["details"]
    assert get_release(engine, SLUG, "1.1.0") is None
    assert git(repo, "tag", "--list", "v1.1.0") == ""
    assert {e["id"]: e["version"] for e in list_entries(engine, SLUG)}[a] is None


@pytest_git
@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_documented_other_version_replaces_the_screen_the_owner_saw(text, engine, cli):
    """Q2 = another version → the documented re-preview with `--version`:
    the new screen has that version, and the old screen's digest is no
    longer appliable (a version the owner did not see is never released)."""
    body = text()
    repo, run = cli
    a = proven(engine, repo, "feature a")
    first = shown(documented_preview(body, run))
    assert first["consent"]["version"]["target"] == "1.1.0"  # added → minor, pre-selected
    other = first["suggested"]["alternatives"][0]["target"]
    assert other != "1.1.0"

    flags = documented(body, "prepare", having=("--preview", "--version"))
    code, again = run(build("prepare", flags, {"--version": other}, drop=SAME_AS_BEFORE))
    assert code == 0 and again["outcome"] == "preview"
    second = shown(again)
    assert second["consent"]["version"]["target"] == other
    assert second["consent_digest"] != first["consent_digest"]

    code, doc = documented_apply(body, run, first["consent_digest"])
    assert code == 1 and "consent_unknown" in doc["details"]
    code, doc = documented_apply(body, run, second["consent_digest"])
    assert code == 0 and doc["outcome"] == "completed"
    assert get_release(engine, SLUG, other)["entry_ids"] == [a]
    assert remote_refs(repo)[f"refs/tags/v{other}"] == git(repo, "rev-parse", "HEAD")


@pytest_git
@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_documented_none_then_all_then_another_version_shows_the_entries_of_all(
        text, engine, cli):
    """K-62 on the real CLI: Dialog B "None" (`--no-entries`), then the
    screen's "Release with all 1" with another version — the row's
    re-preview shows a screen whose first option releases exactly the
    "all" variant's entries at V2 (never the version-only release of the
    earlier "None"), and applying it releases them."""
    body = text()
    repo, run = cli
    proven(engine, repo, "feature a")
    b = add_entry(engine, SLUG, "added", "feature b")["id"]  # waiting
    none = documented(body, "prepare", having=("--preview", "--no-entries"))
    code, first = run(build("prepare", none, {}, drop=SAME_AS_BEFORE - {"--no-entries"}))
    assert code == 0 and first["outcome"] == "preview", first
    assert shown(first)["version_only"] is True
    all_variant = shown(first, "all")["consent"]
    selected = [e if isinstance(e, int) else e["id"] for e in all_variant["entries"]["selected"]]
    assert b in selected and all_variant["attest"]["ids"] == [b]
    v2 = "3.0.0"

    flags = _all_v2_flags(body, ["--preview", "--no-entries", "--json"],
                          {"--version": v2, "--entries": ",".join(map(str, selected)),
                           "--attest": ",".join(map(str, all_variant["attest"]["ids"]))})
    code, again = run(["release", "prepare", *flags])
    assert code == 0 and again["outcome"] == "preview", again.get("details")
    new = shown(again)
    assert new["version_only"] is False
    assert new["consent"]["version"]["target"] == v2
    assert [e["id"] for e in new["display"]["entries"]["selected"]] == sorted(selected)
    assert new["consent"]["attest"]["ids"] == [b]

    code, doc = documented_apply(body, run, new["consent_digest"])
    assert code == 0 and doc["outcome"] == "completed", doc["details"]
    assert get_release(engine, SLUG, v2)["entry_ids"] == sorted(selected)


@pytest_git
@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_documented_release_with_all_releases_the_waiting_entry_in_one_answer(
        text, engine, cli):
    """One waiting entry → the preview offers the "all" variant; the
    documented apply with ITS digest marks the entry as done (an empty
    commit on H0), releases both and pushes — no second screen."""
    body = text()
    repo, run = cli
    a = proven(engine, repo, "feature a")
    b = add_entry(engine, SLUG, "added", "feature b")["id"]  # no commit says it is done
    h0 = git(repo, "rev-parse", "HEAD")
    preview = documented_preview(body, run)
    assert preview["details"]["all_unavailable"] is None
    assert [e["id"] for e in shown(preview)["display"]["entries"]["waiting"]] == [b]

    code, doc = documented_apply(body, run, shown(preview, "all")["consent_digest"])
    assert code == 0 and doc["outcome"] == "completed", doc["details"]
    record = get_release(engine, SLUG, doc["version"])
    assert record["entry_ids"] == sorted([a, b])
    attest = doc["details"]["made"]["attest_commit"]
    assert git(repo, "rev-parse", f"{attest}^") == h0
    assert git(repo, "rev-parse", "HEAD^") == attest
    assert remote_refs(repo)[f"refs/tags/v{doc['version']}"] == git(repo, "rev-parse", "HEAD")


# ── B4 (K-66): the push choice REPLACES the earlier push flags ──────────────

PUSH_FLAGS = ("--no-push", "--push-remote", "--push-branch", "--push-url")


def _push_flags_replaced(body: str) -> set[str]:
    """The push flags Dialog B says the owner's push choice REPLACES (drops
    from the earlier preview's flags before its own are added) — read from
    the sentence that states the rule, so a document that only says "every
    earlier flag plus the changed ones" replaces nothing."""
    replaced: set[str] = set()
    for sentence in re.split(r"(?<=[.;])\s+", _dialog_b(body)):
        if _affirmed(sentence, r"\breplac\w*\b[^.;]{0,80}\bpush flags?\b"):
            replaced |= {f for f in PUSH_FLAGS if re.search(rf"{f}(?![\w-])", sentence)}
    return replaced


def _after_push_choice(body: str, earlier: list[str], choice: list[str]) -> list[str]:
    """The re-preview's flags after "Where is it pushed?": the earlier flags
    minus the ones the push choice replaces, then the choice's own."""
    replaced = _push_flags_replaced(body)
    return [f for f in earlier if f not in replaced] + [f for f in choice if f not in earlier
                                                         or f in replaced]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_11_the_push_choice_replaces_every_earlier_push_flag(text):
    """K-66: `--no-push` beside `--push-remote` / `--push-branch` is refused
    by the preview (push_target: "--no-push contradicts …"), and a stale
    `--push-url` names the old remote's address. So the choice made under
    "Where is it pushed?" replaces the WHOLE set of push flags — push →
    `--no-push` dropped; local → every `--push-*` dropped; another remote →
    the old `--push-url` dropped."""
    body = text()
    assert _push_flags_replaced(body) == set(PUSH_FLAGS), _push_flags_replaced(body)
    to_push = _after_push_choice(body, ["--preview", "--no-push", "--json"],
                                 ["--push-remote", "--push-branch"])
    assert "--no-push" not in to_push, to_push
    to_local = _after_push_choice(
        body, ["--preview", "--push-remote", "--push-branch", "--push-url", "--json"],
        ["--no-push"])
    assert not {"--push-remote", "--push-branch", "--push-url"} & set(to_local), to_local
    other = _after_push_choice(
        body, ["--preview", "--push-remote", "--push-branch", "--push-url", "--json"],
        ["--push-remote", "--push-branch"])
    assert "--push-url" not in other, other


@pytest_git
@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_documented_push_choice_previews_the_new_target_both_ways(text, engine, cli):
    """On the real CLI: a local screen (`--no-push`) switched to "Push to
    origin/main", then back to "Keep it on this computer" — each re-preview
    built by the documented rule shows the chosen target, where keeping
    every earlier flag is refused."""
    body = text()
    repo, run = cli
    proven(engine, repo, "feature a")
    url = git(repo, "remote", "get-url", "--push", "origin")
    values = {"--push-remote": "origin", "--push-branch": "main", "--push-url": url}

    local = ["--preview", "--no-push", "--json"]
    code, doc = run(build("prepare", local, values))
    assert code == 0 and shown(doc)["consent"]["push"]["mode"] == "local", doc

    naive = local + ["--push-remote", "--push-branch"]   # every earlier flag plus the new
    code, doc = run(build("prepare", naive, values))
    assert code == 1 and doc["outcome"] == "refused", doc

    pushed = _after_push_choice(body, local, ["--push-remote", "--push-branch", "--push-url"])
    code, doc = run(build("prepare", pushed, values))
    assert code == 0 and doc["outcome"] == "preview", doc.get("details")
    push = shown(doc)["consent"]["push"]
    assert (push["mode"], push["remote"], push["branch"], push["url"]) == \
        ("push", "origin", "main", url)

    back = _after_push_choice(body, pushed, ["--no-push"])
    code, doc = run(build("prepare", back, values))
    assert code == 0 and doc["outcome"] == "preview", doc.get("details")
    assert shown(doc)["consent"]["push"]["mode"] == "local"


# ── B2-prose (K-42): a recorded release that cannot be finished here ────────

BLOCKED_WHYS = {"other_checkout", "branch_switched", "head_moved", "progress_lost"}


def _earlier_release(body: str) -> str:
    """The "A commit made under an earlier Release" part of the dialog, up to
    "Previewing again after a commit was made"."""
    plain = _plain(_new_release(body))
    start = re.search(r"(### |\*)A commit made under an earlier Release", plain).start()
    return plain[start:plain.index("Previewing again after a commit was made", start)]


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_12_a_blocked_release_says_its_next_step_and_stops(text):
    """K-42: the plan's `pending_consent.blocked` {why, next_step} says why a
    release approved earlier cannot be finished HERE and the step that works.
    A switched branch (a detached HEAD included) → that one sentence and
    STOP: a new preview on the switched branch would leave the approved
    release — and the commit made under it — behind."""
    part = _earlier_release(text())
    assert "pending_consent.blocked" in part and "blocked.next_step" in part, part[:400]
    named = set(re.findall(r"\b(other_checkout|branch_switched|head_moved|progress_lost)\b", part))
    assert named == BLOCKED_WHYS, named
    assert all(why in planning.pending_consent.__doc__ for why in BLOCKED_WHYS), \
        "a `why` the prose routes is not the plan's"
    switched = [s for s in re.split(r"(?<=[.;])\s+|\s-\s", part)
                if "branch_switched" in s and "blocked.next_step" in s]
    assert switched and re.search(r"\bSTOP\b", switched[0]), switched
    assert re.search(r"\bnever\b[^.;]{0,30}\bnew preview\b[^.;]{0,60}switched branch", part,
                     re.IGNORECASE), \
        "a new preview on the switched branch is not ruled out"


@pytest.mark.parametrize("text", RELEASE_DIALOGS)
def test_12_a_switched_branch_refused_by_apply_is_routed_the_same_way(text):
    """Apply's `base_moved` with `why: "branch_switched"` after a commit was
    made is the same situation: not the "Something moved" re-preview but
    the plan's `pending_consent.blocked`."""
    plain = _plain(_new_release(text()))
    start = plain.index("Something moved")
    bullet = plain[start:plain.index("The one automatic re-run", start)]
    assert re.search(r"branch_switched[^.]{0,200}pending_consent\.blocked", bullet), bullet
