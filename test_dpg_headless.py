"""Headless DearPyGui tests — a real DPG context, no viewport/window shown.

DPG builds its item tree and runs get/set without a GPU window, so we can verify
the construction code and drive the callbacks directly. What this CAN'T cover —
live event dispatch and rendering — is left to a first run on Matthew's machine
(see README). Together with test_grid_model.py this exercises the whole spike
short of pixels.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import pytest

from trellis import Workbook

from dpg_grid import DpgGrid, col_label, keypress_from_code
from grid_model import GridModel


@pytest.fixture
def ctx():
    dpg.create_context()
    yield
    dpg.destroy_context()


def _grid(cells):
    wb = Workbook()
    sh = wb.add_sheet("Sheet1")
    for a1, v in cells:
        sh[a1] = v
    g = DpgGrid(GridModel(sh))
    with dpg.window(tag="primary"):
        g.build("primary")
    return g, g.model, sh


def test_col_label():
    # pure helper, no context needed
    assert col_label(0) == "A" and col_label(25) == "Z" and col_label(26) == "AA"


def test_build_shows_values(ctx):
    g, m, sh = _grid([("A1", "name"), ("B1", 42)])
    assert dpg.get_value(g.cell_tag(0, 0)) == "name"
    assert dpg.get_value(g.cell_tag(0, 1)) == "42"
    assert dpg.get_value(g.CURSOR_LABEL) == "A1"
    assert dpg.does_item_exist(g.BAR)


def test_grid_shows_value_not_formula(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    assert dpg.get_value(g.cell_tag(2, 0)) == "15"  # computed value in the cell


def test_keymap_nav_moves_cursor_and_updates_bar(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_Down)
    assert m.cursor == (2, 0)
    assert dpg.get_value(g.CURSOR_LABEL) == "A3"
    assert dpg.get_value(g.BAR) == "=A1+A2"  # bar prefills the formula source


def test_bar_commit_recalcs_and_repaints_grid(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    dpg.set_value(g.BAR, "20")          # edit A1 (cursor starts at A1)
    g._on_bar_enter(None, None)
    assert dpg.get_value(g.cell_tag(2, 0)) == "25"  # A3 recalc visible in the grid
    assert m.cursor == (1, 0)                       # commit moved the cursor down


def test_type_to_edit_seeds_the_bar(ctx):
    g, m, sh = _grid([])
    g._on_key(None, dpg.mvKey_K)  # a printable -> BeginEdit(seed="k")
    assert dpg.get_value(g.BAR) == "k"


def test_window_grows_to_cover_new_far_cell(ctx):
    g, m, sh = _grid([])
    assert not dpg.does_item_exist(g.cell_tag(29, 7))
    m.commit(29, 7, "x")     # row 29, col 7 — past the 12x6 minimum (via the model)
    g.refresh()
    assert dpg.does_item_exist(g.cell_tag(29, 7))
    assert dpg.get_value(g.cell_tag(29, 7)) == "x"


def test_load_model_reads_csv_with_live_formulas(tmp_path):
    # the file-loading glue (no DPG context needed)
    from dpg_grid import load_model
    p = tmp_path / "d.csv"
    p.write_text("Item,Qty,Price,Total\nApples,3,2,=B2*C2\n", encoding="utf-8")
    m = load_model([str(p)])
    assert m.display(1, 3) == "6"   # D2 = B2*C2 = 3*2
    assert m.display(0, 0) == "Item"


def test_load_model_empty_when_no_file():
    from dpg_grid import load_model
    m = load_model([])
    assert m.display(0, 0) == ""    # blank Sheet1
