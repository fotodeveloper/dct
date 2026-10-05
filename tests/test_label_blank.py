"""The shared blank-label rule (K-64).

A label is BLANK when it consists only of whitespace and Unicode format
characters (category Cf: zero-width space, joiners, BOM, bidi marks).
- Writes (normalize_short_title): the raw length is checked FIRST, then a
  blank label becomes NULL; a visible label keeps its inner Cf characters
  (only surrounding whitespace is stripped — no global Cf removal).
- Visible labels (item_label, CLI `show` / `list`, web list views) fall back
  for a blank STORED label (one written before the rule); raw API data keeps
  the historical value — no mass rewrite.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from conftest import run_cli

from dct.core.items import create_item, get_item, normalize_short_title
from dct.core.text import is_blank_label, item_label, visible_label
from dct.db import items

ZWSP, ZWJ, BOM, LRM = "\u200b", "\u200d", "\ufeff", "\u200e"

BLANK = ["", " ", "\t\n", ZWSP, ZWSP * 3, f" {BOM} ", f"{LRM}{ZWJ}\u00a0"]


@pytest.mark.parametrize("text", BLANK)
def test_whitespace_and_format_characters_only_is_blank(text):
    assert is_blank_label(text)
    assert visible_label(text) is None


@pytest.mark.parametrize("text", ["a", f"{ZWSP}a", f"Fix{ZWJ}er", "  x  "])
def test_any_visible_character_is_not_blank(text):
    assert not is_blank_label(text)
    assert visible_label(text) == text


def test_none_is_blank():
    assert is_blank_label(None) and visible_label(None) is None


@pytest.mark.parametrize("raw", [ZWSP, f" {BOM}{ZWSP} "])
def test_a_blank_label_is_stored_as_null(raw):
    assert normalize_short_title(raw) is None


def test_inner_format_characters_of_a_visible_label_are_kept():
    assert normalize_short_title(f"  Fix{ZWJ}er  ") == f"Fix{ZWJ}er"


def test_the_raw_limit_applies_before_normalization():
    with pytest.raises(ValueError, match="short_title too long"):
        normalize_short_title(ZWSP * 51)


@pytest.mark.parametrize("stored", ["   ", ZWSP, f"{BOM} "])
def test_item_label_falls_back_to_the_title_for_a_blank_stored_label(stored):
    assert item_label(stored, "The title") == "The title"


# ── visible surfaces over a legacy blank label (raw data untouched) ──────────


@pytest.fixture
def legacy_blank(cli_engine):
    iid = create_item(cli_engine, "proj-a", "todo", "Legacy item")["id"]
    with cli_engine.begin() as conn:
        conn.execute(items.update().where(items.c.id == iid).values(short_title=ZWSP + " "))
    return cli_engine, iid


def test_cli_show_hides_a_blank_stored_label(legacy_blank, monkeypatch, capsys):
    engine, iid = legacy_blank
    run_cli(monkeypatch, ["show", str(iid)])
    assert "Short title" not in capsys.readouterr().out
    assert get_item(engine, iid)["short_title"] == ZWSP + " "   # raw value kept


def test_cli_list_renders_a_blank_stored_label_as_empty(legacy_blank, monkeypatch, capsys):
    _, iid = legacy_blank
    run_cli(monkeypatch, ["list", "proj-a"])
    row = next(line for line in capsys.readouterr().out.splitlines()
               if line.lstrip().startswith(str(iid)))
    assert ZWSP not in row


def test_web_item_views_hide_a_blank_stored_label(legacy_blank):
    from dct.web.app import create_app

    engine, iid = legacy_blank
    app = create_app(engine, read_only=True)
    client = app.test_client()
    for url in (f"/p/proj-a/items/{iid}", "/p/proj-a/items"):
        page = client.get(url)
        assert page.status_code == 200, url
        assert 'class="short-title"' not in page.get_data(as_text=True), url


def test_no_mass_rewrite_of_history(legacy_blank):
    engine, iid = legacy_blank
    with engine.begin() as conn:
        assert conn.execute(sa.select(items.c.short_title)
                            .where(items.c.id == iid)).scalar() == ZWSP + " "
