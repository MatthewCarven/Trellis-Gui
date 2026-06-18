"""Headless tests for the spike's model — no DearPyGui, no display.

Proves the reusable half: windowing/bounds, the commit policy, recalc
propagation through the engine, and — the point of the whole keymap extraction
— the SAME `trellis_keymap.ExcelKeymap` driving this GUI model.
"""

from __future__ import annotations

import trellis_keymap as km
from trellis import Workbook

from grid_model import GridModel


def model(min_rows=12, min_cols=6):
    wb = Workbook()
    sh = wb.add_sheet("Sheet1")
    return GridModel(sh, min_rows=min_rows, min_cols=min_cols), sh


def feed(m, keyname, char=None):
    """Route a key through ExcelKeymap exactly as the GUI shell will."""
    action = km.ExcelKeymap().handle(km.KeyPress.parse(keyname, char=char), m.key_context())
    return m.apply_action(action)


# --------------------------------------------------------------- windowing
def test_empty_sheet_has_minimum_window():
    m, _ = model()
    assert (m.ul, m.lr) == ((0, 0), (11, 5))  # 12x6 minimum
    assert m.window.nrows == 12 and m.window.ncols == 6


def test_window_grows_to_cover_data():
    m, sh = model()
    sh["I21"] = 1  # row 20, col 8 — past the minimum
    m._recompute_window()
    assert m.in_window(20, 8)
    assert m.lr == (20, 8)


def test_in_window_is_bounds_only():
    m, _ = model()
    assert m.in_window(0, 0) and m.in_window(11, 5)
    assert not m.in_window(12, 0) and not m.in_window(0, 6)


# ------------------------------------------------------------ commit policy
def test_commit_infers_number():
    m, sh = model()
    m.commit(0, 0, "42")
    assert sh["A1"].value == 42 and isinstance(sh["A1"].value, int)


def test_commit_text_stays_string():
    m, sh = model()
    m.commit(0, 0, "hello")
    assert sh["A1"].value == "hello"


def test_commit_empty_deletes():
    m, sh = model()
    m.commit(0, 0, "x")
    m.commit(0, 0, "")
    assert sh.get((0, 0)).is_empty()


def test_commit_formula_and_recalc_propagates():
    m, sh = model()
    m.commit(0, 0, "10")          # A1
    m.commit(1, 0, "5")           # A2
    m.commit(2, 0, "=A1+A2")      # A3
    assert m.display(2, 0) == "15"
    m.commit(0, 0, "20")          # change A1 -> A3 recomputes (20+5)
    assert m.display(2, 0) == "25"


def test_display_shows_error_code():
    m, _ = model()
    m.commit(0, 0, "=1/0")
    assert m.display(0, 0) == "#DIV/0!"


def test_edit_text_prefills_formula_then_value():
    m, _ = model()
    m.commit(0, 0, "=1+2")
    assert m.edit_text(0, 0) == "=1+2"   # formula source, not "3"
    m.commit(1, 0, "7")
    assert m.edit_text(1, 0) == "7"


# ----------------------------------------- ExcelKeymap drives the GUI model
def test_arrows_move_cursor():
    m, _ = model()
    feed(m, "down")
    feed(m, "right")
    assert m.cursor == (1, 1)


def test_shift_arrow_extends_selection():
    m, _ = model()
    feed(m, "down")
    feed(m, "shift+right")
    feed(m, "shift+down")
    assert m.selection == ((1, 0), (2, 1))


def test_ctrl_home_jumps_to_a1():
    m, _ = model()
    feed(m, "down"); feed(m, "down"); feed(m, "right")
    feed(m, "ctrl+home")
    assert m.cursor == (0, 0)


def test_ctrl_a_selects_used_range():
    m, sh = model()
    sh["A1"] = 1
    sh["B2"] = 2
    feed(m, "ctrl+a")
    assert m.selection == ((0, 0), (1, 1))


def test_delete_clears_cursor_cell():
    m, sh = model()
    m.commit(0, 0, "doomed")
    feed(m, "delete")
    assert sh.get((0, 0)).is_empty()


def test_printable_returns_edit_intent():
    m, _ = model()
    feed(m, "down")  # cursor (1,0)
    intent = feed(m, "k", char="k")
    assert intent == ("edit", 1, 0, "k")
