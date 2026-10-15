"""Observable workflow boundaries that must survive both host renderings.

These checks protect actionable prose contracts; they do not prove model obedience
or universal semantic equivalence. All rendering is in memory, without live assets.
"""
import re
import shutil
from pathlib import Path

import pytest

from dct.skill_render import _expand

SOURCES = Path(__file__).resolve().parents[1] / "dct/skill_sources"
WORKFLOWS = tuple(sorted(p.stem for p in (SOURCES / "workflows").glob("*.md")))


def workflow(name, host="codex", sources=SOURCES):
    return _expand((sources / "workflows" / f"{name}.md").read_text(), sources, host)


@pytest.mark.parametrize("host", ("claude", "codex", "fallback"))
@pytest.mark.parametrize("name", WORKFLOWS)
def test_domain_language_change_reaches_every_host_without_another_authored_copy(tmp_path, host, name):
    """A shared policy edit must reach every workflow rather than a host summary."""
    sources = tmp_path / "sources"
    shutil.copytree(SOURCES, sources)
    language = sources / "shared/language.md"
    language.write_text(language.read_text() + "\nKeep the newly approved language boundary.\n")
    assert "Keep the newly approved language boundary." in workflow(name, host, sources)


@pytest.mark.parametrize("name", ("commit", "handoff", "pickup", "release", "dct-init", "dct-import", "plan-resolve-uncertain"))
def test_codex_consent_never_requires_a_claude_or_plan_only_permission_tool(name):
    """An ineligible UI cannot turn a readable preview into permission."""
    text = workflow(name)
    assert "AskUserQuestion" not in text
    assert "Never use a Plan-only or optional-question-only tool for permission" in text
    assert "visible assistant text" in text
    assert "explicit reply" in text


def test_codex_init_repairs_only_codex_and_omits_claude_allowlist_writes():
    """Explicit setup on Codex must not mutate another client's integration."""
    text = workflow("dct-init")
    assert "dct doctor --client codex --json" in text
    assert "dct integrate repair codex" in text
    assert "dct integrate repair claude" not in text
    assert "permissions.allow" not in text
    assert "~/.claude/settings.json" not in text
    assert "No Q4 allowlist question on Codex" in text
    assert "Instruction-file conflicts are report-only" in text


def test_pickup_accepts_all_three_commands_but_status_and_quotes_do_not_consume():
    """Accepted AUTO scope cannot regress to the prior proposed-resume rule."""
    text = workflow("pickup")
    assert "All three commands have the same accepted consume behavior" in text
    assert "proposed refinement" not in text
    assert 'consume="false"' in text
    assert "merely quotes or mentions" in text
    assert "Never auto-pick among several handoffs" in text


def test_manual_and_batch_plan_classification_have_real_typed_writers():
    """No claimed native plan workflow may fall back to an absent write path."""
    text = workflow("plan-resolve-uncertain")
    assert "set_plan_section_type(section_id=<integer>" in text
    assert "resolve_uncertain_sections(plan_id=<integer>" in text
    assert "<section-ulid> <type> --project <slug> --json" in text
    assert "<plan-ulid> --project <slug> --json" in text
    for retired in ("if available", "if present", "claude --print", "Auto-classify all via Claude Haiku", "add_plan_section("):
        assert retired not in text
    assert "until explicitly revoked" in text
    assert "partial failure keep acknowledged ids" in text


def test_historical_import_preserves_history_without_a_release_workflow():
    """A history row cannot stamp unrelated unreleased entries or advance a version."""
    text = workflow("dct-import")
    section = text[text.index("### Format F"):text.index("### Format G")]
    assert "import_released_changelog(project=<slug>" in section
    assert 'released_at_source="import_time"' in section
    assert "neither current_version, unrelated unreleased entries nor release records" in section
    assert "Never use add→release stamping" in section
    assert "Per-row failure leaves earlier successful rows intact" in section
    assert "whole-file atomicity" in section
    assert "then `release_changelog" not in section


def test_cross_project_listing_enumerates_explicit_slugs_and_source_hotspots_require_full_rows():
    """Empty project from a registered checkout cannot accidentally narrow an all-project request."""
    text = workflow("track-list")
    assert "NOT cross-project" in text
    assert "list_items(project=<each slug>" in text
    assert "source_file`, absent from brief structured rows" in text
    assert 'format="dict", view="full"' in text
    assert "disclose when the result is capped" in text


def test_release_keeps_rare_refusal_and_recovery_keys_across_hosts():
    """A host conversion must retain every named refusal branch, not only the happy path."""
    source = (SOURCES / "workflows/release.md").read_text()
    keys = set(re.findall(r"`details\.[a-z_]+`", source))
    assert len(keys) >= 70
    for host in ("claude", "codex", "fallback"):
        rendered = workflow("release", host)
        assert keys <= set(re.findall(r"`details\.[a-z_]+`", rendered))
        for boundary in ("## Resume path", "## Legacy path", "The one automatic re-run", "resume.blocked", "pending_consent.adoptable", "consent_digest of the variant whose content the screen rendered"):
            assert boundary in rendered


@pytest.mark.parametrize("name", ("pickup", "handoff"))
def test_shared_read_schema_refusal_never_triggers_an_unrelated_writer(name):
    """An old-schema read is a visible migration decision, not hidden bootstrap."""
    text = workflow(name)
    assert "SchemaNotReady" in text
    assert "Propose `dct db upgrade` with explicit consent" in text
    assert "Never invoke an unrelated writer to trigger a hidden migration" in text


@pytest.mark.parametrize("host,prefix", [("claude", "/"), ("codex", "$"), ("fallback", "")])
def test_skill_invocation_uses_the_native_host_prefix_without_rewriting_cli_or_paths(host, prefix):
    """Corrections/resume use real native invocation; CLI commands stay literal."""
    release = workflow("release", host)
    listing = workflow("track-list", host)
    assert f"run {prefix}release again" in release
    assert f"`{prefix}track-list --all`" in listing
    assert "`dct release plan --json`" in release
    assert "docs/release-workflow.md" in release
    if host == "codex":
        assert "run /release again" not in release
        assert "`/track-list --all`" not in listing


@pytest.mark.parametrize("host", ("claude", "codex", "fallback"))
def test_triage_acceptance_keeps_saved_digest_proof_and_conditional_text_origin(host):
    """K45: a current URL cannot prove which proposals were actually displayed."""
    text = " ".join(workflow("triage", host).split())
    assert "never a read of the page's current URL as the only proof" in text
    assert "local file exactly as published in this session" in text
    assert "CLOSED.answers[K-n].origin" in text
    assert "only when the decision came from a prefilled paste" in text
    assert "later decisions without that origin reset it" in text


@pytest.mark.parametrize("host", ("claude", "codex", "fallback"))
@pytest.mark.parametrize("name", ("pickup", "handoff", "triage"))
def test_actor_identity_omits_unverified_project_and_time_without_auto_preflight(host, name):
    """Initial AUTO cannot invent a slug/time merely to satisfy an actor example."""
    text = " ".join(workflow(name, host).split())
    assert "Only the true client is mandatory" in text
    assert "prior successful DCT response or an explicit user-supplied slug" in text
    assert "never a directory, worktree or task-title guess" in text
    assert "an actual provided clock, known host time context or a time tool" in text
    assert "omit unknown components entirely" in text
    assert "no approximate time" in text
    assert "No mandatory extra lookup" in text
    if name in ("pickup", "handoff"):
        assert '<known_actor_identity>' in text
        assert not re.search(r'(?:created_by|consumed_by)="<client> @ <project_slug> <iso_utc>Z"', text)


def test_handoff_tool_descriptions_do_not_require_optional_actor_components():
    """Always-visible tool text must not reintroduce invented model/slug/clock facts."""
    from dct.mcp import server
    for tool in (server.create_handoff, server.consume_handoff, server.pickup_work, server.handoff_work):
        text = " ".join(tool.__doc__.split())
        assert "true client" in text
        assert "project suffix only" in text
        assert "UTC only" in text
        assert "omit unknown components" in text.lower()
        assert "model + timestamp" not in text


@pytest.mark.parametrize('host', ('claude', 'codex', 'fallback'))
def test_commit_without_cli_has_actionable_manual_candidates_and_preserves_guards(host):
    """Missing CLI must retrieve rows, not point back to its own generated section."""
    text = workflow('commit', host)
    assert 'list_changelog(project=<selected slug>, unreleased_only=true)' in text
    assert 'branch_source' in text and 'already_proven' in text
    assert 'git diff --cached' in text
    assert 'Never fall back to an' in text
    assert 'select by hand with the' not in text


@pytest.mark.parametrize('host', ('claude', 'codex', 'fallback'))
def test_plan_import_consent_maps_to_a_real_boolean(host):
    """A strategy label cannot become a bool API argument or authorize a provider."""
    text = workflow('dct-import', host)
    assert 'auto_resolve_uncertain=true' in text
    assert 'auto_resolve_uncertain=false' in text
    assert '<the approved configured strategy>' not in text
