"""dpg_grid_hybrid — the candidate: in-place editing + a formula/status bar.

Matthew's pick after running variants A and B: edit *in place* (the cursor cell
is the editor), with the formula bar from variant A kept up top as a combined
formula + status line (address, the cursor cell's source, and the READY/EDIT
mode). Plus readable, fixed-width columns — the earlier spikes left columns
collapsed because `SizingFixedFit` + `width=-1` inputs have no intrinsic width.

Same engine, same `grid_model.GridModel`, same key adapter as the other two.

Run:  python dpg_grid_hybrid.py demo.csv
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import dearpygui.dearpygui as dpg

import trellis_keymap as km
from trellis import Workbook, read_csv, to_a1

from dpg_grid import _named_keys, col_label, keypress_from_code, load_model
from grid_model import GridModel

COL_WIDTH = 92       # fixed data-column width (px) — the readability fix
ROW_LABEL_WIDTH = 40
TABLE_HEIGHT = 400   # scrolls past this rather than growing the window forever
MSG_FLASH_SECONDS = 3.0  # how long a copy/cut/paste status message stays up


def cell_at(pos, rects):
    """Pure hit-test: the (row, col) whose pixel rectangle contains ``pos`` (an
    (x, y) point), or ``None``. ``rects`` maps (r, c) -> ((x0, y0), (x1, y1)).
    Kept free of DearPyGui so the Shift+drag geometry is unit-testable without a
    live mouse; the thin glue that reads real rects/positions lives on the grid."""
    x, y = pos
    for key, ((x0, y0), (x1, y1)) in rects.items():
        if x0 <= x <= x1 and y0 <= y <= y1:
            return key
    return None


class HybridGrid:
    TABLE = "hy_table"
    HOLDER = "hy_holder"
    BAR = "hy_bar"
    ADDR = "hy_addr"
    MODE = "hy_mode"
    MSG = "hy_msg"
    SAVE = "hy_save"
    SAVE_DIALOG = "hy_save_dialog"
    OPEN_DIALOG = "hy_open_dialog"
    CONFIRM = "hy_confirm"
    TABBAR = "hy_tabbar"

    def __init__(self, model: GridModel):
        self.model = model
        self.models = [model]   # one GridModel per sheet tab; self.model is the active one
        # The ONE workbook every tab's sheet lives in. Sharing it is what makes
        # cross-sheet refs (``=Sheet2!A1``) and the engine's shared recalc work
        # across tabs. Falls back to a fresh book if the model wasn't given one
        # (so a bare ``HybridGrid(GridModel(sheet))`` still runs single-sheet).
        self.wb = model.workbook if model.workbook is not None else Workbook()
        self.active = 0
        self._tab_index: dict = {}
        self._tab_tags: dict[int, int | str] = {}   # tab index -> dpg tab tag
        self._status_msg = ""                        # transient copy/cut/paste feedback
        self._msg_expiry: float | None = None        # when the flash message auto-clears
        self.editing = False
        self._named = None
        self._ready_theme = None
        self._edit_theme = None
        self._highlighted: list[str] = []
        self._selecting = False
        self._shift_select = False   # a Shift+drag rectangle selection is in progress
        self._shift_hint = False     # whether the status bar is showing the Shift hint
        self._sel_theme = None
        self._copy_theme = None     # marquee tint for a copied source region
        self._cut_theme = None      # dimmed style for a cut source region
        self._marquee = None        # (rect, mode) of the clipboard source, or None
        self._pending = None        # a deferred action awaiting the unsaved-changes modal
        self._built_dims: tuple[int, int] | None = None

    def cell_tag(self, r: int, c: int) -> str:
        return f"hy_cell_{r}_{c}"

    # ---------------------------------------------------------------- build
    def build(self, parent: int | str) -> None:
        self._named = _named_keys()
        with dpg.theme() as self._ready_theme:
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (58, 92, 145))
        with dpg.theme() as self._edit_theme:
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 120, 70))
        with dpg.theme() as self._sel_theme:
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (38, 60, 96))
        with dpg.theme() as self._copy_theme:           # copied source: amber marquee tint
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (96, 80, 30))
        with dpg.theme() as self._cut_theme:            # cut source: dimmed
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (34, 34, 34))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (110, 110, 110))

        # File menu — makes the Ctrl+N/O/S gestures discoverable.
        with dpg.menu_bar(parent=parent):
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="New", shortcut="Ctrl+N", callback=lambda: self._new())
                dpg.add_menu_item(label="Open...", shortcut="Ctrl+O", callback=lambda: self._open())
                dpg.add_menu_item(label="Save", shortcut="Ctrl+S", callback=lambda: self._save())
                dpg.add_menu_item(label="Save As...", callback=lambda: dpg.show_item(self.SAVE_DIALOG))
                dpg.add_separator()
                dpg.add_menu_item(label="Close Tab", shortcut="Ctrl+W", callback=lambda: self._close_tab())

        # The formula / status bar: address | cursor cell source | mode | save state.
        with dpg.group(horizontal=True, parent=parent):

            dpg.add_text("A1", tag=self.ADDR)
            dpg.add_input_text(tag=self.BAR, width=-380, readonly=True, hint="(formula / value)")
            dpg.add_text("[READY]", tag=self.MODE)
            dpg.add_text("", tag=self.MSG)    # copy/cut/paste feedback
            dpg.add_text("", tag=self.SAVE)
        dpg.add_separator(parent=parent)
        dpg.add_tab_bar(tag=self.TABBAR, parent=parent, callback=self._on_tab_changed)
        dpg.add_group(tag=self.HOLDER, parent=parent)
        self._rebuild_table()
        self._rebuild_tabs()

        # A hidden Save As dialog, shown only when the sheet has no path yet.
        with dpg.file_dialog(
            tag=self.SAVE_DIALOG, directory_selector=False, show=False,
            default_filename="untitled.csv", width=520, height=400,
            callback=self._on_save_as,
        ):
            dpg.add_file_extension(".csv")
            dpg.add_file_extension(".*")

        # Open dialog + an unsaved-changes confirm modal.
        with dpg.file_dialog(
            tag=self.OPEN_DIALOG, directory_selector=False, show=False,
            width=520, height=400, callback=self._on_open_file,
        ):
            dpg.add_file_extension(".csv")
            dpg.add_file_extension(".*")
        with dpg.window(
            label="Unsaved changes", tag=self.CONFIRM, modal=True, show=False,
            no_resize=True, width=300, height=110,
        ):
            dpg.add_text("Discard unsaved changes?")
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Discard", width=90, callback=self._confirm_discard)
                dpg.add_button(label="Cancel", width=90, callback=self._confirm_cancel)


        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key)
            dpg.add_mouse_down_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_down)
            dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_drag)
            dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_release)

        self.refresh()
        self._subscribe_engine()

    def _rebuild_table(self) -> None:
        if dpg.does_item_exist(self.TABLE):
            dpg.delete_item(self.TABLE)
        w = self.model.window
        with dpg.table(
            tag=self.TABLE, parent=self.HOLDER, header_row=True,
            borders_innerH=True, borders_innerV=True,
            borders_outerH=True, borders_outerV=True,
            resizable=True, scrollX=True, scrollY=True, height=TABLE_HEIGHT,
            policy=dpg.mvTable_SizingFixedFit,
        ):
            dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=ROW_LABEL_WIDTH)
            for c in w.cols:
                dpg.add_table_column(label=col_label(c), width_fixed=True, init_width_or_weight=COL_WIDTH)
            for r in w.rows:
                with dpg.table_row():
                    dpg.add_text(str(r + 1))
                    for c in w.cols:
                        tag = self.cell_tag(r, c)
                        dpg.add_input_text(
                            tag=tag, width=-1, default_value=self.model.display(r, c),
                            callback=self._mirror_to_bar,
                        )
                        with dpg.item_handler_registry() as reg:
                            dpg.add_item_activated_handler(callback=self._on_cell_focus, user_data=(r, c))
                            dpg.add_item_deactivated_after_edit_handler(
                                callback=self._on_cell_commit, user_data=(r, c)
                            )
                            dpg.add_item_deactivated_handler(callback=self._on_cell_blur, user_data=(r, c))
                        dpg.bind_item_handler_registry(tag, reg)
        self._built_dims = (w.nrows, w.ncols)

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        self._tick_status()
        w = self.model.window
        if self._built_dims != (w.nrows, w.ncols):
            self._rebuild_table()
            w = self.model.window
        for r in w.rows:
            for c in w.cols:
                tag = self.cell_tag(r, c)
                if dpg.does_item_exist(tag) and not (self.editing and (r, c) == self.model.cursor):
                    dpg.set_value(tag, self.model.display(r, c))
        self._paint_cursor()
        self._update_bar()

    def _paint_cursor(self) -> None:
        # Clear every previously highlighted cell, then repaint the selection
        # rectangle (dim) with the cursor cell on top (bright) — so both
        # keyboard (Shift+arrow) and mouse drag selections show in the grid.
        for t in self._highlighted:
            if dpg.does_item_exist(t):
                dpg.bind_item_theme(t, 0)
        self._highlighted = []
        if self._marquee is not None:
            rect, mode = self._marquee
            mtheme = self._cut_theme if mode == "cut" else self._copy_theme
            (mt, ml), (mb, mr) = rect
            for r in range(mt, mb + 1):
                for c in range(ml, mr + 1):
                    tag = self.cell_tag(r, c)
                    if dpg.does_item_exist(tag) and mtheme is not None:
                        dpg.bind_item_theme(tag, mtheme)
                        self._highlighted.append(tag)
        sel = self.model.selection
        if sel is not None and self._sel_theme is not None:
            (t0, l0), (b0, r0) = sel
            for r in range(t0, b0 + 1):
                for c in range(l0, r0 + 1):
                    tag = self.cell_tag(r, c)
                    if dpg.does_item_exist(tag):
                        dpg.bind_item_theme(tag, self._sel_theme)
                        self._highlighted.append(tag)
        tag = self.cell_tag(*self.model.cursor)
        theme = self._edit_theme if self.editing else self._ready_theme
        if dpg.does_item_exist(tag) and theme is not None:
            dpg.bind_item_theme(tag, theme)
            self._highlighted.append(tag)

    def _update_bar(self) -> None:
        cr, cc = self.model.cursor
        dpg.set_value(self.ADDR, to_a1(cr, cc))
        dpg.set_value(self.MODE, "[EDIT]" if self.editing else "[READY]")
        if not self.editing:
            dpg.set_value(self.BAR, self.model.edit_text(cr, cc))
        self._update_save_state()

    def _update_save_state(self) -> None:
        name = Path(self.model.path).name if self.model.path else "(unsaved)"
        dpg.set_value(self.SAVE, f"{name} *" if self.model.dirty else name)
        self._refresh_tab_labels()

    # ------------------------------------------------- clipboard feedback
    def _set_msg(self, text: str) -> None:
        """Set the transient status message. A non-empty message 'flashes' — it
        auto-clears after MSG_FLASH_SECONDS (see _tick_status), so it can outlive
        the keypress that set it without sticking around forever."""
        self._status_msg = text
        self._msg_expiry = (time.monotonic() + MSG_FLASH_SECONDS) if text else None
        if dpg.does_item_exist(self.MSG):
            dpg.set_value(self.MSG, text)

    def _tick_status(self, now: float | None = None) -> None:
        """Clear the flash message once it has been up for MSG_FLASH_SECONDS.
        Driven by the render loop (every frame) and by refresh(); ``now`` is
        injectable for headless testing."""
        if self._status_msg and self._msg_expiry is not None:
            now = time.monotonic() if now is None else now
            if now >= self._msg_expiry:
                self._set_msg("")

    def _feedback_for_operate(self, op: str) -> None:
        """React to a clipboard Operate: flash a coordinate message and mark the
        source region. Copy/cut set the marquee (source highlighted/dimmed until
        paste, Escape, or the next copy); paste stamps and clears the marquee."""
        if op in ("copy", "cut"):
            region = self.model.clipboard_region()
            if region is not None:
                rect, mode = region
                self._marquee = region
                verb = "Cut" if mode == "cut" else "Copied"
                self._set_msg(f"{verb} {self._a1_range(rect)}")
        elif op == "paste":
            clip = self.model.clipboard
            if clip is not None:
                if self.model.selection is not None:
                    target = self.model.selection
                else:
                    rows = len(clip.cells)
                    cols = len(clip.cells[0]) if rows else 0
                    tr, tc = self.model.cursor
                    target = ((tr, tc), (tr + rows - 1, tc + cols - 1))
                self._set_msg(f"Pasted {self._a1_range(target)}")
            self._marquee = None
        elif op == "clear":
            self._set_msg("Cleared")
            self._marquee = None

    @staticmethod
    def _a1_range(rect) -> str:
        (r0, c0), (r1, c1) = rect
        a = to_a1(r0, c0)
        return a if (r0, c0) == (r1, c1) else f"{a}:{to_a1(r1, c1)}"

    def _cancel_clipboard(self) -> None:
        """Escape with nothing being edited: drop the clipboard and its marquee
        (and the flash message) — Excel cancelling the marching ants."""
        self.model.clipboard = None
        self._marquee = None
        self._set_msg("")
        self.refresh()

    # --------------------------------------------- live engine -> grid repaint
    def _subscribe_engine(self) -> None:
        """Make the visible grid repaint whenever the engine changes underneath
        it — a cross-sheet recalc cascade landing on the active sheet, or a
        direct edit to these same objects from a REPL/script (the library-first
        thesis). Inactive sheets aren't drawn, so we repaint only when the
        changed sheet is the active one; switching tabs repaints from scratch
        regardless. Sheets added later (New/Open) are watched as they appear.

        Wildcard ``*`` handlers fire after the recalc engine's own cell:change
        handler, so by the time we repaint the dependent values are already
        recomputed. (One repaint per change event — fine at CSV scale; coalesce
        if a sheet ever gets large.)"""
        for name in list(self.wb):
            self._watch_sheet(self.wb[name])
        self.wb.on("sheet:add", lambda sheet, **kw: self._watch_sheet(sheet))

    def _watch_sheet(self, sheet) -> None:
        sheet.on("*", lambda event, **kw: self._on_engine_event(sheet))

    def _on_engine_event(self, sheet) -> None:
        # Repaint only the visible grid, and never mid-edit (a repaint would
        # fight the in-place editor). Swallow render errors: a handler exception
        # propagates out of the engine's emit and would break the very write
        # that triggered it.
        if self.editing or sheet is not self.model.sheet:
            return
        try:
            self.refresh()
        except Exception:
            pass

    # ---------------------------------------------------------- key handling
    def _on_key(self, sender, app_data) -> None:
        kp = keypress_from_code(app_data, self._named)
        if self.editing:
            if kp.key == "enter":
                self._commit_active()
                self.model.apply_action(km.Move(1, 0))
                self._after_move()
            elif kp.key == "tab":
                self._commit_active()
                self.model.apply_action(km.Move(0, 1))
                self._after_move()
            elif kp.key == "escape":
                self._cancel_active()
            return
        # Ctrl+S saves. Save is a frontend gesture, not a keymap Action (the
        # ExcelKeymap leaves it unbound, exactly as the TUI does), so catch it
        # here before handing the key to the keymap.
        if kp.ctrl and kp.key == "s":
            self._save()
            return
        if kp.ctrl and kp.key == "n":
            self._new()
            return
        if kp.ctrl and kp.key == "o":
            self._open()
            return
        if kp.ctrl and kp.key == "w":
            self._close_tab()
            return
        if kp.key == "escape":
            self._cancel_clipboard()   # cancel a pending copy/cut (marquee + clipboard)
        action = km.ExcelKeymap().handle(kp, self.model.key_context())
        intent = self.model.apply_action(action)
        if intent and intent[0] == "edit":
            self._begin_edit(seed=intent[3])
        elif isinstance(action, km.Operate):
            self._feedback_for_operate(action.op)
            self.refresh()
            if dpg.does_item_exist(self.cell_tag(*self.model.cursor)):
                dpg.focus_item(self.cell_tag(*self.model.cursor))
        elif isinstance(action, (km.Move, km.MoveTo, km.Select, km.EnterMode, km.Undo, km.Redo)):
            self.refresh()
            if dpg.does_item_exist(self.cell_tag(*self.model.cursor)):
                dpg.focus_item(self.cell_tag(*self.model.cursor))


    # ------------------------------------------------------------- editing
    def _begin_edit(self, *, seed: str | None) -> None:
        r, c = self.model.cursor
        self.editing = True
        text = seed if seed is not None else self.model.edit_text(r, c)
        tag = self.cell_tag(r, c)
        dpg.set_value(tag, text)
        dpg.set_value(self.BAR, text)
        dpg.focus_item(tag)
        self._paint_cursor()
        self._update_bar()

    def _commit_active(self) -> None:
        r, c = self.model.cursor
        self.model.commit(r, c, dpg.get_value(self.cell_tag(r, c)))
        self.editing = False

    def _cancel_active(self) -> None:
        r, c = self.model.cursor
        self.editing = False
        if dpg.does_item_exist(self.cell_tag(r, c)):
            dpg.set_value(self.cell_tag(r, c), self.model.display(r, c))
        self.refresh()

    def _after_move(self) -> None:
        self.refresh()
        tag = self.cell_tag(*self.model.cursor)
        if dpg.does_item_exist(tag):
            dpg.focus_item(tag)

    def _mirror_to_bar(self, sender, app_data) -> None:
        # Live Excel-style mirror: while editing, the bar echoes the cell.
        if self.editing:
            dpg.set_value(self.BAR, dpg.get_value(sender))

    # --------------------------------------------------------------- saving
    def _save(self) -> None:
        """Ctrl+S: write to the loaded file, or pop a Save As dialog when the
        sheet has no path yet."""
        if not self.model.path:
            dpg.show_item(self.SAVE_DIALOG)
            return
        self._save_to(self.model.path)

    def _on_save_as(self, sender, app_data) -> None:  # pragma: no cover (UI dialog)
        path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
        if path:
            self._save_to(path)

    def _save_to(self, path: str) -> None:
        try:
            self.model.save(path)
        except Exception as exc:  # surface the failure in the status line
            dpg.set_value(self.SAVE, f"save failed: {exc}")
            return
        self._update_save_state()

    # ----------------------------------------------------- tabs / open / new
    def _new(self) -> None:
        """New blank sheet in its own tab (non-destructive — existing tabs
        stay, so there is nothing to discard and no prompt)."""
        self._do_new()

    def _do_new(self) -> None:
        # Add the new sheet to the SHARED workbook (not a fresh one) so it can
        # be referenced from, and reference, the other tabs.
        name = self._new_sheet_name()
        sheet = self.wb.add_sheet(name)
        self._add_tab(GridModel(sheet, workbook=self.wb))

    def _new_sheet_name(self) -> str:
        """Lowest free ``Sheet<N>`` name in the shared workbook."""
        i = 1
        while f"Sheet{i}" in self.wb:
            i += 1
        return f"Sheet{i}"

    def _unique_name(self, base: str) -> str:
        """``base`` if free in the workbook, else ``base2``, ``base3``, … —
        sheet names must be unique for cross-sheet refs to address them."""
        if base and base not in self.wb:
            return base
        i = 2
        while f"{base}{i}" in self.wb:
            i += 1
        return f"{base}{i}"

    def _open(self) -> None:
        dpg.show_item(self.OPEN_DIALOG)

    def _on_open_file(self, sender, app_data) -> None:  # pragma: no cover (UI dialog)
        path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
        if path:
            self._open_path(path)

    def _open_path(self, path: str) -> None:
        # Load into the SHARED workbook under a unique, file-derived name so the
        # opened sheet joins the same recalc graph as the other tabs.
        name = self._unique_name(Path(path).stem or "Sheet")
        read_csv(path, formulas=True, workbook=self.wb, sheet_name=name)
        sheet = self.wb[name]
        self._add_tab(GridModel(sheet, path=path, workbook=self.wb))

    def _add_tab(self, model: GridModel) -> None:
        """Append a sheet tab and make it active."""
        self.models.append(model)
        self.active = len(self.models) - 1
        self._rebuild_tabs()
        self._activate()

    def _switch_to(self, i: int) -> None:
        if i != self.active and 0 <= i < len(self.models):
            self.active = i
            self._activate()

    def _activate(self) -> None:
        """Point the view at ``self.models[self.active]`` and rebuild the grid."""
        self.model = self.models[self.active]
        self.editing = False
        self._selecting = False
        self._highlighted = []
        self._built_dims = None      # force a full rebuild for the new sheet
        self.refresh()

    def _on_tab_changed(self, sender, app_data) -> None:
        i = self._tab_index.get(app_data)
        if i is not None:
            self._switch_to(i)

    def _tab_label(self, i: int) -> str:
        """The tab caption: the engine sheet name (what you type in a
        cross-sheet ref) plus a ``*`` when that tab has unsaved edits."""
        m = self.models[i]
        return f"{m.sheet.name} *" if m.dirty else m.sheet.name

    def _rebuild_tabs(self) -> None:
        if dpg.does_item_exist(self.TABBAR):
            dpg.delete_item(self.TABBAR, children_only=True)
        self._tab_index = {}
        self._tab_tags = {}
        active_tag = None
        for i in range(len(self.models)):
            t = dpg.add_tab(label=self._tab_label(i), parent=self.TABBAR)
            self._tab_index[t] = i
            self._tab_tags[i] = t
            if i == self.active:
                active_tag = t
        dpg.add_tab_button(label="+", parent=self.TABBAR, trailing=True,
                           callback=lambda: self._new())
        if active_tag is not None:
            dpg.set_value(self.TABBAR, active_tag)

    def _refresh_tab_labels(self) -> None:
        """Update tab captions in place so the per-tab dirty marker tracks
        edits live, without the cost of a full tab-bar rebuild."""
        for i, tag in self._tab_tags.items():
            if i < len(self.models) and dpg.does_item_exist(tag):
                dpg.configure_item(tag, label=self._tab_label(i))

    def _close_tab(self) -> None:
        """Close the active tab (never the last one); a dirty tab prompts."""
        if len(self.models) <= 1:
            return
        self._guard(self._do_close)

    def _do_close(self) -> None:
        # Drop the sheet from the shared workbook too, so a closed tab stops
        # participating in cross-sheet refs and recalc (refs to it resolve to
        # an error, exactly like deleting a sheet in Excel).
        closing = self.models[self.active].sheet
        if closing.name in self.wb:
            self.wb.remove_sheet(closing.name)
        del self.models[self.active]
        if self.active >= len(self.models):
            self.active = len(self.models) - 1
        self._rebuild_tabs()
        self._activate()

    def _guard(self, proceed) -> None:
        """Run ``proceed`` now, or stash it behind the unsaved-changes modal
        (used when closing a tab that has unsaved edits)."""
        if self.model.dirty:
            self._pending = proceed
            dpg.show_item(self.CONFIRM)
        else:
            proceed()

    def _confirm_discard(self) -> None:
        dpg.hide_item(self.CONFIRM)
        pending, self._pending = self._pending, None
        if pending is not None:
            pending()

    def _confirm_cancel(self) -> None:
        dpg.hide_item(self.CONFIRM)
        self._pending = None

    # ------------------------------------------------------ mouse select
    def _cell_under_mouse(self):

        """The (row, col) of the visible cell under the pointer, or None.
        DPG has no cell-hit-test for a table, so scan the window's cells."""
        w = self.model.window
        for r in w.rows:
            for c in w.cols:
                tag = self.cell_tag(r, c)
                if dpg.does_item_exist(tag) and dpg.is_item_hovered(tag):
                    return (r, c)
        return None

    def _begin_mouse_select(self, cell) -> None:
        self.model.cursor = cell
        self.model.anchor = cell
        self.model.selection = None
        self._selecting = True
        self.refresh()

    def _drag_select_to(self, cell) -> None:
        if not self._selecting or cell == self.model.cursor:
            return
        self.model.cursor = cell
        self.model.selection = self.model._norm(self.model.anchor, cell)
        self.refresh()

    def _end_mouse_select(self) -> None:
        self._selecting = False

    # ---- Shift+drag rectangle selection (geometry-based, Shift-gated) -------
    # Runs ONLY while Shift is held; every non-Shift path is unchanged, so the
    # existing click / edit / plain-drag behaviour is untouched. We hit-test by
    # pixel geometry (mouse pos vs each cell's rect) rather than hover, because an
    # active input_text captures the mouse and breaks hover scanning mid-drag.
    def _shift_down(self) -> bool:  # pragma: no cover (live key poll)
        return bool(dpg.is_key_down(dpg.mvKey_LShift)
                    or dpg.is_key_down(dpg.mvKey_RShift))

    def _visible_cell_rects(self) -> dict:  # pragma: no cover (needs live items)
        rects = {}
        w = self.model.window
        for r in w.rows:
            for c in w.cols:
                tag = self.cell_tag(r, c)
                if dpg.does_item_exist(tag):
                    rects[(r, c)] = (tuple(dpg.get_item_rect_min(tag)),
                                     tuple(dpg.get_item_rect_max(tag)))
        return rects

    def _cell_at_mouse(self):  # pragma: no cover (needs live mouse)
        return cell_at(tuple(dpg.get_mouse_pos(local=False)), self._visible_cell_rects())

    def _begin_shift_select(self, cell) -> None:
        self.model.cursor = cell
        self.model.anchor = cell
        self.model.selection = (cell, cell)
        self._shift_select = True
        self.refresh()

    def _shift_drag_to(self, cell) -> None:
        if not self._shift_select:
            return
        self.model.cursor = cell
        self.model.selection = self.model._norm(self.model.anchor, cell)
        self.refresh()

    def _end_shift_select(self) -> None:
        self._shift_select = False

    def _tick_shift_hint(self) -> None:  # pragma: no cover (needs live viewport)
        # DearPyGui 2.x exposes no mouse-cursor API, so we can't swap to a
        # crosshair. Instead, while Shift is held (and nothing is flashing) we
        # show a one-line hint in the status bar; it doubles as a live signal
        # that Shift is being detected at all.
        if self.editing:
            return
        down = self._shift_down()
        if down and not self._status_msg:
            dpg.set_value(self.MSG, "\u21e7 drag or shift-click to select")
            self._shift_hint = True
        elif not down and self._shift_hint:
            if not self._status_msg:
                dpg.set_value(self.MSG, "")
            self._shift_hint = False

    def _on_mouse_down(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        if self.editing:
            return
        if self._shift_down():                       # Shift -> draw a rectangle
            cell = self._cell_at_mouse()
            if cell is not None:
                self._begin_shift_select(cell)
            return
        if self._selecting:
            return
        cell = self._cell_under_mouse()
        if cell is not None:
            self._begin_mouse_select(cell)

    def _on_mouse_drag(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        if self.editing:
            return
        if self._shift_select:
            cell = self._cell_at_mouse()
            if cell is not None:
                self._shift_drag_to(cell)
            return
        if not self._selecting:
            return
        cell = self._cell_under_mouse()
        if cell is not None:
            self._drag_select_to(cell)

    def _on_mouse_release(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        if self._shift_select:
            self._end_shift_select()
            return
        self._end_mouse_select()

    # -------------------------------------------------- per-cell handlers


    def _on_cell_focus(self, sender, app_data, user_data) -> None:  # pragma: no cover (needs mouse)
        if self._shift_select:
            return                       # mid Shift-drag: keep the rectangle, ignore the focus
        if not self.editing:
            self._focus_cell(user_data, extend=self._shift_down())

    def _focus_cell(self, cell, *, extend: bool) -> None:
        """A click landed on ``cell``. Normally that moves the cursor and drops
        any selection; with Shift held it instead EXTENDS the selection from the
        existing anchor to ``cell`` (Shift+click range-select — reliable, needs
        no pixel geometry). Split out from the DPG callback so it's testable."""
        self.model.cursor = cell
        if extend:
            self.model.selection = self.model._norm(self.model.anchor, cell)
        else:
            self.model.anchor = cell
            self.model.selection = None
        self._paint_cursor()
        self._update_bar()

    def _on_cell_commit(self, sender, app_data, user_data) -> None:
        r, c = user_data
        self.model.commit(r, c, dpg.get_value(self.cell_tag(r, c)))
        self.editing = False
        self.refresh()

    def _on_cell_blur(self, sender, app_data, user_data) -> None:
        r, c = user_data
        if dpg.does_item_exist(self.cell_tag(r, c)):
            dpg.set_value(self.cell_tag(r, c), self.model.display(r, c))

    # ----------------------------------------------------------------- run
    def run(self, title: str = "Trellis - DearPyGui (hybrid)") -> None:  # pragma: no cover
        dpg.create_viewport(title=title, width=960, height=600)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("primary", True)
        # Manual render loop so the flash message can auto-clear after a few
        # seconds even when the user isn't pressing keys.
        while dpg.is_dearpygui_running():
            self._tick_status()
            self._tick_shift_hint()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    argv = sys.argv[1:] if argv is None else argv
    model = load_model(argv)
    dpg.create_context()
    grid = HybridGrid(model)
    with dpg.window(tag="primary", menubar=True):
        grid.build("primary")
    grid.run()


if __name__ == "__main__":  # pragma: no cover
    main()
