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

_REPO = Path(__file__).resolve().parents[2]
for _src in (_REPO / "src", _REPO / "packages" / "trellis-keymap" / "src"):
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

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

    def __init__(self, model: GridModel):
        self.model = model
        self.editing = False
        self._named = None
        self._ready_theme = None
        self._edit_theme = None
        self._highlighted: str | None = None
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

        # The formula / status bar: address | cursor cell source | mode.
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("A1", tag=self.ADDR)
            dpg.add_input_text(tag=self.BAR, width=-90, readonly=True, hint="(formula / value)")
            dpg.add_text("[READY]", tag=self.MODE)
        dpg.add_separator(parent=parent)
        dpg.add_group(tag=self.HOLDER, parent=parent)
        self._rebuild_table()

        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key)

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
        if self._highlighted and dpg.does_item_exist(self._highlighted):
            dpg.bind_item_theme(self._highlighted, 0)
        tag = self.cell_tag(*self.model.cursor)
        theme = self._edit_theme if self.editing else self._ready_theme
        if dpg.does_item_exist(tag) and theme is not None:
            dpg.bind_item_theme(tag, theme)
            self._highlighted = tag

    def _update_bar(self) -> None:
        cr, cc = self.model.cursor
        dpg.set_value(self.ADDR, to_a1(cr, cc))
        dpg.set_value(self.MODE, "[EDIT]" if self.editing else "[READY]")
        if not self.editing:
            dpg.set_value(self.BAR, self.model.edit_text(cr, cc))

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
        action = km.ExcelKeymap().handle(kp, self.model.key_context())
        intent = self.model.apply_action(action)
        if intent and intent[0] == "edit":
            self._begin_edit(seed=intent[3])
        elif isinstance(action, (km.Move, km.MoveTo, km.Select, km.Operate, km.EnterMode)):
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
