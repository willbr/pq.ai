# conchars UI text in textured (zbuf) mode

**Date:** 2026-06-13
**Status:** design, awaiting review

## Problem

All UI text in the engine — centerprint (trigger messages), the console, and the
escape/options menu — is currently drawn by each frontend as an OS-native font
overlay (`TextOutW` on Windows, AppKit `NSString` on macOS, Tk canvas text items),
painted *on top of* the blitted framebuffer. The only place the real Quake conchars
bitmap font is used is the status bar (`quake/sbar.py`), which composites 8×8 glyphs
straight into the 8-bit indexed framebuffer.

In **textured (`zbuf`) mode** this OS-native text looks out of place against the
authentic software-rendered scene, and it does not match real Quake, where
`SCR_DrawCenterString`, `Con_DrawConsole`, and the menu code all blit conchars into
the framebuffer.

## Goal

In **`zbuf` mode only**, render centerprint, intermission, console, and menu using
the conchars bitmap font composited directly into the framebuffer — like the status
bar already is, and like real Quake. Stop emitting those elements as OS-native
overlays in `zbuf` mode. Wire and flat modes are unchanged (they have no framebuffer
to composite into and keep the OS-native overlay path).

### Out of scope (stay OS-native overlays in all modes)

- The debug HUD (green fps / pos / leaf / movemode string, top-left, `"nw"` anchor).
- The profiler bar chart (P-key), which rides on the debug HUD string.
- The bottom status string (only shown when the sprite sbar is absent).

These are developer/diagnostic overlays, not the game's own UI text.

## Architecture

### New module: `quake/conchars.py` — `ConFont`

A pure, UI- and OS-agnostic conchars glyph blitter. Mirrors `Sbar`'s blit
primitives but is reusable and standalone. Ports `Draw_Character` / `Draw_String`
(draw.c) and `SCR_DrawCenterString` (screen.c) line centering.

```
class ConFont:
    def __init__(self, conchars):        # raw 128x128 lump (16x16 grid of 8x8)
    def char(self, fb, fbw, x, y, n):    # 8x8 glyph; conchars index 0 transparent
    def text(self, fb, fbw, x, y, s):    # left-aligned string, 8px advance
    def text_centered(self, fb, fbw, cx, y, s):  # one line centered on cx
```

Client constructs one as `self.confont = ConFont(self.sbar.conchars)`, reusing the
lump the `Sbar` already loaded from `gfx.wad` (no second wad read).

`Sbar` is left untouched. Its private `_char` is golden-tested; rather than refactor
it to delegate to `ConFont` (regression risk against the sbar goldens), the ~8-line
glyph blit is duplicated. A code comment in both spots notes they should be unified
later.

### Console background — port `Draw_ConsoleBackground`

`gfx/conback.lmp` (present in the shareware pak; qpic: 8-byte width/height header +
320×200 palette indices) is loaded once at level load. `Draw_ConsoleBackground(lines)`
blits it across the top `lines` rows of the framebuffer. Because the framebuffer
width can differ from conback's native 320, the pic is sampled to fit the fb width
(nearest-neighbour column/row map) rather than assuming 320 wide.

The console panel covers the top ~40% of the view (matching today's overlay), conchars
text drawn over the conback. The input line is `]` + the input text, followed by the
flashing cursor (conchars index 11), blinked from `self.sv.time` (`Con_DrawInput`).

### Menu background — port `Draw_FadeScreen`

Before drawing the menu, dim the view region: a cheap pass that maps each fb pixel in
the region through the palette's darkening (use the colormap's darkest ramp row, or
simply replace with a fixed dark index — see "palette darkening" below). Then draw the
menu with conchars:

- title centered near the top of the menu block,
- each row as `label` (left) and `value_label` (right of a fixed column),
- the selected row marked with the spinning Quake menu cursor (conchars 12/13,
  toggled from `self.sv.time`).

`menu.view()` already returns `(title, [(label, value_label, is_selected), …])`.

**Palette darkening:** real Quake's `Draw_FadeScreen` lays a sparse mask of black
pixels over the screen (a fixed dither pattern) so the scene shows through dimmed.
Port that directly: over the menu region, set every pixel whose `(x ^ y) & 1` (a
checkerboard) — or denser — to palette index 0 (black). This needs no colormap and no
blend table, matches Quake's "darken without a real alpha blend" intent, and keeps the
menu text legible. Exact dither density is a tunable, not a correctness requirement.

## Data flow

`client.py` builds the zbuf framebuffer and composites the sprite sbar at
`client.py:1158-1165`. Immediately after, a new method runs:

```
Client._composite_zbuf_ui(fb, vw, vh)
    # vw, vh = view region (vh excludes the appended sbar rows)
    # draws, in framebuffer pixel coords (8px cells), in this order:
    #   1. centerprint OR intermission stats (centered, ~0.35*vh)
    #   2. console (if con.active) — conback + text
    #   3. menu (if self.menu.active) — fade + text
```

- Centerprint splits on `\n`, each line `text_centered` on `vw//2`, block top at
  ~`0.35*vh - nlines*4`. Intermission uses the same centered path with its existing
  multi-line "LEVEL COMPLETE / Time / Secrets / Kills" string.
- Console: in zbuf mode, `con.width = max(20, vw // 8)` (conchars cells, not the
  `//9` display-pixel estimate the overlay path uses), rows derived from the conback
  panel height (`panel_px // 8 - 1`).

Then the `RenderFrame` for zbuf mode is built so the frontends draw none of these as
OS text:

- the centerprint / intermission overlay tuples are **not** appended to `overlays`,
- `console=None`,
- `menu=None`.

The debug HUD and status overlays are still appended as today. In wire/flat modes the
`RenderFrame` is built exactly as before (overlays + `console`/`menu` populated).

Concretely, the mode-conditional becomes: build the centerprint/intermission/console/
menu either into the framebuffer (zbuf) or into the `RenderFrame` fields (wire/flat).

## Animation timing

Cursor blink and the menu spinner need a clock. Real Quake uses `realtime`; this port
uses `self.sv.time` (already the time source for the sbar's pickup-flash and pain-face
animations), so the UI animations share that cadence. No new timer.

## Testing

`tests/test_conchars_ui.py` (boots the full stack against shareware data, per the
`_boot()` pattern):

- **centerprint:** force `sv.center_msg = ("TEST MESSAGE", sv.time)`, render a zbuf
  frame, assert the framebuffer has conchars pixels in the centered text region, and
  assert `RenderFrame` emitted **no** center overlay, `console is None`, `menu is None`
  semantics for the centerprint (i.e. it's not in `overlays`).
- **mode contrast:** render the same state in `flat` mode, assert the centerprint **is**
  present as an overlay tuple and the framebuffer path is not taken — proving the split
  is mode-gated.
- **console:** open the console, add a line, render zbuf, assert text pixels appear in
  the top panel and `RenderFrame.console is None`.
- **menu:** open the escape menu, render zbuf, assert title/row pixels appear and
  `RenderFrame.menu is None`.

`tests/test_conchars_font.py` (standalone unit, no shareware boot needed if a tiny
synthetic conchars lump is used, else boot): construct `ConFont`, draw a known glyph
and a string into a scratch framebuffer, assert the expected non-zero pixels at the
expected offsets, and that `text_centered` offsets a line by `len(line)*4`.

Run muted: `PQ_AUDIO=0 python tests/test_conchars_ui.py` (avoids CoreAudio segfaults).

## Files touched

- **new** `quake/conchars.py` — `ConFont`.
- **new** `tests/test_conchars_ui.py`, `tests/test_conchars_font.py`.
- `client.py` — construct `ConFont` + load `gfx/conback.lmp` at level load; add
  `_composite_zbuf_ui`; make the centerprint/intermission/console/menu emission
  mode-conditional in `frame()`.

## Reference sources

- `draw.c` — `Draw_Character`, `Draw_String`, `Draw_ConsoleBackground`,
  `Draw_FadeScreen`.
- `screen.c` — `SCR_DrawCenterString` (line centering, vertical placement).
- `console.c` — `Con_DrawConsole`, `Con_DrawInput` (flashing cursor = char 11).
- `menu.c` — `M_Print` / `M_DrawCharacter`, the cursor spinner (chars 12/13).
- existing `quake/sbar.py` `_char` / `_pic` — the in-repo precedent for compositing
  conchars into the 8-bit framebuffer.
