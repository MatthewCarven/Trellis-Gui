"""Headless tests for the hybrid (in-place + formula/status bar) candidate."""

from __future__ import annotations

import dearpygui.dearpygui as dpg
import pytest

import trellis_keymap as km
from trellis import Workbook, read_csv

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
    # Hand the workbook to the model so HybridGrid shares it across tabs
    # (cross-sheet refs + shared recalc), exactly as load_model does.
    g = HybridGrid(GridModel(sh, workbook=wb))
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


# ------------------------------------------------------------- save / undo
def test_save_to_writes_and_clears_dirty(ctx, tmp_path):
    g, m, sh = _grid([("A1", 2), ("A2", 3), ("A3", "=A1+A2")])
    m.dirty = True
    out = tmp_path / "hy.csv"
    g._save_to(str(out))
    assert out.exists() and m.dirty is False
    wb = read_csv(str(out), formulas=True)
    sh2 = wb[next(iter(wb))]
    assert sh2.get((2, 0)).formula == "=A1+A2"   # formulas round-trip
    assert dpg.get_value(g.SAVE) == "hy.csv"     # status shows the saved file


def test_ctrl_s_saves_to_remembered_path(ctx, tmp_path):
    out = tmp_path / "remembered.csv"
    g, m, sh = _grid([("A1", 1)])
    m.path = str(out)
    m.dirty = True
    g._save()                                    # path set -> writes directly, no dialog
    assert out.exists() and m.dirty is False


def test_undo_repaints_grid(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "20")
    g._on_key(None, dpg.mvKey_Return)            # commit: A1 = 20
    assert sh["A1"].value == 20
    m.apply_action(km.Undo())                    # (Ctrl+Z; modifier polling is headless)
    g.refresh()
    assert dpg.get_value(g.cell_tag(0, 0)) == "10"   # repaint reflects the undo
    assert sh["A1"].value == 10


def test_dirty_marker_in_status(ctx):
    g, m, sh = _grid([("A1", 1)])
    g._on_key(None, dpg.mvKey_F2)
    dpg.set_value(g.cell_tag(0, 0), "2")
    g._on_key(None, dpg.mvKey_Return)
    assert dpg.get_value(g.SAVE) == "(unsaved) *"


def test_paste_repaints_grid(ctx):
    g, m, sh = _grid([("A1", 5)])
    m.cursor = (0, 0)
    m.apply_action(km.Operate("copy"))       # (Ctrl+C; modifier polling is headless)
    m.cursor = (1, 1)
    m.apply_action(km.Operate("paste"))
    g.refresh()
    assert dpg.get_value(g.cell_tag(1, 1)) == "5"   # paste shows up in the grid
    assert sh["B2"].value == 5


def test_selection_highlights_range(ctx):
    g, m, sh = _grid([])
    m.selection = ((0, 0), (1, 1))
    m.cursor = (1, 1)
    g.refresh()
    for r, c in [(0, 0), (0, 1), (1, 0), (1, 1)]:
        assert g.cell_tag(r, c) in g._highlighted   # whole rect highlighted


def test_mouse_drag_builds_selection(ctx):
    g, m, sh = _grid([])
    g._begin_mouse_select((0, 0))                    # press
    assert g._selecting is True and m.selection is None and m.cursor == (0, 0)
    g._drag_select_to((1, 2))                        # drag to C2
    assert m.selection == ((0, 0), (1, 2)) and m.cursor == (1, 2)
    g._end_mouse_select()                            # release
    assert g._selecting is False


def test_begin_select_clears_prior_selection(ctx):
    g, m, sh = _grid([("A1", 1), ("B2", 2)])
    m.selection = ((0, 0), (1, 1)); m.anchor = (0, 0); m.cursor = (1, 1)
    g._begin_mouse_select((2, 2))                    # a fresh click drops the old selection
    assert m.selection is None and m.cursor == (2, 2) and m.anchor == (2, 2)


# ----------------------------------------------------- tabs / open / new
def test_new_adds_blank_tab(ctx):
    g, m, sh = _grid([("A1", 10), ("A2", 5)])
    assert len(g.models) == 1
    g._new()
    assert len(g.models) == 2 and g.active == 1
    assert g.model is not m
    assert g.model.sheet.used_range() is None         # the new tab is blank
    assert dpg.get_value(g.cell_tag(0, 0)) == ""


def test_switch_tab_changes_active_model(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._new()                                           # tab 2 (blank), now active
    assert g.active == 1
    g._switch_to(0)                                    # back to tab 1
    assert g.active == 0 and g.model is m
    assert dpg.get_value(g.cell_tag(0, 0)) == "10"     # tab 1's data is shown again


def test_open_path_opens_in_new_tab(ctx, tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("x,y\n1,=A1+1\n")
    g, m, sh = _grid([("A1", 10)])
    g._open_path(str(p))
    assert len(g.models) == 2 and g.active == 1
    assert g.model.path == str(p)
    assert dpg.get_value(g.cell_tag(0, 0)) == "x"
    assert g.model.sheet.get((1, 1)).formula == "=A1+1"   # formulas live on load


def test_close_tab_keeps_last(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._close_tab()                                     # only one tab -> no-op
    assert len(g.models) == 1


def test_close_tab_guard_blocks_then_discards(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._do_new()                                        # 2 tabs; tab 2 active
    g.model.commit(0, 0, "z")                          # dirty the active tab
    g._close_tab()                                     # dirty -> modal, no close yet
    assert len(g.models) == 2 and g._pending is not None
    g._confirm_discard()                               # discard -> close
    assert len(g.models) == 1


def test_close_tab_guard_cancel_keeps_tab(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._do_new()
    g.model.commit(0, 0, "z")
    g._close_tab()
    g._confirm_cancel()
    assert len(g.models) == 2 and g._pending is None



# --------------------------------------------- cross-sheet refs (shared book)
def test_tabs_share_one_workbook(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._new()
    assert g.model.sheet.name == "Sheet2"          # numbered in the shared book
    assert g.wb is m.workbook and g.model.workbook is g.wb
    assert "Sheet1" in g.wb and "Sheet2" in g.wb    # both sheets, one workbook


def test_cross_sheet_formula_resolves_across_tabs(ctx):
    g, m, sh1 = _grid([("A1", 10)])                 # Sheet1!A1 = 10
    g._new()                                        # Sheet2, active
    g.model.commit(0, 0, "=Sheet1!A1")              # Sheet2!A1 -> Sheet1!A1
    assert g.model.sheet.get((0, 0)).value == 10    # resolves via the shared book
    # editing the source on the other tab recalculates the dependent cell
    g._switch_to(0)
    g.model.commit(0, 0, "42")                       # Sheet1!A1 = 42
    g._switch_to(1)
    g.refresh()
    assert g.model.sheet.get((0, 0)).value == 42
    assert dpg.get_value(g.cell_tag(0, 0)) == "42"  # and the grid repaints it


def test_open_loads_into_shared_workbook_with_unique_name(ctx, tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("1\n")
    g, m, sh = _grid([("A1", 10)])
    g._open_path(str(p))
    assert g.model.sheet.name == "in"               # named from the file stem
    assert "in" in g.wb                              # joined the shared workbook
    g._switch_to(0)
    g.model.commit(0, 1, "=in!A1")                  # Sheet1 can reference it
    assert g.model.sheet.get((0, 1)).value == 1


def test_close_tab_removes_sheet_from_workbook(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._do_new()                                     # Sheet2 active
    assert "Sheet2" in g.wb
    g._do_close()                                   # close active (clean) tab
    assert "Sheet2" not in g.wb                      # sheet dropped from the book
    assert len(g.models) == 1


# ----------------------------------------------------- per-tab dirty markers
def test_per_tab_dirty_marker(ctx):
    g, m, sh = _grid([("A1", 1)])
    g._new()                                        # Sheet2, clean, active
    g.model.commit(0, 0, "x")                       # dirty Sheet2 only
    g.refresh()
    assert g._tab_label(1).endswith(" *")           # active tab marked dirty
    assert not g._tab_label(0).endswith(" *")       # other tab still clean
    # the live tab caption (not just the computed string) reflects it
    assert dpg.get_item_configuration(g._tab_tags[1])["label"].endswith(" *")
    assert dpg.get_item_configuration(g._tab_tags[0])["label"] == "Sheet1"


def test_dirty_marker_clears_on_save(ctx, tmp_path):
    g, m, sh = _grid([("A1", 1)])
    m.commit(0, 0, "2")
    g.refresh()
    assert g._tab_label(0).endswith(" *")
    g._save_to(str(tmp_path / "s.csv"))             # save clears dirty
    assert not g._tab_label(0).endswith(" *")
    # the tab shows the (stable) engine sheet name, not the save filename —
    # renaming on save would break cross-sheet refs pointing at this sheet.
    assert dpg.get_item_configuration(g._tab_tags[0])["label"] == "Sheet1"


# ----------------------------------------------------- copy/cut status feedback
def test_copy_cut_paste_status_feedback(ctx):
    g, m, sh = _grid([("A1", 5)])
    m.cursor = (0, 0)
    m.apply_action(km.Operate("copy")); g._feedback_for_operate("copy")
    assert dpg.get_value(g.MSG) == "Copied A1"          # coordinates, not RxC
    assert g._marquee == (((0, 0), (0, 0)), "copy")
    m.apply_action(km.Operate("cut")); g._feedback_for_operate("cut")
    assert dpg.get_value(g.MSG) == "Cut A1"
    assert g._marquee[1] == "cut"
    m.cursor = (2, 2)
    m.apply_action(km.Operate("paste")); g._feedback_for_operate("paste")
    assert dpg.get_value(g.MSG) == "Pasted C3"          # 1x1 stamp at the cursor
    assert g._marquee is None                            # paste clears the marquee


def test_status_message_flashes_then_expires(ctx):
    g, m, sh = _grid([("A1", 5)])
    g._set_msg("Copied A1")
    exp = g._msg_expiry
    g._tick_status(now=exp - 0.1)                    # still within the flash window
    assert dpg.get_value(g.MSG) == "Copied A1"
    g._tick_status(now=exp + 0.1)                    # past it -> auto-cleared
    assert dpg.get_value(g.MSG) == ""



# ------------------------------------------------ live engine -> grid repaint
def test_active_grid_repaints_live_on_engine_change(ctx):
    g, m, sh = _grid([("A1", 10)])
    sh["A1"] = 99                                   # mutate the engine directly (REPL-style)
    assert g._repaint_pending is True               # coalesced: marked dirty, not yet painted
    g._flush_repaint()                              # one frame tick
    assert dpg.get_value(g.cell_tag(0, 0)) == "99"  # grid repaints live on the next frame


def test_engine_events_coalesce_into_one_repaint(ctx):
    # A1=A2=...=A5, so one edit to A5 cascades upward: 1 cell:change + several
    # cell:recalc events. Coalescing collapses that burst into a single repaint
    # on the next frame instead of one full refresh() per event.
    g, m, sh = _grid([("A1", "=A2"), ("A2", "=A3"), ("A3", "=A4"),
                      ("A4", "=A5"), ("A5", 1)])
    n = {"c": 0}
    orig = g.refresh

    def counting_refresh():
        n["c"] += 1
        orig()

    g.refresh = counting_refresh
    sh["A5"] = 99                                   # one edit -> a cascade of engine events
    assert n["c"] == 0                              # no synchronous per-event repaint
    assert g._repaint_pending is True               # just the dirty flag
    g._flush_repaint()                              # one frame tick
    assert n["c"] == 1                              # exactly one repaint for the whole cascade
    assert g._repaint_pending is False
    assert dpg.get_value(g.cell_tag(0, 0)) == "99"  # propagated value is visible
    g._flush_repaint()                              # nothing pending -> no extra repaint
    assert n["c"] == 1


def test_cross_sheet_dependent_repaints_live_when_its_sheet_active(ctx):
    g, m, sh1 = _grid([("A1", 10)])
    g._new()                                        # Sheet2 active
    g.model.commit(0, 0, "=Sheet1!A1")              # Sheet2!A1 -> 10
    g._flush_repaint()
    assert dpg.get_value(g.cell_tag(0, 0)) == "10"
    # change Sheet1!A1 while Sheet2 is the visible tab; the recalc cascade lands
    # on Sheet2!A1, which is on screen -> it repaints live (next frame), no tab switch
    g.wb["Sheet1"]["A1"] = 42
    g._flush_repaint()
    assert dpg.get_value(g.cell_tag(0, 0)) == "42"


def test_live_repaint_skips_while_editing(ctx):
    g, m, sh = _grid([("A1", 10)])
    g._on_key(None, dpg.mvKey_F2)                   # begin in-place edit on A1
    dpg.set_value(g.cell_tag(0, 0), "in-progress")
    sh["B1"] = 7                                    # engine change while editing
    assert g.editing is True
    assert dpg.get_value(g.cell_tag(0, 0)) == "in-progress"   # editor not clobbered


def test_inactive_sheet_change_does_not_touch_active_grid(ctx):
    g, m, sh1 = _grid([("A1", 10)])                 # Sheet1 (will become inactive)
    g._new()                                        # Sheet2 active, blank
    g.wb["Sheet1"]["A1"] = 5                        # change the hidden sheet
    assert dpg.get_value(g.cell_tag(0, 0)) == ""    # active (Sheet2) grid untouched
    g._switch_to(0)
    assert dpg.get_value(g.cell_tag(0, 0)) == "5"   # switching shows the fresh value



# --------------------------------------------- clipboard marquee + Escape-cancel
def test_marquee_paints_copied_source(ctx):
    g, m, sh = _grid([("A1", 1), ("A2", 2)])
    m.selection = ((0, 0), (1, 0)); m.cursor = (1, 0)
    m.apply_action(km.Operate("copy")); g._feedback_for_operate("copy")
    g.refresh()
    assert g._marquee == (((0, 0), (1, 0)), "copy")
    assert dpg.get_value(g.MSG) == "Copied A1:A2"
    for r in (0, 1):                                 # both source cells get painted
        assert g.cell_tag(r, 0) in g._highlighted


def test_cut_marquee_uses_cut_mode(ctx):
    g, m, sh = _grid([("A1", 1)])
    m.cursor = (0, 0)
    m.apply_action(km.Operate("cut")); g._feedback_for_operate("cut")
    assert g._marquee == (((0, 0), (0, 0)), "cut")


def test_escape_cancels_clipboard(ctx):
    g, m, sh = _grid([("A1", 1)])
    m.cursor = (0, 0)
    m.apply_action(km.Operate("copy")); g._feedback_for_operate("copy")
    assert m.clipboard is not None and g._marquee is not None
    g._on_key(None, dpg.mvKey_Escape)               # Escape, not editing
    assert m.clipboard is None                        # clipboard dropped
    assert g._marquee is None                         # marquee gone
    assert dpg.get_value(g.MSG) == ""


def test_paste_clears_marquee(ctx):
    g, m, sh = _grid([("A1", 5)])
    m.cursor = (0, 0)
    m.apply_action(km.Operate("copy")); g._feedback_for_operate("copy")
    assert g._marquee is not None
    m.cursor = (1, 1)
    m.apply_action(km.Operate("paste")); g._feedback_for_operate("paste")
    assert g._marquee is None



# ------------------------------------------------ Shift+drag rectangle selection
def test_shift_drag_selects_rectangle(ctx):
    g, m, sh = _grid([])
    g._begin_shift_select((0, 0))                   # Shift+press on A1
    assert g._shift_select is True
    assert m.selection == ((0, 0), (0, 0)) and m.cursor == (0, 0)
    g._shift_drag_to((2, 1))                         # drag to B3
    assert m.selection == ((0, 0), (2, 1)) and m.cursor == (2, 1)
    g._end_shift_select()
    assert g._shift_select is False


def test_shift_drag_normalizes_backwards(ctx):
    g, m, sh = _grid([])
    m.anchor = (2, 2); m.cursor = (2, 2)            # a prior plain click anchored at C3
    g._begin_shift_select((2, 2))                   # Shift extends from the anchor
    g._shift_drag_to((0, 0))                         # drag up-left to A1
    assert m.selection == ((0, 0), (2, 2))          # normalised rectangle
    g._end_shift_select()


def test_shift_focus_guard_keeps_selection(ctx):
    g, m, sh = _grid([("A1", 1)])
    g._begin_shift_select((0, 0))
    g._shift_drag_to((1, 1))
    g._on_cell_focus(None, None, (0, 0))            # a Shift-press focus mid-drag
    assert m.selection == ((0, 0), (1, 1))          # selection survives (not cleared)



# ---------------------------------------------- Shift+click range select (reliable)
def test_shift_click_extends_selection(ctx):
    g, m, sh = _grid([])
    g._focus_cell((0, 0), extend=False)             # plain click sets the anchor
    assert m.cursor == (0, 0) and m.selection is None
    g._focus_cell((2, 3), extend=True)              # shift-click extends to D3
    assert m.selection == ((0, 0), (2, 3)) and m.cursor == (2, 3)


def test_plain_click_clears_selection(ctx):
    g, m, sh = _grid([])
    m.selection = ((0, 0), (1, 1)); m.anchor = (0, 0); m.cursor = (1, 1)
    g._focus_cell((3, 3), extend=False)             # a fresh plain click drops it
    assert m.selection is None and m.cursor == (3, 3) and m.anchor == (3, 3)
