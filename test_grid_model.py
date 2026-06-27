"""Headless tests for the spike's model — no DearPyGui, no display.

Proves the reusable half: windowing/bounds, the commit policy, recalc
propagation through the engine, and — the point of the whole keymap extraction
— the SAME `trellis_keymap.ExcelKeymap` driving this GUI model.
"""

from __future__ import annotations

import pytest

import trellis_keymap as km
from trellis import Workbook, read_csv

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


# ----------------------------------------------------- undo / redo (Part 7)
def test_undo_redo_through_keymap():
    m, sh = model()
    m.commit(0, 0, "10")
    m.commit(0, 0, "20")
    feed(m, "ctrl+z")             # ExcelKeymap maps Ctrl+Z -> Undo()
    assert sh["A1"].value == 10
    feed(m, "ctrl+y")             # Ctrl+Y -> Redo()
    assert sh["A1"].value == 20


def test_undo_restores_formula_and_recalc():
    m, sh = model()
    m.commit(0, 0, "2")           # A1
    m.commit(1, 0, "3")           # A2
    m.commit(2, 0, "=A1+A2")      # A3 == 5
    m.commit(2, 0, "999")         # clobber A3
    assert m.display(2, 0) == "999"
    feed(m, "ctrl+z")             # undo the clobber -> formula returns, recalcs
    assert sh.get((2, 0)).formula == "=A1+A2"
    assert m.display(2, 0) == "5"


def test_undo_log_attached_to_sheet_meta():
    m, sh = model()
    assert sh.meta["undo"] is m.undo_log


# ------------------------------------------------------------- save (CSV)
def test_dirty_flag_lifecycle(tmp_path):
    m, _ = model()
    assert m.dirty is False
    m.commit(0, 0, "x")
    assert m.dirty is True
    m.save(str(tmp_path / "out.csv"))
    assert m.dirty is False


def test_save_round_trips_formulas(tmp_path):
    m, _ = model()
    m.commit(0, 0, "2")           # A1
    m.commit(1, 0, "3")           # A2
    m.commit(2, 0, "=A1+A2")      # A3 == 5
    out = tmp_path / "rt.csv"
    assert m.save(str(out)) == str(out)
    wb = read_csv(str(out), formulas=True)
    sh2 = wb[next(iter(wb))]
    assert sh2.get((2, 0)).formula == "=A1+A2"   # formula survived the round-trip
    assert sh2.get((2, 0)).value == 5


def test_save_uses_remembered_path(tmp_path):
    out = tmp_path / "remembered.csv"
    m, _ = model()
    m.path = str(out)
    m.commit(0, 0, "hi")
    m.save()                      # no arg -> falls back to self.path
    assert out.exists()


def test_save_without_path_raises():
    m, _ = model()
    with pytest.raises(ValueError):
        m.save()


# ------------------------------------------------ copy / cut / paste (Part 6)
def test_copy_paste_single_value():
    m, sh = model()
    m.commit(0, 0, "5")                      # A1 = 5
    m.cursor = (0, 0)
    feed(m, "ctrl+c")                        # ExcelKeymap -> Operate("copy")
    m.cursor = (1, 1)                        # move to B2
    feed(m, "ctrl+v")                        # Operate("paste")
    assert sh["B2"].value == 5


def test_paste_shifts_formula():
    m, sh = model()
    m.commit(0, 0, "2")                      # A1
    m.commit(1, 0, "3")                      # A2
    m.commit(2, 0, "=A1+A2")                 # A3 = 5
    m.commit(0, 1, "10")                     # B1
    m.commit(1, 1, "20")                     # B2
    m.cursor = (2, 0)
    feed(m, "ctrl+c")                        # copy A3
    m.cursor = (2, 1)
    feed(m, "ctrl+v")                        # paste into B3 -> refs shift one col
    assert sh["B3"].formula == "=B1+B2"
    assert sh["B3"].value == 30


def test_cut_moves_and_clears_source():
    m, sh = model()
    m.commit(0, 0, "99")                     # A1
    m.cursor = (0, 0)
    feed(m, "ctrl+x")                        # cut A1
    m.cursor = (2, 2)
    feed(m, "ctrl+v")                        # paste into C3
    assert sh["C3"].value == 99
    assert sh.get((0, 0)).is_empty()         # source cleared by the move


def test_cut_demotes_to_copy_after_paste():
    m, sh = model()
    m.commit(0, 0, "7")
    m.cursor = (0, 0)
    feed(m, "ctrl+x")
    m.cursor = (1, 0)
    feed(m, "ctrl+v")                        # move 7 to A2; clipboard demotes to copy
    m.cursor = (2, 0)
    feed(m, "ctrl+v")                        # re-paste re-stamps a copy (no clear)
    assert sh["A2"].value == 7 and sh["A3"].value == 7


def test_copy_paste_block_range():
    m, sh = model()
    m.commit(0, 0, "1")
    m.commit(0, 1, "2")                      # A1, B1
    m.selection = ((0, 0), (0, 1)); m.anchor = (0, 0); m.cursor = (0, 1)
    feed(m, "ctrl+c")                        # copy A1:B1
    m.selection = None; m.cursor = (2, 0); m.anchor = (2, 0)
    feed(m, "ctrl+v")                        # paste block at A3
    assert sh["A3"].value == 1 and sh["B3"].value == 2


def test_paste_with_empty_clipboard_is_noop():
    m, sh = model()
    m.commit(0, 0, "1")
    m.cursor = (1, 0)
    feed(m, "ctrl+v")                        # nothing copied yet
    assert sh.get((1, 0)).is_empty()


# --------------------------------------------------------------- fill (Ctrl+D/R)
# Ctrl+D/Ctrl+R go keymap -> km.Fill -> apply_action._fill, the path that was
# silently dropped before the Fill handler existed.


def test_fill_down_multilane_shifts_formulas():
    m, sh = model()
    m.commit(0, 0, "10")                     # A1
    m.commit(0, 1, "=A1*2")                  # B1 -> 20
    m.selection = ((0, 0), (3, 1)); m.anchor = (0, 0); m.cursor = (3, 1)  # A1:B4
    feed(m, "ctrl+d")                        # fill down from row 1
    assert sh["A4"].value == 10              # value copies
    assert sh["B2"].formula == "=A2*2"       # formula shifts per target lane
    assert sh["B4"].formula == "=A4*2"


def test_fill_right_shifts_formulas():
    m, sh = model()
    m.commit(0, 0, "5")                      # A1
    m.commit(1, 0, "=A1+1")                  # A2 -> 6
    m.selection = ((0, 0), (1, 2)); m.anchor = (0, 0); m.cursor = (1, 2)  # A1:C2
    feed(m, "ctrl+r")                        # fill right from column A
    assert sh["C1"].value == 5
    assert sh["C2"].formula == "=C1+1"


def test_fill_down_no_selection_uses_neighbour_above():
    m, sh = model()
    m.commit(0, 0, "42")                     # A1
    m.cursor = (1, 0); m.anchor = (1, 0)     # cursor on A2, no selection
    feed(m, "ctrl+d")                        # fills A2 from A1 (neighbour above)
    assert sh["A2"].value == 42


def test_fill_at_top_edge_is_noop():
    m, sh = model()
    m.cursor = (0, 0); m.anchor = (0, 0)     # row 0: nothing above to fill from
    feed(m, "ctrl+d")                        # must not raise, writes nothing
    assert sh.get((0, 0)).is_empty()


def test_fill_is_one_undo_step():
    m, sh = model()
    m.commit(0, 0, "1")
    m.selection = ((0, 0), (2, 0)); m.anchor = (0, 0); m.cursor = (2, 0)  # A1:A3
    feed(m, "ctrl+d")
    assert sh["A3"].value == 1
    feed(m, "ctrl+z")                        # one undo unwinds the whole fill
    assert sh.get((2, 0)).is_empty() and sh.get((1, 0)).is_empty()


# ----------------------------------------------------- vim Action vocabulary
# grid_model is keymap-agnostic: it handles the Actions a stateful keymap (vim)
# emits — Chain, Hint, Save, Quit — built directly here, no vim dependency.


def test_chain_runs_members_in_order():
    m, sh = model()
    m.commit(0, 0, "5")                          # A1 = 5
    m.cursor = (0, 0)
    # dd-shaped chain: copy then clear the cursor cell
    m.apply_action(km.Chain((km.Operate("copy", ((0, 0), (0, 0))),
                             km.Operate("clear", ((0, 0), (0, 0))))))
    assert sh.get((0, 0)).is_empty()             # cleared (second member)
    assert m.clipboard is not None               # copied first (first member)


def test_chain_forwards_save_intent():
    m, sh = model()
    intent = m.apply_action(km.Chain((km.EnterMode("normal"), km.Save(prompt=False))))
    assert intent == ("save", False)             # :w-shaped chain
    assert m.mode == "normal"                    # the earlier member still ran


def test_hint_is_stored_then_cleared_leaving_command():
    m, sh = model()
    m.apply_action(km.Hint(":w"))
    assert m.hint == ":w"
    m.apply_action(km.EnterMode("normal"))
    assert m.hint == ""


def test_save_and_quit_actions_return_intents():
    m, sh = model()
    assert m.apply_action(km.Save(prompt=True)) == ("save", True)
    assert m.apply_action(km.Quit(force=False)) == ("quit", False)


def test_enter_normal_clears_selection():
    m, sh = model()
    m.selection = ((0, 0), (2, 2)); m.anchor = (0, 0); m.cursor = (2, 2)
    m.apply_action(km.EnterMode("normal"))
    assert m.selection is None and m.anchor == m.cursor


def test_enter_visual_anchors_selection_at_cursor():
    m, sh = model()
    m.cursor = (2, 1)
    m.apply_action(km.EnterMode("visual"))
    assert m.mode == "visual"
    assert m.anchor == (2, 1)
    assert m.selection == ((2, 1), (2, 1))
