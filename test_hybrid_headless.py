"""Headless tests for the hybrid (in-place + formula/status bar) candidate."""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import pytest

from trellis import Workbook

from dpg_grid_hybrid import HybridGrid
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
    g = HybridGrid(GridModel(sh))
    with dpg.window(tag="primary"):
        g.build("primary")
    return g, g.model, sh


def test_build_shows_values_and_bar(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    assert dpg.get_value(g.cell_tag(2, 0)) == "15"
    assert dpg.get_value(g.ADDR) == "A1"
    assert dpg.get_value(g.MODE) == "[READY]"


def test_nav_updates_address_and_formula_bar(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_Down)              # cursor -> A3 (a formula)
    assert dpg.get_value(g.ADDR) == "A3"
    assert dpg.get_value(g.BAR) == "=A1+A2"      # bar shows the cursor cell's source


def test_f2_edits_in_place_and_sets_edit_mode(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_Down)
    g._on_key(None, dpg.mvKey_F2)
    assert g.editing is True
    assert dpg.get_value(g.MODE) == "[EDIT]"
    assert dpg.get_value(g.cell_tag(2, 0)) == "=A1+A2"   # cell shows source in place


def test_type_to_edit_seeds_cell_and_bar(ctx):
    g, m, sh = _grid([])
    g._on_key(None, dpg.mvKey_K)
    assert g.editing is True
    assert dpg.get_value(g.cell_tag(0, 0)) == "k"
    assert dpg.get_value(g.BAR) == "k"           # bar mirrors the seed


def test_enter_commits_recalcs_and_returns_to_ready(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5), ("A3", "=A1+A2")])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "20")
    g._on_key(None, dpg.mvKey_Return)
    assert g.editing is False and dpg.get_value(g.MODE) == "[READY]"
    assert sh["A1"].value == 20
    assert dpg.get_value(g.cell_tag(2, 0)) == "25"   # recalc visible in the grid
    assert m.cursor == (1, 0)


def test_escape_cancels(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "999")
    g._on_key(None, dpg.mvKey_Escape)
    assert g.editing is False
    assert dpg.get_value(g.cell_tag(0, 0)) == "10"
    assert sh["A1"].value == 10
