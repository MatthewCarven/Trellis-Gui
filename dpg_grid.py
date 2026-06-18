"""dpg_grid — a DearPyGui shell over the Trellis engine. A spike, not a product.

Goal: feel out DearPyGui's ergonomics for a spreadsheet and prove the seam the
S40 work created — a frontend is `import trellis` (engine) + `import
trellis_keymap` (key language) + "draw the window and adapt the keys". No
Textual anywhere.

Interaction model (deliberately the TUI's, ported):
  * The visible window is a grid of read-only value cells (Matthew's "enough
    text-boxes for the active view") — read-only so they never fight the
    keyboard. They show computed VALUES, so recalc is visible as you edit.
  * A formula bar at the top edits the cursor cell (prefilled with the formula
    source, full fidelity). Enter commits -> engine recomputes -> grid repaints.
  * EVERY key flows through the same `ExcelKeymap` the TUI uses, via a tiny
    DearPyGui-keycode -> KeyPress adapter (the "~20-line adapter" the keymap
    contract always anticipated, here in its DearPyGui flavour). Arrows move,
    Ctrl+Home jumps, Ctrl+A selects, Delete clears, a printable / F2 / Enter
    opens the formula bar.

What this spike does NOT do (out of scope, each easy to add later): in-place
cell editing, mouse drag-select, undo wiring (trellis-undo attaches the same
way it does in the TUI), multi-sheet tabs, themes.
"""

from __future__ import annotations

import sys
from pathlib import Path

# --- spike convenience: run straight from a checkout, no install needed.
# A real frontend just declares `trellis` + `trellis-keymap` as deps; this
# block puts the in-repo source on the path so `python dpg_grid.py` works
# before (or without) installing anything but dearpygui. ---
_REPO = Path(__file__).resolve().parents[2]
for _src in (_REPO / "src", _REPO / "packages" / "trellis-keymap" / "src"):
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

import dearpygui.dearpygui as dpg

import trellis_keymap as km
from trellis import Workbook, read_csv, to_a1

from grid_model import GridModel


# --------------------------------------------------------------------------
# The key adapter: a DearPyGui key event -> the contract's textual-free
# KeyPress. Named keys via a table; letters/digits computed from the
# contiguous mvKey ranges; modifiers polled. This is the ONLY DearPyGui-
# specific knowledge the key path needs — swap this file's framework and the
# keymap is untouched.
# --------------------------------------------------------------------------
def _named_keys() -> dict[int, str]:
    return {
        dpg.mvKey_Up: "up", dpg.mvKey_Down: "down",
        dpg.mvKey_Left: "left", dpg.mvKey_Right: "right",
        dpg.mvKey_Home: "home", dpg.mvKey_End: "end",
        dpg.mvKey_Return: "enter", dpg.mvKey_Escape: "escape",
        dpg.mvKey_Delete: "delete", dpg.mvKey_Back: "backspace",
        dpg.mvKey_Tab: "tab", dpg.mvKey_F2: "f2", dpg.mvKey_Spacebar: "space",
    }


def _down(*names: str) -> bool:
    for n in names:
        code = getattr(dpg, n, None)
        if code is not None and dpg.is_key_down(code):
            return True
    return False


def keypress_from_code(code: int, named: dict[int, str] | None = None) -> km.KeyPress:
    named = named if named is not None else _named_keys()
    ctrl = _down("mvKey_LControl", "mvKey_RControl")
    shift = _down("mvKey_LShift", "mvKey_RShift")
    alt = _down("mvKey_LAlt", "mvKey_RAlt")
    name = named.get(code)
    char: str | None = None
    if name is None:
        if dpg.mvKey_A <= code <= dpg.mvKey_Z:
            base = chr(ord("a") + code - dpg.mvKey_A)
            name = base
            char = base.upper() if shift else base
        elif dpg.mvKey_0 <= code <= dpg.mvKey_9:
            name = chr(ord("0") + code - dpg.mvKey_0)
            char = name
        else:
            name = str(code)  # unknown — the keymap will decline it
    # ctrl-combinations are commands, never printable text
    return km.KeyPress(key=name, char=(None if ctrl else char), ctrl=ctrl, alt=alt, shift=shift)


def col_label(c: int) -> str:
    return "".join(ch for ch in to_a1(0, c) if ch.isalpha())


# --------------------------------------------------------------------------
class DpgGrid:
    """The view. Owns no spreadsheet logic — it draws `GridModel` and feeds it
    keys. Every method that mutates state goes through the model."""

    TABLE = "grid_table"
    HOLDER = "table_holder"
    BAR = "formula_bar"
    STATUS = "status_text"
    CURSOR_LABEL = "cursor_label"

    def __init__(self, model: GridModel):
        self.model = model
        self._named = None          # built after the context exists
        self._cursor_theme = None
        self._built_dims: tuple[int, int] | None = None  # (nrows, ncols) drawn

    # ------------------------------------------------------------- tags
    def cell_tag(self, r: int, c: int) -> str:
        return f"cell_{r}_{c}"

    # ------------------------------------------------------------- build
    def build(self, parent: int | str) -> None:
        self._named = _named_keys()
        with dpg.theme() as self._cursor_theme:
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (58, 92, 145))

        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("A1", tag=self.CURSOR_LABEL)
            dpg.add_input_text(
                tag=self.BAR, width=-1, on_enter=True,
                callback=self._on_bar_enter, hint="value or =formula",
            )
        dpg.add_separator(parent=parent)
        dpg.add_group(tag=self.HOLDER, parent=parent)
        self._rebuild_table()
        dpg.add_separator(parent=parent)
        dpg.add_text("", tag=self.STATUS, parent=parent)

        with dpg.handler_registry():
            dpg.add_key_press_handler(callback=self._on_key)

        self.refresh(focus_bar=False)

    def _rebuild_table(self) -> None:
        if dpg.does_item_exist(self.TABLE):
            dpg.delete_item(self.TABLE)
        w = self.model.window
        with dpg.table(
            tag=self.TABLE, parent=self.HOLDER, header_row=True,
            borders_innerH=True, borders_innerV=True,
            borders_outerH=True, borders_outerV=True,
            policy=dpg.mvTable_SizingFixedFit,
        ):
            dpg.add_table_column(label="", width_fixed=True, init_width_or_weight=36)
            for c in w.cols:
                dpg.add_table_column(label=col_label(c))
            for r in w.rows:
                with dpg.table_row():
                    dpg.add_text(str(r + 1))
                    for c in w.cols:
                        dpg.add_input_text(
                            tag=self.cell_tag(r, c), width=-1, readonly=True,
                            default_value=self.model.display(r, c),
                        )
        self._built_dims = (w.nrows, w.ncols)

    # ------------------------------------------------------------ refresh
    def _sync_structure(self) -> None:
        """Redraw the table only if the window's size changed (data grew)."""
        w = self.model.window
        if self._built_dims != (w.nrows, w.ncols):
            self._rebuild_table()

    def refresh(self, *, focus_bar: bool = False) -> None:
        self._sync_structure()
        w = self.model.window
        for r in w.rows:
            for c in w.cols:
                tag = self.cell_tag(r, c)
                if dpg.does_item_exist(tag):
                    dpg.set_value(tag, self.model.display(r, c))
        self._paint_cursor()
        cr, cc = self.model.cursor
        dpg.set_value(self.CURSOR_LABEL, to_a1(cr, cc))
        dpg.set_value(self.BAR, self.model.edit_text(cr, cc))
        mode = "" if self.model.mode == "default" else f"  -- {self.model.mode.upper()} --"
        sel = ""
        if self.model.selection:
            (t, l), (b, rr) = self.model.selection
            sel = f"   sel {to_a1(t, l)}:{to_a1(b, rr)}"
        dpg.set_value(self.STATUS, f"cursor {to_a1(cr, cc)}{sel}{mode}")
        if focus_bar and dpg.does_item_exist(self.BAR):
            dpg.focus_item(self.BAR)

    _highlighted: str | None = None

    def _paint_cursor(self) -> None:
        if self._highlighted and dpg.does_item_exist(self._highlighted):
            dpg.bind_item_theme(self._highlighted, 0)
        tag = self.cell_tag(*self.model.cursor)
        if dpg.does_item_exist(tag) and self._cursor_theme is not None:
            dpg.bind_item_theme(tag, self._cursor_theme)
            self._highlighted = tag

    # ----------------------------------------------------------- callbacks
    def _on_bar_enter(self, sender, app_data) -> None:
        cr, cc = self.model.cursor
        self.model.commit(cr, cc, dpg.get_value(self.BAR))
        self.model.apply_action(km.Move(1, 0))  # Excel: commit moves down
        self.refresh()

    def _on_key(self, sender, app_data) -> None:
        # While the formula bar has focus it owns the keyboard (text entry,
        # its own Enter-to-commit). The grid keymap only drives the grid.
        if dpg.does_item_exist(self.BAR) and dpg.is_item_focused(self.BAR):
            if app_data == dpg.mvKey_Escape:
                self.refresh()  # discard the in-progress edit, restore the bar
                dpg.focus_item(self.TABLE)
            return
        kp = keypress_from_code(app_data, self._named)
        action = km.ExcelKeymap().handle(kp, self.model.key_context())
        intent = self.model.apply_action(action)
        self.refresh(focus_bar=bool(intent and intent[0] == "edit"))
        if intent and intent[0] == "edit" and intent[3]:
            dpg.set_value(self.BAR, intent[3])  # type-to-edit: seed the bar

    # --------------------------------------------------------------- run
    def run(self, title: str = "Trellis - DearPyGui (formula bar)") -> None:  # pragma: no cover
        dpg.create_viewport(title=title, width=900, height=560)
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("primary", True)
        dpg.start_dearpygui()
        dpg.destroy_context()


# --------------------------------------------------------------------------
def load_model(argv: list[str]) -> GridModel:
    if argv and Path(argv[0]).exists():
        wb = read_csv(argv[0], formulas=True)  # CSV-as-spreadsheet: formulas live
    else:
        wb = Workbook()
        wb.add_sheet("Sheet1")
    sheet = wb[next(iter(wb))]  # first sheet by name (Workbook iterates names)
    return GridModel(sheet)


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    argv = sys.argv[1:] if argv is None else argv
    model = load_model(argv)
    dpg.create_context()
    grid = DpgGrid(model)
    with dpg.window(tag="primary"):
        grid.build("primary")
    grid.run()


if __name__ == "__main__":  # pragma: no cover
    main()
