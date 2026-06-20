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
from pathlib import Path

import dearpygui.dearpygui as dpg

import trellis_keymap as km
from trellis import to_a1

from dpg_grid import _named_keys, col_label, keypress_from_code, load_model
from grid_model import GridModel

COL_WIDTH = 92       # fixed data-column width (px) — the readability fix
ROW_LABEL_WIDTH = 40
TABLE_HEIGHT = 400   # scrolls past this rather than growing the window forever


class HybridGrid:
    TABLE = "hy_table"
    HOLDER = "hy_holder"
    BAR = "hy_bar"
    ADDR = "hy_addr"
    MODE = "hy_mode"
    SAVE = "hy_save"
    SAVE_DIALOG = "hy_save_dialog"

    def __init__(self, model: GridModel):
        self.model = model
        self.editing = False
        self._named = None
        self._ready_theme = None
        self._edit_theme = None
        self._highlighted: list[str] = []
        self._selecting = False
        self._sel_theme = None
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

        # The formula / status bar: address | cursor cell source | mode | save state.
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("A1", tag=self.ADDR)
            dpg.add_input_text(tag=self.BAR, width=-260, readonly=True, hint="(formula / value)")
            dpg.add_text("[READY]", tag=self.MODE)
            dpg.add_text("", tag=self.SAVE)
        dpg.add_separator(parent=parent)
        dpg.add_group(tag=self.HOLDER, parent=parent)
        self._rebuild_table()

        # A hidden Save As dialog, shown only when the sheet has no path yet.
        with dpg.file_dialog(
            tag=self.SAVE_DIALOG, directory_selector=False, show=False,
            default_filename="untitled.csv", width=520, height=400,
            callback=self._on_save_as,
        ):
            dpg.add_file_extension(".csv")
            dpg.add_file_extension(".*")

        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key)
            dpg.add_mouse_down_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_down)
            dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_drag)
            dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left, callback=self._on_mouse_release)

        self.refresh()

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
        action = km.ExcelKeymap().handle(kp, self.model.key_context())
        intent = self.model.apply_action(action)
        if intent and intent[0] == "edit":
            self._begin_edit(seed=intent[3])
        elif isinstance(action, (km.Move, km.MoveTo, km.Select, km.Operate, km.EnterMode, km.Undo, km.Redo)):
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

    def _on_mouse_down(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        if self.editing or self._selecting:
            return
        cell = self._cell_under_mouse()
        if cell is not None:
            self._begin_mouse_select(cell)

    def _on_mouse_drag(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        if self.editing or not self._selecting:
            return
        cell = self._cell_under_mouse()
        if cell is not None:
            self._drag_select_to(cell)

    def _on_mouse_release(self, sender, app_data) -> None:  # pragma: no cover (needs mouse)
        self._end_mouse_select()

    # -------------------------------------------------- per-cell handlers


    def _on_cell_focus(self, sender, app_data, user_data) -> None:
        if not self.editing:
            self.model.cursor = user_data
            self.model.anchor = user_data
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
        dpg.start_dearpygui()
        dpg.destroy_context()


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    argv = sys.argv[1:] if argv is None else argv
    model = load_model(argv)
    dpg.create_context()
    grid = HybridGrid(model)
    with dpg.window(tag="primary"):
        grid.build("primary")
    grid.run()


if __name__ == "__main__":  # pragma: no cover
    main()
