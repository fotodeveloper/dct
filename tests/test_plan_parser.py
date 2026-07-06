"""Regression tests for dct.core.plans.parser — ≥20 fixture cases."""

from dct.core.plans.parser import classify_heading, parse_plan, slugify


# ── Small pure helpers ───────────────────────────────────────────────────────

class TestSlugify:
    def test_basic(self):
        assert slugify("Sprint 1: Hub Redesign") == "sprint-1-hub-redesign"

    def test_non_ascii(self):
        # NFKD strips combining marks only; base letters stay. So Ś→s, ż→z.
        assert slugify("Ścieżka Ważna") == "sciezka-wazna"

    def test_special_chars(self):
        assert slugify("§5.8.1 — Spec Reference") == "5-8-1-spec-reference"

    def test_collapses_whitespace(self):
        assert slugify("  Multiple    Spaces  ") == "multiple-spaces"


class TestClassifyHeading:
    def test_sprint(self):
        assert classify_heading("Sprint 1: Hub Redesign") == ("sprint", "high")
        assert classify_heading("Sprint 0.5 UX Polish") == ("sprint", "high")

    def test_phase(self):
        assert classify_heading("Phase 1 — Foundation") == ("phase", "high")
        assert classify_heading("Phase A — Session Resumption") == ("phase", "high")

    def test_task(self):
        assert classify_heading("Task 1: Write parser") == ("task", "high")
        assert classify_heading("Task M0.1 — deny.toml") == ("task", "high")

    def test_block(self):
        assert classify_heading("Block A: Orchestrator Fixes") == ("block", "high")

    def test_milestone(self):
        assert classify_heading("Milestone M0 — Licensing") == ("milestone", "high")

    def test_adr(self):
        assert classify_heading("ADR-001: Capability-Sliced Ports") == ("decision", "high")
        assert classify_heading("ADR 005: Something") == ("decision", "high")

    def test_changelog_subsections(self):
        assert classify_heading("Added") == ("changelog", "high")
        assert classify_heading("Changed") == ("changelog", "high")
        assert classify_heading("Fixed") == ("changelog", "high")
        assert classify_heading("Removed") == ("changelog", "high")

    def test_adr_section_names(self):
        assert classify_heading("Context") == ("adr_section", "high")
        assert classify_heading("Decision") == ("adr_section", "high")
        assert classify_heading("Consequences") == ("adr_section", "high")
        assert classify_heading("Alternatives") == ("adr_section", "high")

    def test_reference_prose(self):
        assert classify_heading("Appendix") == ("reference", "high")
        assert classify_heading("References") == ("reference", "high")
        assert classify_heading("Open questions") == ("reference", "high")

    def test_uncertain_fallback(self):
        assert classify_heading("Some unusual title") == ("uncertain", "low")
        assert classify_heading("Pivot from original scope") == ("uncertain", "low")

    def test_numbered_spec_headings_classify_as_prose(self):
        """Spec-style numbered headings like '1. Problem' / '4.1 Glossary' / '9.1.1 Discovery'.

        These are chapters of prose — not sprints, not references. Regression
        for live dogfood where 52/52 sections of our own spec fell through
        to 'uncertain' because parser had no rule for numeric prefixes.
        """
        assert classify_heading("1. Problem") == ("prose", "high")
        assert classify_heading("4.1 Glossary") == ("prose", "high")
        assert classify_heading("9.1.1 Discovery phase") == ("prose", "high")
        assert classify_heading("12. Hooks") == ("prose", "high")

    def test_numbered_heading_with_backtick_title_classifies_as_prose(self):
        """Headings like '9.1 `/dct-import` — discover' where the first
        non-whitespace char after the number is non-word. Regression: the
        original `\\w` anchor missed these; widening to `\\S` catches them.
        """
        assert classify_heading("9.1 `/dct-import` — discover") == ("prose", "high")
        assert classify_heading("12.1 `dct-plan-autoscan.sh` (PostToolUse)") == ("prose", "high")
        assert classify_heading("5.2 [bracket title] etc.") == ("prose", "high")
        assert classify_heading("3. (parens first)") == ("prose", "high")

    def test_numbered_but_then_sprint_still_sprint(self):
        """Specific patterns win over the numbered-prose fallback.

        A heading 'Sprint 1' was already sprint; ensure that a naturally-
        numbered but otherwise-matching heading is NOT re-classified as prose.
        """
        # "Sprint 1" matches sprint rule, not prose (sprint rule is earlier).
        assert classify_heading("Sprint 1") == ("sprint", "high")

    def test_lone_number_without_title_stays_uncertain(self):
        """'1.' alone (no title text) is too ambiguous — remains uncertain."""
        assert classify_heading("1.") == ("uncertain", "low")


# ── Full parse_plan fixtures ─────────────────────────────────────────────────

ULID_A = "01HX9Z4K5M7PQRSTVWXYZ00001"
ULID_B = "01HX9Z4K5M7PQRSTVWXYZ00002"
ULID_C = "01HX9Z4K5M7PQRSTVWXYZ00003"
ULID_D = "01HX9Z4K5M7PQRSTVWXYZ00004"


def test_empty_file():
    r = parse_plan("")
    assert r.plan_ulid is None
    assert r.sections == []
    assert r.checkpoints == []
    assert "plan_anchor_missing" in r.warnings


def test_plan_anchor_first_line():
    content = f"<!-- dct:plan:{ULID_A} -->\n# Title\n"
    r = parse_plan(content)
    assert r.plan_ulid == ULID_A
    assert r.plan_anchor_line == 1
    assert "plan_anchor_missing" not in r.warnings


def test_plan_anchor_anywhere_in_file():
    content = f"# Title\n\nSome prose.\n<!-- dct:plan:{ULID_A} -->\n## Sprint 1\n"
    r = parse_plan(content)
    assert r.plan_ulid == ULID_A
    assert r.plan_anchor_line == 4


def test_headings_levels_h1_h6():
    content = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6\n"
    r = parse_plan(content)
    assert [s.depth for s in r.sections] == [1, 2, 3, 4, 5, 6]
    assert [s.heading for s in r.sections] == ["H1", "H2", "H3", "H4", "H5", "H6"]


def test_parent_derived_from_nesting():
    content = (
        "# Plan\n"
        "## Sprint 1\n"
        "### Task A\n"
        "### Task B\n"
        "## Sprint 2\n"
    )
    r = parse_plan(content)
    # 5 sections: Plan (H1), Sprint 1, Task A, Task B, Sprint 2
    assert len(r.sections) == 5
    # parent_ulid cannot be set (no anchors yet), but structural parents recorded
    # Task A's structural parent is Sprint 1 (index 1); Sprint 2's is Plan (index 0).
    # Since ulids are None without anchors, we just verify the anchor-based derivation
    # works downstream by ensuring no crash and no wrong parent_mismatch warning.
    assert all("parent_mismatch" not in w for w in r.warnings)


def test_section_anchor_attaches_to_last_heading():
    content = (
        f"# Plan\n"
        f"## Sprint 1\n"
        f"<!-- dct:section:{ULID_A} -->\n"
        f"### Task 1\n"
        f"<!-- dct:section:{ULID_B} parent={ULID_A} -->\n"
    )
    r = parse_plan(content)
    assert r.sections[1].ulid == ULID_A
    assert r.sections[1].parent_ulid is None  # no explicit parent in comment
    assert r.sections[2].ulid == ULID_B
    assert r.sections[2].parent_ulid == ULID_A


def test_duplicate_section_anchor_warns():
    content = (
        f"## Sprint 1\n"
        f"<!-- dct:section:{ULID_A} -->\n"
        f"<!-- dct:section:{ULID_B} -->\n"
    )
    r = parse_plan(content)
    assert r.sections[0].ulid == ULID_A  # first wins
    assert any("duplicate_section_anchor" in w for w in r.warnings)


def test_checkbox_pending_and_done():
    content = (
        "## Sprint 1\n"
        "- [ ] Task one\n"
        "- [x] Task two\n"
        "- [X] Task three (uppercase)\n"
    )
    r = parse_plan(content)
    assert len(r.checkpoints) == 3
    assert r.checkpoints[0].initial_state == "pending"
    assert r.checkpoints[0].text == "Task one"
    assert r.checkpoints[1].initial_state == "done"
    assert r.checkpoints[2].initial_state == "done"


def test_checkpoint_anchor_inline():
    content = (
        f"## Sprint\n"
        f"<!-- dct:section:{ULID_A} -->\n"
        f"- [ ] <!-- dct:cp:{ULID_B} parent={ULID_A} --> Write code\n"
    )
    r = parse_plan(content)
    cp = r.checkpoints[0]
    assert cp.ulid == ULID_B
    assert cp.parent_section_ulid == ULID_A
    assert cp.text == "Write code"


def test_checkpoint_without_anchor_gets_parent_from_section():
    content = (
        f"## Sprint\n"
        f"<!-- dct:section:{ULID_A} -->\n"
        f"- [ ] Bare checkbox, no cp anchor\n"
    )
    r = parse_plan(content)
    cp = r.checkpoints[0]
    assert cp.ulid is None
    assert cp.parent_section_ulid == ULID_A


def test_parent_mismatch_warning_on_checkpoint():
    content = (
        f"## Sprint\n"
        f"<!-- dct:section:{ULID_A} -->\n"
        f"- [ ] <!-- dct:cp:{ULID_B} parent={ULID_C} --> Copied from elsewhere\n"
    )
    r = parse_plan(content)
    assert any("parent_mismatch" in w for w in r.warnings)


def test_orphan_checkpoint_warns():
    content = "- [ ] This has no preceding heading\n"
    r = parse_plan(content)
    assert len(r.checkpoints) == 1
    assert any("orphan_checkpoint" in w for w in r.warnings)


def test_code_fence_skips_checkboxes():
    content = (
        "## Sprint\n"
        "```python\n"
        "# - [ ] This is a comment in a code block\n"
        "x = '- [x] still inside fence'\n"
        "```\n"
        "- [ ] Real checkbox after fence\n"
    )
    r = parse_plan(content)
    assert len(r.checkpoints) == 1
    assert r.checkpoints[0].text == "Real checkbox after fence"


def test_tilde_fence_also_skips():
    content = (
        "## Sprint\n"
        "~~~bash\n"
        "- [ ] fake checkbox\n"
        "~~~\n"
        "- [ ] Real one\n"
    )
    r = parse_plan(content)
    assert len(r.checkpoints) == 1
    assert r.checkpoints[0].text == "Real one"


def test_backtick_fence_does_not_close_tilde_fence():
    """Fences must be matched by their own kind — closing ``` inside ~~~ is literal."""
    content = (
        "## Sprint\n"
        "~~~\n"
        "- [ ] fake one\n"
        "```\n"                    # this is literal inside the tilde fence
        "- [ ] still inside fence\n"
        "~~~\n"
        "- [ ] real checkbox\n"
    )
    r = parse_plan(content)
    assert len(r.checkpoints) == 1
    assert r.checkpoints[0].text == "real checkbox"


def test_sort_order_increments():
    content = (
        "# Plan\n"
        "## Sprint 1\n"
        "## Sprint 2\n"
        "## Sprint 3\n"
    )
    r = parse_plan(content)
    orders = [s.sort_order for s in r.sections]
    assert orders == [0, 10, 20, 30]


def test_heading_line_numbers_tracked():
    content = (
        "# Plan\n"              # line 1
        "\n"                    # line 2
        "Prose.\n"              # line 3
        "## Sprint 1\n"         # line 4
        "\n"                    # line 5
        "### Task\n"            # line 6
    )
    r = parse_plan(content)
    assert r.sections[0].heading_line == 1
    assert r.sections[1].heading_line == 4
    assert r.sections[2].heading_line == 6


def test_checkbox_line_numbers_tracked():
    content = "## Sprint\n- [ ] First\n- [x] Second\n"
    r = parse_plan(content)
    assert r.checkpoints[0].checkbox_line == 2
    assert r.checkpoints[1].checkbox_line == 3


def test_only_headings_no_checkboxes():
    content = "# Plan\n## Sprint 1\n## Sprint 2\n"
    r = parse_plan(content)
    assert len(r.sections) == 3
    assert r.checkpoints == []


def test_only_checkboxes_under_one_heading():
    content = (
        "## Backlog\n"
        "- [ ] A\n"
        "- [ ] B\n"
        "- [ ] C\n"
    )
    r = parse_plan(content)
    assert len(r.sections) == 1
    assert len(r.checkpoints) == 3


def test_full_mini_plan_end_to_end():
    """Integration fixture: plan anchor, H1+H2+H3, code block, checkboxes."""
    content = (
        f"<!-- dct:plan:{ULID_A} -->\n"
        f"# v0.2 Implementation Plan\n"
        f"\n"
        f"Intro prose.\n"
        f"\n"
        f"## Sprint 1: Schema\n"
        f"<!-- dct:section:{ULID_B} parent={ULID_A} -->\n"
        f"\n"
        f"### Task 1: Migration\n"
        f"<!-- dct:section:{ULID_C} parent={ULID_B} -->\n"
        f"\n"
        f"Example:\n"
        f"```sql\n"
        f"CREATE TABLE plans (...);\n"
        f"- [ ] not a checkbox (inside fence)\n"
        f"```\n"
        f"\n"
        f"- [ ] <!-- dct:cp:{ULID_D} parent={ULID_C} --> Write migration 003\n"
        f"- [x] Confirm schema loads in SQLite\n"
    )
    r = parse_plan(content)

    assert r.plan_ulid == ULID_A
    assert len(r.sections) == 3
    assert [s.depth for s in r.sections] == [1, 2, 3]
    assert r.sections[1].ulid == ULID_B
    assert r.sections[1].parent_ulid == ULID_A
    assert r.sections[2].ulid == ULID_C
    assert r.sections[2].parent_ulid == ULID_B
    assert r.sections[1].section_type == "sprint"
    assert r.sections[2].section_type == "task"

    assert len(r.checkpoints) == 2
    assert r.checkpoints[0].ulid == ULID_D
    assert r.checkpoints[0].initial_state == "pending"
    assert r.checkpoints[0].parent_section_ulid == ULID_C
    assert r.checkpoints[1].ulid is None  # no anchor
    assert r.checkpoints[1].initial_state == "done"
    assert r.warnings == []
