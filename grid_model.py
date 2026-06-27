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
from trellis import Cell, FormulaError, Sheet, infer_value, shift_formula, to_a1
from trellis_undo import attach as _attach_undo

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


@dataclass(frozen=True)
class Clipboard:
    """A snapshot of a copied/cut rectangle: ``cells`` is rows×cols of
    ``(formula, value)`` payloads, ``anchor`` is the source top-left (formula
    shifting keys off it), and ``mode`` is ``"copy"`` or ``"cut"``. Mirrors the
    TUI's clipboard, minus the terminal-only OS text bridge."""

    cells: tuple
    mode: str
    anchor: tuple[int, int]


class GridModel:
    """Cursor + selection + windowing over one engine ``Sheet``.

    Reads go straight to the engine (`sheet.get`), writes go through the
    canonical commit policy (mirrors the TUI's `editor.commit_text`), and keys
    are executed by handing an Action from `trellis_keymap` to `apply_action`.
    """

    def __init__(
        self,
        sheet: Sheet,
        *,
        min_rows: int = MIN_ROWS,
        min_cols: int = MIN_COLS,
        path: str | None = None,
    ):
        self.sheet = sheet
        self.cursor: tuple[int, int] = (0, 0)
        self.anchor: tuple[int, int] = (0, 0)  # fixed corner of a selection
        self.selection: km.Rect | None = None
        self.mode: str = "default"
        # Command-line echo a keymap may set (vim's ``:w``) via a Hint
        # action; the shell shows it while in command mode. Empty at rest.
        self.hint: str = ""
        self.min_rows = min_rows
        self.min_cols = min_cols
        # Where Ctrl+S writes: the file we loaded from, if any (None => Save As).
        self.path = path
        # True once anything has been edited since the last save/load — drives
        # the GUI's unsaved-changes marker.
        self.dirty = False
        # The internal cells clipboard (None until the first copy/cut).
        self.clipboard: Clipboard | None = None
        # Undo/redo: attach a trellis_undo.UndoLog to the sheet exactly as the
        # TUI does (also reachable at ``sheet.meta["undo"]``). Attached AFTER any
        # CSV load, so opening a file is not itself an undoable step.
        self.undo_log = _attach_undo(sheet)
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
        self.dirty = True
        self._recompute_window()

    def save(self, path: str | None = None) -> str:
        """Write the sheet to CSV (formulas preserved — round-trips with
        ``read_csv(..., formulas=True)``). Uses ``path`` if given, else the
        path we loaded from; raises ``ValueError`` if neither exists (the
        caller should prompt — a Save As). Clears the dirty flag, returns the
        path written to."""
        target = path or self.path
        if not target:
            raise ValueError("no save path: this sheet was not loaded from a file")
        self.sheet.to_csv(target, formulas=True)
        self.path = target
        self.dirty = False
        return target

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
        """Execute an Action from the keymap. Returns a frontend intent the
        model cannot perform itself, for the shell to carry out:
        ``("edit", row, col, seed)`` (open the editor — F2/Enter/type/vim ``i``),
        ``("save", prompt)`` (vim ``:w``), or ``("quit", force)`` (vim ``:q``);
        otherwise ``None``. A ``Chain`` runs its members in order and forwards
        the last intent any of them produced (``:w`` = enter-normal + save)."""
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
            # "normal" is vim's resting mode (Excel's is "default"): both
            # collapse any selection. "visual" opens a selection anchored at
            # the cursor so the next motion extends from here.
            if action.name in ("default", "normal"):
                self.selection = None
                self.anchor = self.cursor
            elif action.name == "visual":
                self.anchor = self.cursor
                self.selection = (self.cursor, self.cursor)
            if action.name != "command":
                self.hint = ""
        elif isinstance(action, km.Operate):
            if action.op == "clear":
                self._clear(action.rect)
            elif action.op == "copy":
                self._copy(action.rect, "copy")
            elif action.op == "cut":
                self._copy(action.rect, "cut")
            elif action.op == "paste":
                self._paste(action.rect)
        elif isinstance(action, km.Fill):
            self._fill(self._op_rect(action.rect), action.axis)
        elif isinstance(action, km.Undo):
            if self.undo_log.undo():
                self.dirty = True
            self._recompute_window()
        elif isinstance(action, km.Redo):
            if self.undo_log.redo():
                self.dirty = True
            self._recompute_window()
        elif isinstance(action, km.BeginEdit):
            return ("edit", self.cursor[0], self.cursor[1], action.seed)
        elif isinstance(action, km.Chain):
            # Run each member in order; forward the last intent produced (a
            # ``:w`` chain is enter-normal then Save -> the save intent wins).
            intent = None
            for member in action.actions:
                got = self.apply_action(member)
                if got is not None:
                    intent = got
            return intent
        elif isinstance(action, km.Hint):
            self.hint = action.msg
        elif isinstance(action, km.Save):
            return ("save", action.prompt)
        elif isinstance(action, km.Quit):
            return ("quit", action.force)
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
        self.dirty = True
        self._recompute_window()

    # --------------------------------------------------------- clipboard
    def _op_rect(self, rect: km.Rect | None) -> km.Rect:
        """Resolve an Operate/paste rect at execution time: explicit rect,
        else the live selection, else the cursor cell — the TUI's rule."""
        if rect is not None:
            return rect
        if self.selection is not None:
            return self.selection
        return (self.cursor, self.cursor)

    def _copy(self, rect: km.Rect | None, mode: str) -> None:
        """Snapshot a rectangle into the clipboard (no sheet write, so not
        dirty). A cut only relocates cells when it is later pasted."""
        (r0, c0), (r1, c1) = self._op_rect(rect)
        rows = []
        for r in range(r0, r1 + 1):
            payload = []
            for c in range(c0, c1 + 1):
                cell = self.sheet.get((r, c))
                payload.append((cell.formula, cell.value))
            rows.append(tuple(payload))
        self.clipboard = Clipboard(cells=tuple(rows), mode=mode, anchor=(r0, c0))

    def _paste(self, rect: km.Rect | None) -> None:
        """Stamp the clipboard at the target's top-left as ONE undo step.
        Copy mode shifts formulas by the paste offset (a 1×1 payload fills the
        whole target rect, Excel-style); cut mode pastes verbatim, clears the
        not-overwritten source cells, then demotes to copy (re-paste re-stamps)."""
        clip = self.clipboard
        if clip is None:
            return
        (t0r, t0c), (t1r, t1c) = self._op_rect(rect)
        src = clip.cells
        sr, sc = clip.anchor
        moving = clip.mode == "cut"
        with self.sheet.batch():
            if not moving and len(src) == 1 and len(src[0]) == 1:
                formula, value = src[0][0]
                for r in range(t0r, t1r + 1):
                    for c in range(t0c, t1c + 1):
                        self._paste_cell(r, c, formula, value, r - sr, c - sc)
            else:
                dr, dc = (0, 0) if moving else (t0r - sr, t0c - sc)
                written = set()
                for r_off, payload in enumerate(src):
                    for c_off, (formula, value) in enumerate(payload):
                        self._paste_cell(t0r + r_off, t0c + c_off, formula, value, dr, dc)
                        written.add((t0r + r_off, t0c + c_off))
                if moving:
                    for r in range(sr, sr + len(src)):
                        for c in range(sc, sc + len(src[0])):
                            if (r, c) not in written:
                                self.sheet.delete((r, c))
        if moving:
            self.clipboard = Clipboard(cells=src, mode="copy", anchor=clip.anchor)
        self.dirty = True
        self._recompute_window()

    def _paste_cell(self, r: int, c: int, formula, value, dr: int, dc: int) -> None:
        """Write one transferred cell. Formulas shift (off-edge refs become
        ``#REF!``); a literal ``"="``-string value stores verbatim via a
        prebuilt ``Cell``; empty source cells clear the target."""
        addr = (r, c)
        if formula is not None:
            self.sheet.set(addr, shift_formula(formula, dr, dc))
        elif value is None:
            self.sheet.delete(addr)
        elif isinstance(value, str) and value.startswith("="):
            self.sheet.set(addr, Cell(value=value))
        else:
            self.sheet.set(addr, value)

    def _fill(self, rect: km.Rect, axis: str) -> None:
        """Ctrl+D / Ctrl+R (Part 8 parity with the TUI): fill ``rect`` along
        ``axis`` ("down"/"right") as ONE undo step. A 2+-lane rect fills from
        its own first row/column (the source lane stays put); a single-lane
        rect fills from the neighbour above/left — Excel's no-selection
        gesture — and at the sheet edge there is nothing to fill from. Lanes
        transfer independently through ``_paste_cell`` (formulas shift per
        target, ``$`` pins hold, off-edge refs land as ``#REF!``, empty
        sources clear their targets); the whole fill is one engine batch.
        """
        (r0, c0), (r1, c1) = rect
        down = axis == "down"
        lo, hi = (r0, r1) if down else (c0, c1)
        if hi > lo:
            src, first = lo, lo + 1  # source lane sits inside the rect
        elif lo == 0:
            return  # at the edge: nothing above/left to fill from
        else:
            src, first = lo - 1, lo  # the neighbour above/left
        lanes = range(c0, c1 + 1) if down else range(r0, r1 + 1)
        with self.sheet.batch():
            for lane in lanes:
                addr = (src, lane) if down else (lane, src)
                cell = self.sheet.get(addr)
                for t in range(first, hi + 1):
                    tr, tc = (t, lane) if down else (lane, t)
                    dr, dc = (t - src, 0) if down else (0, t - src)
                    self._paste_cell(tr, tc, cell.formula, cell.value, dr, dc)
        self.dirty = True
        self._recompute_window()

    @staticmethod
    def _norm(a: tuple[int, int], b: tuple[int, int]) -> km.Rect:

        (ar, ac), (br, bc) = a, b
        return ((min(ar, br), min(ac, bc)), (max(ar, br), max(ac, bc)))
