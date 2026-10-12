"""K-74 (dogfood of #877): /dct-init is the owner's first contact with dct.

The dogfood owner met six questions in two dialogs, in jargon ("version-sync
script", "template", "block uncommented", "slug"), four of them confirming
values the skill had already detected, and no word about what dct is or why
it registers anything ("as a first contact there could be an introduction and
more hand-holding — what this does and why").

The contract locked here: ONE registration dialog; what dct is and what
registering stores travel inside it (the "Register" preview, per K-73); the
detected folder, name and version are shown, never asked one by one; only
real choices are asked, every option says what it does; the version-sync
offer says what the script does, when it runs and why (K-76 + the owner's
addition) and the script is written by the CLI, never copied by hand.
"""

import math
import re
import textwrap

from dct.assets import skills_dir


def _init() -> str:
    return (skills_dir() / "dct-init" / "SKILL.md").read_text(encoding="utf-8")


def _step(title: str) -> str:
    body = _init()
    at = body.index(title)
    nxt = body.find("\n### ", at + len(title))
    return body[at:nxt if nxt > 0 else len(body)]


def _fenced_after(text: str, anchor: str) -> list[str]:
    """The preview block after `anchor`, dedented to the preview's own margin."""
    at = text.index(anchor)
    block = re.search(r"```\n(.*?)\n[ \t]*```", text[at:], re.DOTALL)
    assert block, f"no preview block after {anchor!r}"
    return textwrap.dedent(block.group(1)).rstrip().splitlines()


def test_registration_is_one_dialog_without_one_by_one_questions():
    body = _init()
    for header in ("Path", "Slug", "Name", "Version"):
        assert f'header: "{header}"' not in body, f"a separate {header} question is back"
    assert "### Step 5: The registration dialog — ONE AskUserQuestion" in body
    assert 'question: "Register <folder name> in dct?"' in body


def test_the_register_preview_says_what_dct_is_and_what_registering_stores():
    lines = _fenced_after(_step("### Step 5"), '"Register" (recommended) — its preview')
    text = "\n".join(lines)
    for what in ("items", "changelog", "handoffs", "releases"):
        assert re.search(rf"^\s+{what}\b", text, re.MULTILINE), f"the intro misses {what}"
    for stored in ("name", "folder", "version"):
        assert re.search(rf"^\s+{stored}\s", text, re.MULTILINE), f"the preview misses {stored}"
    assert "nothing in this folder changes" in text


def test_every_preview_fits_the_dialog():
    """K-73: a preview longer than the terminal is not shown at all; K-79: the
    dialog wraps at about 60 characters, so prose is one line per paragraph
    (never broken by hand) and a list / table line stays under 60. The budget
    of 15 lines counts the lines as the dialog shows them."""
    step = _step("### Step 5")
    for anchor in ('"Register" (recommended) — its preview',
                   '"Yes — add scripts/dct-version-sync.sh (recommended)" — its preview',
                   'f"No — I change {source_file} myself" — its preview'):
        lines = _fenced_after(step, anchor)
        shown = sum(max(1, math.ceil(len(ln) / 60)) for ln in lines)
        assert shown <= 15, (anchor, shown)
        listed = [ln for ln in lines if ln.startswith(" ")]
        assert all(len(ln) < 60 for ln in listed), (anchor, listed)


def test_the_version_sync_offer_says_what_when_and_why():
    lines = _fenced_after(_step("### Step 5"),
                          '"Yes — add scripts/dct-version-sync.sh (recommended)" — its preview')
    text = " ".join(ln.strip() for ln in lines)
    assert "What it does:" in text and "only this line" in text
    assert "When it runs:" in text and "before you approve it" in text
    assert "Why:" in text
    assert "never builds, tags, pushes or publishes" in text
    assert "once it is committed" in text, "the owner is not told the script must be committed"


def test_the_questions_speak_plain_words():
    """No dct jargon in what the owner reads in the dialog: the question
    lines and the option labels of Steps 1, 4 and 5."""
    jargon = re.compile(r"\bslug\b|\btemplate\b|\bsentinel\b|\buncomment|\bblock\b",
                        re.IGNORECASE)
    for title in ("### Step 1", "### Step 4", "### Step 5"):
        for line in _step(title).splitlines():
            stripped = line.strip()
            if stripped.startswith(("question:", "- \"", "- f\"")):
                label = re.match(r'(question: f?|- f?)"([^"]*)"', stripped)
                assert label, stripped
                shown = re.sub(r"<[^>]+>|\{[^}]+\}", "", label.group(2))  # placeholders
                assert not jargon.search(shown), f"{title}: {label.group(2)}"


def test_every_registration_option_says_what_it_does():
    """Q1, Q3 and Q4 options carry a description (or a preview) — the "why"."""
    step = _step("### Step 5")
    for question in ('header: "dct"', 'header: "Commit lines"', 'header: "Allowlist"'):
        at = step.index(question)
        options = step[at:step.find("\n\n", at)]
        labels = [ln.strip() for ln in options.splitlines() if ln.strip().startswith('- "')]
        assert labels, question
        bare = [ln for ln in labels
                if " — " not in ln and ln != '- "Session link only"']
        assert not bare, f"{question}: options without a why: {bare}"


def test_the_script_is_written_by_the_cli_never_copied():
    from dct.cli import build_parser

    body = _init()
    assert "dct project version-sync-script <slug>" in body
    assert not re.search(r"copy dct's|copies dct's|uncomment the blocks", body, re.IGNORECASE)
    args = build_parser().parse_args(["project", "version-sync-script", "proj-a",
                                      "--kind", "pyproject.toml"])
    assert (args.slug, args.kind) == ("proj-a", "pyproject.toml")


def test_a_project_without_a_version_file_gets_no_version_question():
    step = _step("### Step 5")
    assert re.search(r"no version file at all: no question", step)


def test_the_report_tells_the_owner_to_commit_the_new_script():
    report = _step("### Step 7")
    assert re.search(r"Version sync: scripts/dct-version-sync\.sh[^\n]*commit it", report)
