# conchars UI text in textured (zbuf) mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In textured (`zbuf`) mode, render centerprint, intermission, console, and menu using the real Quake conchars bitmap font composited into the 8-bit framebuffer, replacing the OS-native overlay path (wire/flat unchanged).

**Architecture:** A new pure `quake/conchars.py` (`ConFont` glyph blitter + `load_qpic`/`blit_conback`/`fade_region` helpers) mirrors the existing `Sbar` conchars compositing. `Client` builds a `ConFont` and loads `gfx/conback.lmp` once, then a new `Client._composite_zbuf_ui` draws the UI into the framebuffer right after the sbar composite; `frame()` emits these as OS overlays only in non-zbuf modes.

**Tech Stack:** Pure Python stdlib. Conchars lump is 128×128 raw 8-bit (16×16 grid of 8×8 glyphs); `gfx/conback.lmp` is a qpic (8-byte w/h header + 320×200 indices). Tests are standalone scripts under `tests/` using `_bootstrap` and real shareware data.

**Spec:** `docs/superpowers/specs/2026-06-13-conchars-ui-design.md`

---

## File structure

- **Create** `quake/conchars.py` — `ConFont` (char/text/text_centered) + `load_qpic`, `blit_conback`, `fade_region`. Pure: no OS/UI/engine imports.
- **Create** `tests/test_conchars_font.py` — unit tests for `quake/conchars.py` with synthetic lumps (no shareware boot).
- **Create** `tests/test_conchars_ui.py` — integration: boots `Client`, drives zbuf frames, asserts framebuffer compositing + mode-gated overlay emission.
- **Modify** `client.py` — import the new module; construct `ConFont`/load conback in `__init__`; add `_composite_zbuf_ui`; make the centerprint/intermission/console/menu emission mode-conditional in `frame()`.

---

## Task 1: `ConFont` glyph blitter

**Files:**
- Create: `quake/conchars.py`
- Test: `tests/test_conchars_font.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_conchars_font.py`:

```python
"""Unit tests for quake/conchars.py: the conchars bitmap-font blitter and the
qpic/console-background/fade helpers that the zbuf UI compositing uses. Uses
synthetic lumps so it needs no shareware data."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from quake.conchars import ConFont, load_qpic, blit_conback, fade_region


def _conchars_with(glyphs):
    """128x128 lump with each (char_num -> fill_index) glyph cell filled."""
    src = bytearray(128 * 128)
    for num, val in glyphs.items():
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            for i in range(8):
                src[(sy + r) * 128 + sx + i] = val
    return bytes(src)


def test_char_blits_8x8_glyph_at_offset():
    cf = ConFont(_conchars_with({65: 7}))   # 'A'
    fb = bytearray(16 * 16)
    cf.char(fb, 16, 2, 3, 65)
    assert fb[3 * 16 + 2] == 7              # top-left of the glyph
    assert fb[(3 + 7) * 16 + (2 + 7)] == 7  # bottom-right of the glyph
    assert fb[0] == 0                       # outside the glyph: untouched


def test_char_index_zero_is_transparent():
    cf = ConFont(_conchars_with({65: 0}))   # all-zero glyph
    fb = bytearray(16 * 16)
    for i in range(len(fb)):
        fb[i] = 5
    cf.char(fb, 16, 0, 0, 65)
    assert all(b == 5 for b in fb)          # nothing overwritten


def test_text_advances_8px_per_char():
    cf = ConFont(_conchars_with({ord('X'): 9}))
    fb = bytearray(80 * 8)
    cf.text(fb, 80, 0, 0, "XX")
    assert fb[0] == 9 and fb[8] == 9        # two glyphs, 8px apart
    assert fb[16] == 0


def test_text_centered_offsets_by_half_width():
    cf = ConFont(_conchars_with({ord('X'): 9}))
    fb = bytearray(80 * 8)
    cf.text_centered(fb, 80, 40, 0, "XX")   # 2 chars -> start at 40 - 8 = 32
    assert fb[32] == 9
    assert fb[31] == 0


def test_load_qpic_parses_header():
    lump = bytes([3, 0, 0, 0, 2, 0, 0, 0]) + bytes(range(6))  # 3x2
    w, h, px = load_qpic(lump)
    assert (w, h) == (3, 2)
    assert px == bytes(range(6))


def test_blit_conback_fills_top_rows_only():
    pic = (2, 2, bytes([1, 1, 1, 1]))       # solid 2x2 of index 1
    fb = bytearray(4 * 4)
    blit_conback(fb, 4, 4, pic, 2)          # only top 2 rows
    assert all(fb[y * 4 + x] for y in range(2) for x in range(4))
    assert all(fb[y * 4 + x] == 0 for y in range(2, 4) for x in range(4))


def test_fade_region_dithers_to_black():
    fb = bytearray([5] * (4 * 4))
    fade_region(fb, 4, 0, 0, 4, 4)
    # checkerboard: (x ^ y) & 1 cleared to 0, the rest left at 5
    assert fb[0 * 4 + 1] == 0 and fb[1 * 4 + 0] == 0
    assert fb[0 * 4 + 0] == 5 and fb[1 * 4 + 1] == 5


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_conchars_font.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'quake.conchars'`.

- [ ] **Step 3: Write the implementation**

Create `quake/conchars.py`:

```python
"""Conchars bitmap font + qpic helpers for compositing UI text into the 8-bit
indexed framebuffer (the same surface the status bar draws into). Ports
draw.c's Draw_Character / Draw_String / Draw_ConsoleBackground / Draw_FadeScreen
and screen.c's SCR_DrawCenterString line centering.

Pure: no OS, UI, or engine imports. The conchars lump is a 128x128 raw 8-bit
image (a 16x16 grid of 8x8 glyphs); glyph index 0 is transparent. Quake .lmp
qpics are an 8-byte width/height header followed by width*height palette
indices.

NOTE: ConFont.char duplicates quake/sbar.py's Sbar._char (the same 8x8 blit).
They should be unified onto this module eventually; sbar.py is left untouched
for now to avoid disturbing its golden tests.
"""

import struct


class ConFont:
    """Draw_Character / Draw_String from a conchars lump."""

    def __init__(self, conchars):
        self.src = conchars                  # 128*128 bytes

    def char(self, fb, fbw, x, y, num):
        """Draw_Character: 8x8 glyph from the 16x16 grid; index 0 transparent."""
        src = self.src
        sy, sx = (num >> 4) * 8, (num & 15) * 8
        for r in range(8):
            s = (sy + r) * 128 + sx
            d = (y + r) * fbw + x
            for i in range(8):
                b = src[s + i]
                if b:
                    fb[d + i] = b

    def text(self, fb, fbw, x, y, s):
        """Draw_String: left-aligned, 8px advance. High bytes wrap into the
        gold/brown half of the conchars grid, exactly like Draw_Character."""
        for ch in s:
            self.char(fb, fbw, x, y, ord(ch) & 255)
            x += 8

    def text_centered(self, fb, fbw, cx, y, s):
        """One line centered on cx (SCR_DrawCenterString: x = cx - len*8/2)."""
        self.text(fb, fbw, cx - len(s) * 4, y, s)


def load_qpic(lump):
    """Parse a .lmp qpic -> (width, height, indices)."""
    w, h = struct.unpack_from("<ii", lump, 0)
    return (w, h, lump[8:8 + w * h])


def blit_conback(fb, fbw, fbh, pic, lines):
    """Draw_ConsoleBackground: stretch the conback pic across the framebuffer
    (nearest-neighbour) and paint only the top `lines` rows -- the console
    panel over the top of the scene."""
    pw, ph, px = pic
    for dy in range(min(lines, fbh)):
        sy = (dy * ph // fbh) * pw
        d = dy * fbw
        for dx in range(fbw):
            fb[d + dx] = px[sy + dx * pw // fbw]


def fade_region(fb, fbw, x0, y0, x1, y1):
    """Draw_FadeScreen: a checkerboard of black (palette index 0) over the
    region so the scene shows through dimmed, no blend table needed."""
    for y in range(y0, y1):
        base = y * fbw
        for x in range(x0, x1):
            if (x ^ y) & 1:
                fb[base + x] = 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_conchars_font.py`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add quake/conchars.py tests/test_conchars_font.py
git commit -m "conchars: ConFont glyph blitter + qpic/conback/fade helpers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 2: Wire `ConFont` + conback into the Client

**Files:**
- Modify: `client.py` (imports near the other `from quake.*` lines; `__init__` after `client.py:172`)

This task only constructs the objects; the drawing/mode-gating is Task 3. No new test — Task 3's integration test covers it. Verify the import/load does not crash boot.

- [ ] **Step 1: Add the import**

In `client.py`, alongside the existing `from quake.sbar import Sbar, SBAR_LINES` (near `client.py:25`), add:

```python
from quake.conchars import ConFont, load_qpic, blit_conback, fade_region
```

- [ ] **Step 2: Construct `ConFont` + load conback in `__init__`**

In `client.py`, immediately after `self.sbar = Sbar(Wad(self.pak.read("gfx.wad")))` (`client.py:172`), add:

```python
        # conchars UI text composited into the zbuf framebuffer (centerprint,
        # console, menu) -- the real Quake bitmap font, like the sbar. Reuses
        # the lump the Sbar already loaded; conback is the console backdrop.
        self.confont = ConFont(self.sbar.conchars)
        self.conback = load_qpic(self.pak.read("gfx/conback.lmp"))
```

- [ ] **Step 3: Verify boot does not crash**

Run: `PQ_AUDIO=0 python -c "from client import Client; c=Client('e1m1'); c.resize(640,480); print('w',c.conback[0],'h',c.conback[1])"`
Expected: prints `w 320 h 200`.

- [ ] **Step 4: Commit**

```bash
git add client.py
git commit -m "client: build ConFont + load conback for zbuf UI compositing

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 3: Composite the UI into the zbuf framebuffer + mode-gate the overlays

**Files:**
- Modify: `client.py` — add `Client._composite_zbuf_ui`; call it in the zbuf render block; mode-gate the centerprint/intermission/console/menu emission in `frame()`.
- Test: `tests/test_conchars_ui.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/test_conchars_ui.py`:

```python
"""Integration test: in zbuf (textured) mode the Client composites centerprint,
console, and menu into the framebuffer with the conchars font instead of
emitting them as OS-native overlays; non-zbuf modes keep the overlay path.

Boots the full engine stack -- needs quake-shareware/id1/pak0.pak. Each check
renders the SAME frame twice with dt=0 (so sv.time and the scene are identical)
toggling one UI element, and asserts the framebuffer changed -- proof the
element was composited -- while the RenderFrame carries no OS overlay for it."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from client import Client, InputState


def _client():
    c = Client("e1m1")
    c.resize(640, 480)
    c.frame(0.0, InputState())              # settle one frame
    return c


def test_centerprint_composited_not_overlaid_in_zbuf():
    c = _client()
    assert c.mode == "zbuf"
    c.sv.center_msg = None
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.sv.center_msg = ("HELLO QUAKE", c.sv.time)
    rf = c.frame(0.0, InputState())
    # framebuffer changed -> centerprint was drawn into it
    assert bytes(rf.framebuffer[0]) != base
    # ...and no OS-native center overlay was emitted
    assert all(o[4] != "center" for o in rf.overlays)


def test_centerprint_overlaid_in_flat_mode():
    c = _client()
    c.frame(0.0, InputState(commands=frozenset({"flat"})))
    assert c.mode == "flat"
    c.sv.center_msg = ("HELLO QUAKE", c.sv.time)
    rf = c.frame(0.016, InputState())
    assert any(o[4] == "center" and "HELLO" in o[2] for o in rf.overlays)


def test_console_composited_not_overlaid_in_zbuf():
    c = _client()
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.con.active = True
    c.con.print("test console line")
    rf = c.frame(0.0, InputState())
    assert rf.console is None               # not handed to the frontend
    assert bytes(rf.framebuffer[0]) != base  # drawn into the framebuffer


def test_menu_composited_not_overlaid_in_zbuf():
    c = _client()
    base = bytes(c.frame(0.0, InputState()).framebuffer[0])
    c.menu.active = True
    rf = c.frame(0.0, InputState())
    assert rf.menu is None
    assert bytes(rf.framebuffer[0]) != base


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_conchars_ui.py`
Expected: FAIL — `test_centerprint_composited_not_overlaid_in_zbuf` asserts `framebuffer changed`, but today the centerprint is only an overlay, so the framebuffer is unchanged (`base == rf.framebuffer[0]`) and the overlay assertion also fails because a `"center"` overlay IS present.

- [ ] **Step 3: Add `_composite_zbuf_ui`**

In `client.py`, add this method to `Client` (place it just before `frame`, near `client.py:948`):

```python
    def _composite_zbuf_ui(self, fb, vw, vh):
        """zbuf mode: draw centerprint/intermission, console, and menu into the
        framebuffer with the conchars font -- the real Quake bitmap UI, drawn
        like the sbar -- instead of handing them to the frontend as OS-native
        overlays. vw/vh are the view region (vh excludes the appended sbar rows).
        Ports SCR_DrawCenterString, Con_DrawConsole/Con_DrawInput, and the
        menu's M_Print/cursor spinner."""
        cf = self.confont

        # centerprint or the intermission stats panel, centered in the view.
        ist = self.sv.intermission_stats() if self.intermission else None
        block = None
        if ist:
            mins, secs = divmod(ist["time"], 60)
            block = ("LEVEL COMPLETE\n\n"
                     f"Time      {mins}:{secs:02d}\n"
                     f"Secrets   {ist['secrets']} / {ist['total_secrets']}\n"
                     f"Kills     {ist['monsters']} / {ist['total_monsters']}")
        else:
            cm = self.sv.center_msg
            if cm and self.sv.time - cm[1] < CENTER_MSG_TIME:
                block = cm[0]
        if block:
            lines = block.split("\n")
            y0 = int(0.35 * vh) - len(lines) * 4
            for i, ln in enumerate(lines):
                cf.text_centered(fb, vw, vw // 2, y0 + i * 8, ln)

        # console: conback backdrop over the top ~40%, text + flashing cursor.
        con = self.con
        if con.active:
            panel = vh * 2 // 5
            blit_conback(fb, vw, vh, self.conback, panel)
            con.width = max(20, vw // 8)
            rows = max(1, panel // 8 - 2)
            y = 0
            for ln in con.view_lines(rows):
                cf.text(fb, vw, 0, y, ln)
                y += 8
            cf.text(fb, vw, 0, y, "]" + con.input)
            if int(self.sv.time * 4) & 1:               # Con_DrawInput cursor
                cf.char(fb, vw, (con.cursor + 1) * 8, y, 11)

        # menu: dim the view (Draw_FadeScreen), then title + rows; the selected
        # row gets the spinning cursor (conchars 12/13).
        if self.menu.active:
            title, rows = self.menu.view()
            fade_region(fb, vw, 0, 0, vw, vh)
            cx = vw // 2
            y = vh // 4
            cf.text_centered(fb, vw, cx, y, title)
            y += 16
            col = cx - 80
            for label, value, sel in rows:
                if sel:
                    cf.char(fb, vw, col - 16, y, 12 + (int(self.sv.time * 4) & 1))
                cf.text(fb, vw, col, y, label)
                if value:
                    cf.text(fb, vw, cx + 16, y, value)
                y += 8
```

- [ ] **Step 4: Call it in the zbuf render block**

In `client.py`, in the `if self.mode == "zbuf":` block, after the sbar handling that ends with `framebuffer = fbdata = (fb, vw, full_h)` (`client.py:1165`), append the composite call. Replace this block (`client.py:1158-1165`):

```python
            if self.rend.sbar_lines:
                fb, vw, vh = fbdata
                fb.extend(bytes(vw * self.rend.sbar_lines))   # the bar rows
                full_h = vh + self.rend.sbar_lines
                if st:
                    self.sbar.draw(fb, vw, full_h, st, self.sv.time,
                                   self.item_gettime, self.faceanimtime)
                framebuffer = fbdata = (fb, vw, full_h)
```

with:

```python
            fb, vw, vh = fbdata                            # view region (pre-sbar)
            if self.rend.sbar_lines:
                fb.extend(bytes(vw * self.rend.sbar_lines))   # the bar rows
                full_h = vh + self.rend.sbar_lines
                if st:
                    self.sbar.draw(fb, vw, full_h, st, self.sv.time,
                                   self.item_gettime, self.faceanimtime)
                framebuffer = fbdata = (fb, vw, full_h)
            self._composite_zbuf_ui(fb, vw, vh)           # conchars UI overlay
```

- [ ] **Step 5: Mode-gate the overlay/console/menu emission in `frame()`**

In `client.py`, replace the intermission + centerprint + console + menu block (`client.py:1247-1267`):

```python
        ist = self.sv.intermission_stats() if self.intermission else None
        if ist:
            mins, secs = divmod(ist["time"], 60)
            panel = ("LEVEL COMPLETE\n\n"
                     f"Time      {mins}:{secs:02d}\n"
                     f"Secrets   {ist['secrets']} / {ist['total_secrets']}\n"
                     f"Kills     {ist['monsters']} / {ist['total_monsters']}")
            overlays.append((w // 2, h // 3, panel, (255, 255, 0), "center"))

        cm = self.sv.center_msg
        if not ist and cm and self.sv.time - cm[1] < CENTER_MSG_TIME:
            overlays.append((w // 2, h // 3, cm[0], (255, 255, 0), "center"))

        con = self.con
        console = None
        if con.active:
            con.width = max(20, w // 9)           # ~9px per monospace cell at the HUD size
            rows = max(1, (h * 2 // 5) // 16 - 1)  # panel is ~40% tall, ~16px lines
            console = (con.view_lines(rows), "]" + con.input, con.cursor + 1)

        menu = self.menu.view() if self.menu.active else None
```

with (centerprint/intermission/console/menu go to the framebuffer in zbuf mode via `_composite_zbuf_ui`, so emit OS overlays only otherwise):

```python
        # In zbuf mode these were composited into the framebuffer by
        # _composite_zbuf_ui; only the wire/flat overlay path emits them here.
        console = None
        menu = None
        if self.mode != "zbuf":
            ist = self.sv.intermission_stats() if self.intermission else None
            if ist:
                mins, secs = divmod(ist["time"], 60)
                panel = ("LEVEL COMPLETE\n\n"
                         f"Time      {mins}:{secs:02d}\n"
                         f"Secrets   {ist['secrets']} / {ist['total_secrets']}\n"
                         f"Kills     {ist['monsters']} / {ist['total_monsters']}")
                overlays.append((w // 2, h // 3, panel, (255, 255, 0), "center"))

            cm = self.sv.center_msg
            if not ist and cm and self.sv.time - cm[1] < CENTER_MSG_TIME:
                overlays.append((w // 2, h // 3, cm[0], (255, 255, 0), "center"))

            con = self.con
            if con.active:
                con.width = max(20, w // 9)           # ~9px per monospace cell at the HUD size
                rows = max(1, (h * 2 // 5) // 16 - 1)  # panel is ~40% tall, ~16px lines
                console = (con.view_lines(rows), "]" + con.input, con.cursor + 1)

            menu = self.menu.view() if self.menu.active else None
```

- [ ] **Step 6: Run the integration test to verify it passes**

Run: `PQ_AUDIO=0 python tests/test_conchars_ui.py`
Expected: `OK`.

- [ ] **Step 7: Run the font test + the broader suite for regressions**

Run: `PQ_AUDIO=0 python tests/test_conchars_font.py && PQ_AUDIO=0 python tests/test_console_client.py && PQ_AUDIO=0 python tests/test_intermission_stats.py`
Expected: each prints `OK` (no assertion failures).

- [ ] **Step 8: Commit**

```bash
git add client.py tests/test_conchars_ui.py
git commit -m "client: composite centerprint/console/menu into the zbuf framebuffer

In textured mode draw the UI text with the conchars bitmap font (like the
sbar / real Quake) instead of OS-native overlays; wire/flat keep the overlay
path. conback backdrop for the console, Draw_FadeScreen dim for the menu.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Task 4: Manual smoke + full-suite check

**Files:** none (verification only)

- [ ] **Step 1: Full test suite (muted)**

Run: `export PQ_AUDIO=0; for t in tests/test_*.py; do echo "== $t"; python "$t" || break; done`
Expected: every test prints `OK` (the zbuffer raster goldens are unaffected — the UI composite only runs through the new path, but confirm `tests/test_zbuffer_raster.py` still passes; if it needs goldens, run it once with `--regen` only if its scene legitimately changed — it should NOT, since the test scene has no active console/menu/centerprint).

- [ ] **Step 2: Eyeball it (optional, if a display is available)**

Run: `python main.py e1m1`, open the console (`F1`/`~`), the escape menu, and walk over a trigger to see a centerprint. Confirm the text now renders in the blocky conchars font baked into the scene (not the OS font), with the conback behind the console and the scene dimmed behind the menu.

- [ ] **Step 3: Final commit (if Step 1 surfaced any golden/notes fixups)**

Only if changes were needed:

```bash
git add -A
git commit -m "tests: regen/adjust goldens for conchars zbuf UI (if needed)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** ConFont/centering (Task 1) ✓; conback console background (Task 1 `blit_conback` + Task 3 wiring) ✓; Draw_FadeScreen menu dim (Task 1 `fade_region` + Task 3) ✓; centerprint + intermission via conchars (Task 3) ✓; flashing cursor char 11 / menu spinner 12-13 (Task 3) ✓; mode-gated overlay suppression — `console=None`, `menu=None`, no center overlay (Task 3 Step 5) ✓; reuse `sbar.conchars`, no second wad read (Task 2) ✓; out-of-scope HUD/profiler/status overlays untouched (Task 3 Step 5 leaves the HUD/status `overlays.append` calls above the block in place) ✓; `sv.time` animation clock ✓; tests for both the font and the mode-gated integration ✓.
- **Type/name consistency:** `ConFont(self.sbar.conchars)`, `load_qpic`/`blit_conback`/`fade_region` signatures match between `quake/conchars.py` (Task 1) and the `client.py` call sites (Tasks 2-3). `con.view_lines(rows)`, `con.input`, `con.cursor`, `con.width`, `con.active`, `menu.view()` → `(title, [(label, value, sel), …])`, `sv.center_msg`, `sv.intermission_stats()` all match the existing code read during planning.
- **Note:** the HUD debug overlay (green fps/pos) and bottom status string remain OS overlays in all modes — in zbuf they coexist with the conchars console/menu, which is intended (they are diagnostic, not game UI).
