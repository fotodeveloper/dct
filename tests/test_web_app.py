"""Task 3 — Flask skeleton, serializers, read-only boot contract."""

from datetime import datetime

import pytest

from dct.web.app import create_app
from dct.web.serializers import parse_tags, render_markdown, to_iso


@pytest.fixture
def client(engine, seed_project):
    app = create_app(engine=engine, read_only=True)
    app.config.update(TESTING=True)
    return app.test_client()


# ── app factory ────────────────────────────────────────────────────────────

def test_create_app_returns_flask(engine):
    import flask

    app = create_app(engine=engine, read_only=True)
    assert isinstance(app, flask.Flask)
    assert app.config["DCT_ENGINE"] is engine
    assert app.config["DCT_READ_ONLY"] is True


def test_healthz_returns_200_json(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json()["status"] == "ok"


def test_create_app_does_not_bootstrap_when_engine_given(engine, monkeypatch):
    """Passing an engine must NOT trigger bootstrap_engine (hence no create_tables)."""
    import dct.web.app as appmod

    calls = []
    monkeypatch.setattr(appmod, "bootstrap_engine", lambda **kw: calls.append(kw) or engine)
    create_app(engine=engine, read_only=True)
    assert calls == []


def test_create_app_bootstraps_read_only_when_no_engine(engine, monkeypatch):
    """No engine → bootstrap_engine(create=False): the web layer never creates tables."""
    import dct.web.app as appmod

    seen = []

    def fake_bootstrap(create=True):
        seen.append(create)
        return engine

    monkeypatch.setattr(appmod, "bootstrap_engine", fake_bootstrap)
    create_app(engine=None, read_only=True)
    assert seen == [False]


def test_current_engine_reads_app_config(engine):
    from dct.web.app import current_engine

    app = create_app(engine=engine, read_only=True)
    with app.app_context():
        assert current_engine() is engine


# ── serializers ────────────────────────────────────────────────────────────

def test_to_iso_passthrough_none():
    assert to_iso(None) is None


def test_to_iso_formats_datetime():
    # Suppression rationale: the naive datetime IS the subject — sqlite hands
    # the web layer naive datetimes and to_iso must pass them through unchanged.
    assert to_iso(datetime(2026, 6, 27, 12, 0, 0)) == "2026-06-27T12:00:00"  # noqa: DTZ001


def test_parse_tags_json_text():
    assert parse_tags('["a", "b"]') == ["a", "b"]


def test_parse_tags_none():
    assert parse_tags(None) == []


def test_parse_tags_list_passthrough():
    assert parse_tags(["x", "y"]) == ["x", "y"]


def test_parse_tags_plain_string_is_single_tag():
    assert parse_tags("solo") == ["solo"]


def test_parse_tags_empty_string():
    assert parse_tags("") == []


def test_render_markdown_escapes_raw_script():
    out = str(render_markdown("<script>alert(1)</script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_markdown_neutralizes_javascript_links():
    # markdown link syntax with a dangerous scheme must not survive as a live href.
    for src in (
        "[click](javascript:alert(1))",
        "[x](vbscript:foo)",
        "![img](data:text/html;base64,PHNjcmlwdD4=)",
    ):
        out = str(render_markdown(src))
        assert "javascript:" not in out
        assert "vbscript:" not in out
        assert "data:text/html" not in out
        assert 'href="#"' in out or 'src="#"' in out


def test_render_markdown_keeps_safe_links():
    out = str(render_markdown("[ok](https://example.com)"))
    assert 'href="https://example.com"' in out


def test_render_markdown_paragraphizes_long_wall():
    # A long single-paragraph block (no newlines) must be split into multiple
    # <p> so it reads as a column, not a wall.
    wall = "This is sentence number one here. " * 20
    out = str(render_markdown(wall.strip()))
    assert out.count("<p>") >= 3


def test_render_markdown_leaves_structured_text_alone():
    # Text that already has paragraph breaks + markdown structure is untouched.
    structured = "Intro paragraph here.\n\n## Heading\n\n- bullet one\n- bullet two"
    out = str(render_markdown(structured))
    assert "<h2>" in out and "<ul>" in out


def test_render_markdown_short_text_not_split():
    out = str(render_markdown("Short. Text."))
    assert out.count("<p>") == 1


def test_render_markdown_still_renders_emphasis():
    out = str(render_markdown("**bold**"))
    assert "<strong>bold</strong>" in out


# ── jinja filters (create_app is the single owner) ───────────────────────────

def test_create_app_registers_all_three_filters(engine):
    app = create_app(engine=engine, read_only=True)
    for name in ("render_markdown", "to_iso", "parse_tags"):
        assert name in app.jinja_env.filters


def test_render_markdown_filter_usable_in_template(engine):
    app = create_app(engine=engine, read_only=True)
    with app.app_context():
        out = app.jinja_env.from_string(
            "{{ '**bold**' | render_markdown }}"
        ).render()
    assert "<strong>bold</strong>" in out


def test_to_iso_and_parse_tags_filters_usable_in_template(engine):
    app = create_app(engine=engine, read_only=True)
    with app.app_context():
        out = app.jinja_env.from_string(
            "{{ '[\"a\", \"b\"]' | parse_tags | join(',') }}"
        ).render()
    assert out == "a,b"


# ── base.html references vendored static (no network) ────────────────────────

def test_base_html_references_vendored_htmx_and_static():
    """The canonical layout must reference the vendored htmx + app.css/app.js.

    Pure file read — no network, no rendering of url_for needed.
    """
    from pathlib import Path

    import dct.web as webpkg

    base = Path(webpkg.__file__).parent / "templates" / "base.html"
    text = base.read_text(encoding="utf-8")
    assert "vendor/htmx.min.js" in text
    assert "app.css" in text
    assert "app.js" in text
    # the pinned SSE refresh wiring
    assert 'new EventSource("/events")' in text
    assert 'id="main"' in text


def test_vendored_htmx_file_exists_and_nonempty():
    """htmx is vendored (committed), not fetched at runtime."""
    from pathlib import Path

    import dct.web as webpkg

    htmx = Path(webpkg.__file__).parent / "static" / "vendor" / "htmx.min.js"
    assert htmx.exists(), "htmx.min.js must be vendored under static/vendor/"
    assert htmx.stat().st_size > 0
