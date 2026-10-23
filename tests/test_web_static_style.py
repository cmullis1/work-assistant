"""The dark visual foundation in app.css: tokens, contrast, shared layout classes, wrapping, banners."""
from __future__ import annotations

import re

import pytest

from css_rules import STATIC, app_css, media_block, rules, top_level, values

SPEC_COLORS = ("#0b1020", "#11182d", "#18213b", "#121a30", "#233056", "#0f1730", "#e8ecf6", "#9aa6c4",
               "#ffffff", "#5eead4", "#fbbf24", "#c4b5fd", "#fb7185")


@pytest.fixture(scope="module")
def css() -> str:
    return app_css()


def luminance(hex_color: str) -> float:
    channels = [int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_dark_only_tokens(css):
    lowered = css.lower()
    for color in SPEC_COLORS:
        assert color in lowered, color
    assert "prefers-color-scheme" not in css
    assert "data-theme" not in css


def test_dark_palette_contrast():
    assert contrast("#e8ecf6", "#0b1020") >= 7
    assert contrast("#9aa6c4", "#18213b") >= 4.5
    for color in ("#5eead4", "#fbbf24", "#c4b5fd", "#fb7185"):
        assert contrast(color, "#18213b") >= 4.5, color


def test_layout_classes_exist(css):
    for selector in (".panel-row", ".cols-2", ".cols-4-lead", ".cols-1-2", ".cols-2-1", ".panel-label",
                     ".group-head", ".lead-num"):
        assert re.search(re.escape(selector) + r"(?![\w-])[^{}]*\{", css), selector
    narrow = media_block(css, "(max-width: 640px)")
    assert ".panel-row" in narrow


def test_long_text_wraps(css):
    for selector in (".panel", ".ev-text", ".det td.ev", ".comment-body"):
        bodies = rules(css, selector)
        assert any(re.search(r"overflow-wrap:\s*(anywhere|break-word)", b) for b in bodies), selector


def test_banner_colors(css):
    assert any("--failure" in body for body in rules(css, ".banner")), ".banner"
    assert any("--attention" in body for body in rules(css, ".banner.info")), ".banner.info"


def test_two_column_rows_stack_below_1024px(css):
    """Every fit check is done at 1024px and wider. Below that a half-width column is too narrow for its
    tables, so the two-column rows go full width, and the Tickets detail's scroll-into-view uses the same
    breakpoint. The lead tile row keeps its own step: two to a row below 900px, the lead tile across both, and so on
    a phone too (iPad and phone design §8b)."""
    wide = top_level(css)
    below = media_block(css, "(max-width: 1023px)")
    for row in (".panel-row.cols-2", ".panel-row.cols-1-2", ".panel-row.cols-2-1"):
        assert values(wide, row, "grid-template-columns") != ["minmax(0, 1fr)"], row + " is two columns at 1024px"
        assert values(below, row, "grid-template-columns") == ["minmax(0, 1fr)"], row + " stacks below 1024px"
    assert values(below, ".panel-row.cols-4-lead", "grid-template-columns") == []
    two_up = media_block(css, "(max-width: 900px)")
    narrow = media_block(css, "(max-width: 640px)")
    assert values(two_up, ".panel-row.cols-4-lead", "grid-template-columns") == ["repeat(2, minmax(0, 1fr))"]
    assert values(narrow, ".panel-row.cols-4-lead", "grid-template-columns") == ["repeat(2, minmax(0, 1fr))"]
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert re.search(r'var SINGLE_COLUMN = "\(max-width: 1023px\)"', js), "scroll-to-detail matches the stacking"


def test_no_parallel_token_names(css):
    """One name per role: the spec role names stay, and the old same-valued aliases are gone."""
    for alias in ("--crit", "--good", "--muted", "--ring", "--surface-1", "--crit-tint"):
        assert not re.search(re.escape(alias) + r"(?![\w-])", css), alias


def test_group_head_box_is_for_block_headers_only(css):
    """The label look applies to every group header, but padding, margin and radius go only on the block
    headers: on a table row they would break the stacked scorecard's grid rows at 640px and below."""
    for prop in ("padding", "margin", "border-radius"):
        assert values(css, ".group-head", prop) == [], prop
        assert values(css, "div.group-head", prop) and values(css, "h4.group-head", prop), prop
    assert values(css, ".group-head", "text-transform") == ["uppercase"]


def test_outcome_mark_stays_on_one_line(css):
    assert "nowrap" in values(css, ".det .ic", "white-space"), "N/A never wraps to N/ over A"


def test_page_is_wide(css):
    """Spec 4.1: on a screen about 2000px wide the page fills most of it, with a comfortable side margin."""
    wide = top_level(css)
    assert values(wide, ":root", "--page-max") == ["1760px"]
    assert values(wide, ":root", "--pad-page-x") == ["32px"], "the page's side padding"
    assert values(wide, ".app", "max-width") == ["var(--page-max)"]
    assert values(wide, ".app", "padding") == ["0 var(--pad-page-x) var(--pad-page)"]


def test_page_fills_the_width_beside_the_side_panel(css):
    """T-064: inside the shell the page column takes the whole width left of the side panel."""
    wide = top_level(css)
    assert values(wide, ":root", "--page-max-shell") == ["none"]
    assert values(wide, ".shell > .app", "max-width") == ["none"]
    assert values(wide, ".shell > .app > .inner", "max-width") == ["var(--page-max-shell)"]
