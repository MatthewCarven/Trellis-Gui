# dpg-grid — a DearPyGui spike over the Trellis engine

A **throwaway spike**, not a package. It exists to answer one question before
committing to a GUI stack: *how does DearPyGui feel for a spreadsheet, and does
the S40 packaging/extraction actually pay off?* The answer to the second is yes,
and this is the proof.

## The thesis it proves

A Trellis frontend is three things bolted together:

```
import trellis           # the engine  (values, formulas, recalc)
import trellis_keymap     # the key language (Excel/vim, frontend-neutral)
+ "draw the window and adapt the keys"   <- the only framework-specific part
```

No Textual. The DearPyGui layer here is ~250 lines, and the only DPG-specific
knowledge in the whole key path is one small adapter (`keypress_from_code`) that
turns a DearPyGui key event into the contract's `KeyPress` — the "~20-line
adapter" the keymap contract always anticipated, now in its DPG flavour. The
**same `ExcelKeymap` the terminal UI uses** drives this GUI unchanged.

## Layout

- `grid_model.py` — the engine-neutral half: windowing (`used_range` ∪ a
  minimum, with the upper-left/lower-right bounds tracked for O(1) "is it
  visible?" lookups — your plan), cursor/selection, the commit policy (identical
  to the TUI's: empty clears, `=` stores a formula, else `infer_value`), and
  `apply_action` that executes the keymap's Actions. **Zero DearPyGui.**
- `dpg_grid.py` — **variant A, formula bar**: a windowed grid of read-only value
  cells (your "text-boxes for the active view") + a formula bar that edits the
  cursor cell. Conflict-free; the keymap drives everything. `python dpg_grid.py demo.csv`.
- `dpg_grid_inplace.py` — **variant B, in-place**: a SECOND view over the *same*
  `grid_model.py`. The cursor cell itself is the editor, modal like Excel
  (READY vs EDIT). `python dpg_grid_inplace.py demo.csv`.
- `dpg_grid_hybrid.py` — **variant C, THE CANDIDATE** (Matthew's pick): in-place
  editing **+** the formula bar kept up top as a combined formula/status line
  (address, cursor-cell source, READY/EDIT), **+ readable fixed-width columns**.
  `python dpg_grid_hybrid.py demo.csv`.
- `test_grid_model.py` (41) + `test_dpg_headless.py` (9) + `test_inplace_headless.py`
  (7) + `test_hybrid_headless.py` (41) = **98 headless tests** — see "What's verified".
- `demo.csv` — a tiny budget with live formulas (`=B2*C2`, `=SUM(D2:D5)`).

## Run it

Install the deps — DearPyGui plus the engine and keymap (editable, from the
sibling `../Trellis` checkout) — then run any variant:

```
pip install -r requirements.txt
python dpg_grid_hybrid.py   demo.csv   # variant C  <- the candidate
python dpg_grid.py          demo.csv   # variant A (formula bar)
python dpg_grid_inplace.py  demo.csv   # variant B (in-place)
```

(`requirements.txt` pulls in `dearpygui` and installs `-e ../Trellis` +
`-e ../Trellis/packages/trellis-keymap`; adjust those paths if your Trellis
checkout lives elsewhere. `pytest` works from this dir too once they're installed.)

**Try (both):** arrow-key around; land on `D2` (shows `6`, formula `=B2*C2`);
change `B2` to `10` and commit — `D2` and the grand total recompute in the grid.
`Ctrl+Home` jumps to A1, `Ctrl+A` selects the used range, `Delete` clears.
- **Variant A:** edits happen in the formula bar at the top.
- **Variant B:** press F2 (or just start typing) to edit the cell in place — the
  cell turns green and shows the formula source; Enter commits and moves down,
  Tab commits and moves right, Esc cancels. Watch the `[READY]`/`[EDIT]` status.

## The two variants, compared

Both share `grid_model.py` and the key adapter — they differ only in *where the
edit happens*. Same engine, same keymap, two feels.

**Variant A — formula bar** (`dpg_grid.py`). Value cells are read-only, so nothing
ever fights the keyboard; the cursor cell is edited in a bar at the top. This is
the Textual TUI's model. Clean, conflict-free, every key flows through
`ExcelKeymap` — but it's un-Excel-like (you don't type *in* the grid).

**Variant B — in-place** (`dpg_grid_inplace.py`). The cursor cell *is* the editor,
so it feels like a real spreadsheet — at the cost of a genuine mode (the tension
the spike was built to expose):
- **READY**: arrows/Ctrl+Home/Ctrl+A move the cursor (the keymap drives). F2, Enter,
  or any printable begins editing.
- **EDIT**: the cell's `input_text` owns the keyboard — type, arrows move the
  *caret*; Enter commits + moves down, Tab commits + moves right, Esc cancels.
  The global key handler steps aside (gated on an `editing` flag) so DPG and the
  keymap don't both grab the same keystroke.

**Variant C — hybrid** (`dpg_grid_hybrid.py`) is B plus A's formula bar, kept up
top as a combined **formula + status line** (the cursor cell's source, the
address, and the `[READY]`/`[EDIT]` mode), and with **fixed-width columns**
(the first spikes left columns collapsed — `SizingFixedFit` with `width=-1`
inputs gives the columns no intrinsic width; explicit `init_width_or_weight`
fixes it, plus horizontal/vertical scroll). This is the direction Matthew
picked for the real GUI.

Frictions variant B (and so C) surfaces (the point of the exercise — eyeball them live):
- **Modal arrows.** Arrows can't move the in-cell caret unless you're in EDIT
  mode — exactly Excel's ready/edit split, and it has to be tracked explicitly.
- **Type-to-replace timing.** A printable in READY seeds the cell with that char;
  whether DPG also delivers the keystroke natively (doubling it) is the kind of
  thing only a live run shows.
- **Click gives a caret.** Because every cell is an editable `input_text`,
  clicking one shows a text caret, so a click-then-type edits in place even
  without F2 — unlike Excel's single-click-selects. A real build might use
  read-only display widgets + an overlaid editor to control this precisely.

## What's verified vs. what to check on first run

**Verified headlessly (99 tests, no GPU):** the model logic, the commit policy,
**recalc propagation showing up in the grid** (both variants), the keymap driving
the cursor, type-to-edit seeding, the window growing to cover new far cells, and
variant B's full modal flow (F2/type begins, Enter/Tab commit + move, Esc
cancels). DearPyGui builds its item tree and runs get/set without a viewport, so
the construction code and callbacks are exercised for real.

**Not coverable headlessly — eyeball these on your machine:**
- Live key dispatch (the global key handler actually firing) and rendering.
- The cursor highlight theme and the formula-bar focus hand-off.
- `is_key_down` modifier polling for `Ctrl+A` / `Ctrl+Home` (headless it reads
  no keys down, so those paths are tested via the model, not through DPG).
- Mouse selection: the selection math (Shift+click/drag extend from the anchor)
  is unit-tested via the model. On a real viewport, Shift+click extends as
  intended; a drag does NOT paint across cells, because DPG reports hover only for
  the pressed cell mid-drag — so only the press registers. Shift+click is the
  group-select; click-drag-to-paint is a known gap (would need a verified
  pixel→cell mapping or an overlay).

## Ergonomic questions this spike is meant to surface

- **Formula bar vs. in-place editing — both now built** (variants A and B; see
  "The two variants, compared"). Run them back-to-back to decide which feel the
  real GUI should take, or whether a hybrid (read-only display cells + an
  overlaid editor) beats both.
- **One redraw strategy.** Here the table rebuilds only when the window's size
  changes; values repaint via `set_value`. Fine at CSV scale; revisit if a
  window ever gets large.
- **Now built on the hybrid candidate** — the basic-spreadsheet essentials beyond
  edit/recalc: undo/redo (`Ctrl+Z`/`Ctrl+Y`, a `trellis-undo` log attached the way
  the TUI does it), save (`Ctrl+S` → `sheet.to_csv(formulas=True)`, with a Save As
  dialog), copy/cut/paste (`Ctrl+C`/`X`/`V`, formulas shift on paste; the source
  region is marked until paste or `Esc` — an amber tint for copy, dimmed for cut —
  and a coordinate flash like "Copied A1:B2" appears in the status bar for a few
  seconds), mouse selection — Shift+click a cell to extend the selection from the
  active cell out to it (the reliable group-select); a plain click/drag just sets
  the active cell. (DearPyGui doesn't report hover on the other cells mid-drag, so
  click-drag-to-paint isn't wired up — Shift+click covers ranges instead; and DPG
  2.x has no settable mouse cursor, so a status-bar hint shows while Shift is
  held.) The selection paints as a visible rectangle. Open/New (`Ctrl+O`/`Ctrl+N`) plus a File menu, and
  multi-sheet tabs (`Ctrl+W` closes, with an unsaved-changes guard). All tabs share
  one Workbook, so a formula can reference another sheet by name (`=Sheet2!A1`) and
  the shared recalc keeps it live; each tab's caption shows its sheet name with a
  `*` dirty marker. The visible grid repaints live when the engine changes
  underneath it — a cross-sheet recalc cascade landing on the active sheet, or a
  direct edit to the same objects from a REPL (the library-first thesis), shows up
  without a manual refresh. A cascade's burst of cell events is coalesced into one
  repaint on the next frame, so a large recalc costs a single grid repaint rather
  than one per changed cell.

## Status

The standalone GUI project, moved out of the Trellis monorepo's `spikes/dpg-grid`.
It depends only on the published `trellis` + `trellis-keymap` + `trellis-undo`.

## Roadmap / TODO

**Recently shipped:**

- `Ctrl+D` / `Ctrl+R` fill — `grid_model.apply_action` honours the shared `ExcelKeymap`'s
  `Fill` action (silently dropped before), mirroring the TUI: formulas shift per target lane,
  `$` pins hold, off-edge refs become `#REF!`, one undo step.
- **The model layer is now vim-ready.** `grid_model.apply_action` handles the full Action
  vocabulary a stateful keymap emits: `Chain` (`dd` = copy+clear, `:w` = enter-normal+save),
  `Hint` (the `:` command-line echo, stored on `model.hint`), and `Save`/`Quit` (returned as
  `("save", prompt)` / `("quit", force)` intents for the shell). `EnterMode` now treats vim's
  `normal` like Excel's `default` (collapses selection) and anchors a `visual` selection at the
  cursor. Driven end-to-end through the real `VimKeymap`, all of `dd`/`yy`/`p`/visual-`y`/`:w`/`:q`
  produce the right model state — proven by tests built from directly-constructed Actions (no vim
  dependency in the suite).
- *(main Trellis repo)* `trellis-tui-vim` renamed to **`trellis-vim`** (frontend-neutral), and
  `trellis_keymap` now exposes `__version__`.

**Open — vim support, the shell half.** The model is ready; what remains is the DearPyGui shell
(`dpg_grid_hybrid.py`):

- Hold ONE keymap instance via `trellis_keymap.load_keymap(name)` instead of constructing
  `ExcelKeymap()` per keypress (a stateful keymap needs its parse state to persist), set from a
  new `--keymap NAME` / `--vim` CLI flag.
- Show the keymap mode in the bar (`-- NORMAL --` / `-- INSERT --` / `-- VISUAL --`, and
  `model.hint` while in command mode) instead of just `[READY]`/`[EDIT]`.
- Wire the `("save", ...)` / `("quit", ...)` intents returned from `apply_action` to the shell's
  existing save / window-close.
- **The reconciliation:** today the editing branch handles `Enter`/`Tab`/`Esc` directly, bypassing
  the keymap — so vim's `Esc` would never reach the keymap and its mode would desync. Editing keys
  need to route through the held keymap so modes stay in sync. **Decided:** `Esc` in insert mode
  **cancels the in-progress edit** and returns to NORMAL (not the vim-standard commit) — uniform
  with Excel's Esc-cancels, so the editing branch keeps its current cancel behaviour and *also*
  drops the keymap to NORMAL.
