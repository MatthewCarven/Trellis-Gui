"""GridModel — a frontend-neutral view-model over the live Trellis engine.

This is the half of the DearPyGui spike that has NOTHING to do with DearPyGui:
the windowing, cursor, selection, commit policy, and keymap execution all live
here and are exercised by `test_grid_model.py` with no GUI at all. The DPG layer
(`dpg_grid.py`) is a thin shell that draws this model and feeds it key events.

That split is the whole thesis of the spike: the engine (`trellis`) and the key
language (`trellis_keymap`) are frontend-neutral, so a GUI is mostly "draw the
window + adapt the keys" on top of reusable logic — the same shape the Textual
TUI has, with Textual swapped for DearPyGui.
"""

from __future__ import annotations

from dataclasses import dataclass

import trellis_keymap as km
from trellis import FormulaError, Sheet, infer_value, to_a1

# A windowed view never draws the whole (unbounded) sheet — only enough cells to
# cover the data plus a small working margin. Matthew's plan, realised here.
MIN_ROWS = 12
MIN_COLS = 6
PAD = 2  # let the cursor roam this far past the data before the window grows


@dataclass(frozen=True)
class Window:
    """The visible rectangle, inclusive, in zero-indexed engine coords.
    ``(top,left)`` = upper-left bound, ``(bottom,right)`` = lower-right — the
    two corners Matthew wanted tracked for O(1) "is this cell visible?" lookups.
    """

    top: int
    left: int
    bottom: int
    right: int

    @property
    def rows(self) -> range:
        return range(self.top, self.bottom + 1)

    @property
    def cols(self) -> range:
        return range(self.left, self.right + 1)

    @property
    def nrows(self) -> int:
        return self.bottom - self.top + 1

    @property
    def ncols(self) -> int:
        return self.right - self.left + 1


class GridModel:
    """Cursor + selection + windowing over one engine ``Sheet``.

    Reads go straight to the engine (`sheet.get`), writes go through the
    canonical commit policy (mirrors the TUI's `editor.commit_text`), and keys
    are executed by handing an Action from `trellis_keymap` to `apply_action`.
    """

    def __init__(self, sheet: Sheet, *, min_rows: int = MIN_ROWS, min_cols: int = MIN_COLS):
        self.sheet = sheet
        self.cursor: tuple[int, int] = (0, 0)
        self.anchor: tuple[int, int] = (0, 0)  # fixed corner of a selection
        self.selection: km.Rect | None = None
        self.mode: str = "default"
        self.min_rows = min_rows
        self.min_cols = min_cols
        # The tracked bounds. Always anchored at A1 for the spike; the lower-
        # right grows with the data and the cursor.
        self.ul: tuple[int, int] = (0, 0)
        self.lr: tuple[int, int] = (0, 0)
        self._recompute_window()

    # -------------------------------------------------------------- windowing
    def _recompute_window(self) -> None:
        max_r = self.min_rows - 1
        max_c = self.min_cols - 1
        ur = self.sheet.used_range()
        if ur is not None:
            (_t, _l), (br, bc) = ur
            max_r = max(max_r, br)
            max_c = max(max_c, bc)
        cr, cc = self.cursor
        max_r = max(max_r, cr + PAD)
        max_c = max(max_c, cc + PAD)
        self.ul = (0, 0)
        self.lr = (max_r, max_c)

    @property
    def window(self) -> Window:
        return Window(self.ul[0], self.ul[1], self.lr[0], self.lr[1])

    def in_window(self, r: int, c: int) -> bool:
        """O(1) visibility test via the tracked corners — Matthew's fast path."""
        return self.ul[0] <= r <= self.lr[0] and self.ul[1] <= c <= self.lr[1]

    # ------------------------------------------------------------------ reads
    def cell(self, r: int, c: int):
        return self.sheet.get((r, c))

    def display(self, r: int, c: int) -> str:
        """What the cell shows at rest: its computed value (errors as codes)."""
        v = self.sheet.get((r, c)).value
        if v is None:
            return ""
        if isinstance(v, FormulaError):
            return v.code
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e16:
            return str(int(v))
        return str(v)

    def edit_text(self, r: int, c: int) -> str:
        """What an edit starts from: the formula source if any, else the value
        (full fidelity — never the lossy display) so F2-style revising round-trips."""
        cell = self.sheet.get((r, c))
        if cell.formula is not None:
            return cell.formula
        return self.display(r, c)

    # ----------------------------------------------------------------- writes
    def commit(self, r: int, c: int, text: str) -> None:
        """The one engine-write path — identical policy to the TUI:
        empty clears, leading ``=`` stores as a formula (broken ones store as
        their error), anything else runs through ``infer_value``."""
        a1 = to_a1(r, c)
        if text == "":
            self.sheet.delete(a1)
        elif text.startswith("="):
            self.sheet[a1] = text
        else:
            self.sheet[a1] = infer_value(text)
        self._recompute_window()

    # -------------------------------------------------------- keymap execution
    def key_context(self) -> km.KeyContext:
        """The read-only snapshot the keymap reads — built exactly like the
        TUI's `grid.key_context`, so the SAME `ExcelKeymap` answers our keys."""
        return km.KeyContext(
            mode=self.mode,
            cursor=self.cursor,
            selection=self.selection,
            used_range=self.sheet.used_range(),
            cell=lambda r, c: self.sheet.get((r, c)),
            viewport_rows=self.window.nrows,
            viewport_cols=self.window.ncols,
            editing=False,
        )

    def apply_action(self, action: km.Action | None):
        """Execute the subset of the Action vocabulary this spike supports.
        Returns ``("edit", row, col, seed)`` when the keymap wants the editor
        opened (F2 / Enter / type-to-edit); otherwise ``None``."""
        if action is None:
            return None
        if isinstance(action, km.Move):
            self._move(action.dr, action.dc, action.extend)
        elif isinstance(action, km.MoveTo):
            self._goto(action.row, action.col, action.extend)
        elif isinstance(action, km.Select):
            (t, l), (b, r) = action.rect
            self.anchor = (t, l)
            self.cursor = (b, r)
            self.selection = action.rect
            self._recompute_window()
        elif isinstance(action, km.EnterMode):
            self.mode = action.name
            if action.name == "default":
                self.selection = None
                self.anchor = self.cursor
        elif isinstance(action, km.Operate):
            if action.op == "clear":
                self._clear(action.rect)
        elif isinstance(action, km.BeginEdit):
            return ("edit", self.cursor[0], self.cursor[1], action.seed)
        return None

    # --------------------------------------------------------------- internals
    def _move(self, dr: int, dc: int, extend: bool) -> None:
        r, c = self.cursor
        self.cursor = (max(0, r + dr), max(0, c + dc))
        if extend:
            self.selection = self._norm(self.anchor, self.cursor)
        else:
            self.anchor = self.cursor
            self.selection = None
        self._recompute_window()

    def _goto(self, r: int, c: int, extend: bool) -> None:
        self.cursor = (max(0, r), max(0, c))
        if extend:
            self.selection = self._norm(self.anchor, self.cursor)
        else:
            self.anchor = self.cursor
            self.selection = None
        self._recompute_window()

    def _clear(self, rect: km.Rect | None) -> None:
        if rect is None:
            rect = self.selection or (self.cursor, self.cursor)
        (t, l), (b, r) = rect
        for rr in range(t, b + 1):
            for cc in range(l, r + 1):
                self.sheet.delete(to_a1(rr, cc))
        self._recompute_window()

    @staticmethod
    def _norm(a: tuple[int, int], b: tuple[int, int]) -> km.Rect:
        (ar, ac), (br, bc) = a, b
        return ((min(ar, br), min(ac, bc)), (max(ar, br), max(ac, bc)))
