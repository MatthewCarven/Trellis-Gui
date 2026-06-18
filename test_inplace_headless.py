"""Headless tests for the in-place editing variant — real DPG context, no
viewport. Drives the modal key flow (READY vs EDIT) directly."""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import pytest

from trellis import Workbook

from dpg_grid_inplace import InplaceGrid
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
    g = InplaceGrid(GridModel(sh))
    with dpg.window(tag="primary"):
        g.build("primary")
    return g, g.model, sh


def test_build_shows_values(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    assert dpg.get_value(g.cell_tag(2, 0)) == "15"  # cell shows the value
    assert g.editing is False


def test_nav_when_ready_moves_cursor(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_Down)
    assert m.cursor == (2, 0) and g.editing is False


def test_f2_begins_edit_and_shows_formula_source(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_Down)      # cursor on A3 (a formula)
    g._on_key(None, dpg.mvKey_F2)        # begin editing
    assert g.editing is True
    assert dpg.get_value(g.cell_tag(2, 0)) == "=A1+A2"  # source, not "15"


def test_type_to_edit_seeds_the_cell(ctx):
    g, m, sh = _grid([])
    g._on_key(None, dpg.mvKey_K)         # a printable from READY
    assert g.editing is True
    assert dpg.get_value(g.cell_tag(0, 0)) == "k"


def test_enter_commits_recalcs_and_moves_down(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_F2)        # edit A1 (cursor starts at A1)
    dpg.set_value(g.cell_tag(0, 0), "20")  # user types 20
    g._on_key(None, dpg.mvKey_Return)    # commit + move down
    assert g.editing is False
    assert sh["A1"].value == 20
    assert dpg.get_value(g.cell_tag(2, 0)) == "25"  # A3 recalc visible in the grid
    assert m.cursor == (1, 0)            # cursor moved down


def test_escape_cancels_without_committing(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "999")
    g._on_key(None, dpg.mvKey_Escape)
    assert g.editing is False
    assert dpg.get_value(g.cell_tag(0, 0)) == "10"  # reverted to the value
    assert sh["A1"].value == 10                     # never committed


def test_tab_commits_and_moves_right(ctx):
    g, m, sh = _grid([])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "hi")
    g._on_key(None, dpg.mvKey_Tab)
    assert sh["A1"].value == "hi" and m.cursor == (0, 1) and g.editing is False
