# VGA Pixel-Aspect (CRT) Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `pixel_aspect` toggle (cvar + video-menu item, default 1.0/square) that renders the zbuf view with `yfocal = focal * pixel_aspect` (R_ViewChanged's `yscale = xscale * pixelAspect`) and has every frontend display the framebuffer stretched to `h / pixel_aspect` rows (the CRT stretch), so 320×200 at 5/6 shows as authentic 4:3.

**Architecture:** Renderer gains a `pixel_aspect` attribute consumed only inside `render_zbuffer` (the y-projection family `hh - cy * focal * iz`); texture/depth gradients self-adapt because `plane_gradients` works from the projected screen coords. The client persists `_pixel_aspect` across maps like `_zbuf_scale`, exposes it via cvar + menu, and stamps it on `RenderFrame`. Frontends stretch at display time: Cocoa/GDI scale the letterbox destination rect; tkinter duplicates framebuffer rows pre-expansion (PhotoImage zoom is integer-only).

**Tech Stack:** Pure stdlib; existing standalone-script tests (`PQ_AUDIO=0`, `_bootstrap`, prints `OK`).

**Spec:** `docs/superpowers/specs/2026-06-12-pixel-aspect-design.md`. Reference: `quake-source/WinQuake/r_main.c` `R_ViewChanged` (`yscale = xscale * pixelAspect`; comment "proper 320*200 pixelAspect = 0.8333333"). Cite it per repo convention. Commits to master, each ending:

```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

---

### Task 1: Renderer `pixel_aspect` (yfocal in the zbuf path)

**Files:**
- Modify: `quake/render.py` (`__init__` ~line 344 next to `video_res`/`sbar_lines`; `render_zbuffer` lines ~1795 and the y-projection sites listed below)
- Test: `tests/test_pixel_aspect.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the VGA pixel-aspect toggle: yfocal in the zbuf renderer
(R_ViewChanged's yscale = xscale * pixelAspect), client plumbing (cvar,
menu, RenderFrame), and that wire/flat stay square. Boots the shareware
stack like the other client tests."""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

import client

CRT = 5.0 / 6.0


def _capture_zbuf_args(c):
    """Run frames, capture render_zbuffer's args so the same instant can be
    re-rendered with different renderer settings (lightstyles animate with
    sv.time, so two frame() calls are never pixel-comparable)."""
    captured = {}
    orig = c.rend.render_zbuffer
    def spy(*a, **kw):
        captured["a"], captured["kw"] = list(a), dict(kw)
        return orig(*a, **kw)
    c.rend.render_zbuffer = spy
    c.frame(0.016, client.InputState())
    c.rend.render_zbuffer = orig
    return captured["a"], captured["kw"]


def _gun_rows(rend, a, kw):
    """Diff a render with and without the view model: the rows the gun
    occupies. view_model is positional arg 5 or the kw."""
    (fb1, w, h), _ = rend.render_zbuffer(*a, **kw)
    if "view_model" in kw:
        kw2 = dict(kw, view_model=None); a2 = a
    else:
        a2 = a[:5] + [None] + a[6:]; kw2 = kw
    (fb2, _w, _h), _ = rend.render_zbuffer(*a2, **kw2)
    fb1, fb2 = bytes(fb1), bytes(fb2)
    return [y for y in range(h) if fb1[y*w:(y+1)*w] != fb2[y*w:(y+1)*w]]


def test_crt_aspect_widens_vertical_fov():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res((320, 200))
    a, kw = _capture_zbuf_args(c)
    rows_square = _gun_rows(c.rend, a, kw)
    c.rend.pixel_aspect = CRT
    rows_crt = _gun_rows(c.rend, a, kw)
    c.rend.pixel_aspect = 1.0
    # wider vertical FOV -> more of the gun (which hangs off the bottom
    # edge) survives the bottom clip
    assert rows_crt and rows_square
    assert min(rows_crt) < min(rows_square)
    assert len(rows_crt) > len(rows_square)


def test_wire_mode_ignores_pixel_aspect():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "wire"
    rf1 = c.frame(0.016, client.InputState())
    # re-render the same camera through the painter path directly
    segs1, _ = c.rend.render((c.pos[0], c.pos[1], c.pos[2] + 22), c.yaw, c.pitch)
    c.rend.pixel_aspect = CRT
    segs2, _ = c.rend.render((c.pos[0], c.pos[1], c.pos[2] + 22), c.yaw, c.pitch)
    c.rend.pixel_aspect = 1.0
    assert segs1 == segs2


if __name__ == "__main__":
    test_crt_aspect_widens_vertical_fov()
    test_wire_mode_ignores_pixel_aspect()
    print("OK")
```

(Adjust the `c.rend.render(...)` call in the wire test to the real signature
— check `Renderer.render` at `quake/render.py:1161`; the point is two
identical calls bracketing a `pixel_aspect` change produce identical segs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py`
Expected: `AttributeError` — `Renderer` has no `pixel_aspect` (or identical
rows if defaulted): the renderer doesn't know the attribute yet.

- [ ] **Step 3: Implement**

In `Renderer.__init__` next to `self.sbar_lines = 0`:

```python
        self.pixel_aspect = 1.0   # vertical/horizontal pixel size ratio for
                                  # the zbuf view (R_ViewChanged: yscale =
                                  # xscale * pixelAspect; 5/6 is the "proper
                                  # 320*200" VGA value). 1.0 = square pixels.
```

In `render_zbuffer` right after `focal = self.focal * iw / self.width`
(~line 1795):

```python
        yfocal = focal * self.pixel_aspect            # R_ViewChanged yscale
```

Then replace `focal` with `yfocal` in the **y projections only**, inside
`render_zbuffer` (all are `hh - <expr> * focal * iz` or `* focal / cz`):
lines ~1857, ~1909, ~1994, ~2050, ~2137, ~2182, ~2595, ~2630. Leave the x
projections (`hw + ...`), the sprite/particle size scales (`scale = focal *
iz` ~2596, `pscale = focal * PARTICLE_ZBUF_RADIUS` ~2621 — extents stay
square per the spec) and everything before line 1754 (`render`/
`render_shaded`, the painter paths) untouched. Grep the body of
`render_zbuffer` for any remaining `focal` after the edit and justify each
survivor (it should be only the size scales and the `yfocal` definition).

- [ ] **Step 4: Run tests**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_sbar_client.py` → `OK` (default 1.0
changes nothing)
Run: `PQ_AUDIO=0 python tests/test_zbuffer_raster.py` → `OK` (goldens
rendered at the default must not shift)

- [ ] **Step 5: Commit**

```bash
git add quake/render.py tests/test_pixel_aspect.py
git commit -m "render: pixel_aspect scales the zbuf vertical projection (R_ViewChanged yscale)"
```

---

### Task 2: Client plumbing — cvar, menu item, RenderFrame field

**Files:**
- Modify: `client.py` (constants ~line 50; `RenderFrame` ~line 127; `__init__`/`_load_map` ~line 215-265; `_build_menu` ~line 730; `_register_console` ~line 744; `frame()` return ~line 1195)
- Test: append to `tests/test_pixel_aspect.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pixel_aspect.py` (and the `__main__` block):

```python
def test_renderframe_reports_pixel_aspect():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.mode = "zbuf"
    c.set_video_res((320, 200))
    rf = c.frame(0.016, client.InputState())
    assert rf.pixel_aspect == 1.0                  # default: square
    c.con.execute("pixel_aspect 0.8333333")
    rf = c.frame(0.016, client.InputState())
    assert abs(rf.pixel_aspect - CRT) < 1e-3
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3   # live renderer updated
    c.mode = "wire"
    rf = c.frame(0.016, client.InputState())
    assert rf.pixel_aspect == 1.0                  # wire never stretches


def test_pixel_aspect_persists_across_map_change():
    c = client.Client("e1m1")
    c.resize(640, 400)
    c.con.execute("pixel_aspect 0.8333333")
    c._cmd_map(["e1m1"])                           # rebuilds the Renderer
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3


def test_pixel_aspect_clamped():
    c = client.Client("e1m1")
    c.con.execute("pixel_aspect 0.1")
    assert c.rend.pixel_aspect == 0.5
    c.con.execute("pixel_aspect 3")
    assert c.rend.pixel_aspect == 1.0


def test_menu_aspect_item_drives_client():
    c = client.Client("e1m1")
    menu_items = {getattr(i, "label", None): i for i in c.menu.items}
    item = menu_items.get("Aspect")
    assert item is not None
    # drive the choice to CRT through the menu's own callback, as the
    # frontend would; copy how test_video_menu.py exercises Resolution
    item.on_change(item.choices[1][1])
    assert abs(c.rend.pixel_aspect - CRT) < 1e-3
```

(Two adaptation points, keep the intent: `c.con.execute(...)` — check
`quake/console.py` for the real entry point for running a command line
(`execute`/`submit`/`run_line`); and the menu test — mirror however
`tests/test_video_menu.py` drives the Resolution `ChoiceItem`, including the
real attribute names for items/choices/callback.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py`
Expected: first new failure — `RenderFrame` has no `pixel_aspect` /
unknown cvar `pixel_aspect`.

- [ ] **Step 3: Implement in client.py**

(a) Constants, near `VIDEO_MODES`:

```python
# Pixel aspect for the textured mode: vertical/horizontal pixel size. CRT is
# R_ViewChanged's "proper 320*200 pixelAspect = 0.8333333" -- VGA mode 13h
# pixels were 5/6 as wide as tall on a 4:3 monitor.
ASPECT_MODES = [("Square", 1.0), ("CRT", 5.0 / 6.0)]
```

(b) `RenderFrame` gains a field (with the others, after `crosshair`):

```python
    pixel_aspect: float = 1.0  # zbuf: display rows taller by 1/this (CRT look)
```

and a one-line addition to its docstring ("pixel_aspect asks the frontend to
display the framebuffer stretched to h/pixel_aspect rows").

(c) `__init__`, next to `self._zbuf_scale = ZBUF_SCALE`:

```python
        self._pixel_aspect = 1.0    # zbuf vertical pixel aspect, persists
                                    # across maps like _zbuf_scale
```

(d) `_load_map`, next to `self.rend.zbuf_scale = self._zbuf_scale`:

```python
        self.rend.pixel_aspect = self._pixel_aspect  # keep the chosen aspect
```

(e) Setter, near `set_video_res`:

```python
    def set_pixel_aspect(self, v):
        """Vertical pixel aspect for the zbuf view (1.0 square, 5/6 VGA CRT);
        clamped to a sane range. Takes effect next frame -- the projection
        reads it live, no framebuffer rebuild needed."""
        v = max(0.5, min(1.0, float(v)))
        self._pixel_aspect = v
        self.rend.pixel_aspect = v
```

(f) Cvar in `_register_console`, following the exact `register_cvar` pattern
used by `zbuf_scale` (~line 782 — match its signature/on_change shape):

```python
        con.register_cvar("pixel_aspect", self._pixel_aspect,
                          on_change=lambda v: self.set_pixel_aspect(v),
                          help="zbuf pixel aspect: 1.0 square, 0.8333 VGA CRT")
```

(g) Menu in `_build_menu`, mirroring the Resolution item:

```python
        aidx = next((i for i, (_, v) in enumerate(ASPECT_MODES)
                     if v == self._pixel_aspect), 0)
        aspect = ChoiceItem("Aspect", ASPECT_MODES, aidx, self.set_pixel_aspect)
        ...
        return Menu("VIDEO OPTIONS", [res, aspect, back, quit_item])
```

(h) `frame()` return: add to the `RenderFrame(...)` constructor call:

```python
                           pixel_aspect=(self._pixel_aspect
                                         if self.mode == "zbuf" else 1.0),
```

- [ ] **Step 4: Run tests**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_video_menu.py` → `OK` (menu gained an
item — check that file for assertions on the item list/count and update the
expected items if it pins them)
Run: `PQ_AUDIO=0 python tests/test_console_client.py` → `OK`

- [ ] **Step 5: Commit**

```bash
git add client.py tests/test_pixel_aspect.py tests/test_video_menu.py
git commit -m "client: pixel_aspect cvar + video-menu Aspect item, stamped on RenderFrame"
```

---

### Task 3: Frontend display stretch (Cocoa, GDI, tkinter)

**Files:**
- Modify: `mac_cocoa.py` (~line 233, the `letterbox_rect(fw, fh, w, h)` call)
- Modify: `win_gdi.py` / `win_ui.py` (the `letterbox_rect(w, h, dst_w, dst_h)` call at `win_ui.py:332` inside `GdiBlitter` — thread the aspect in from where `win_gdi` hands over the frame)
- Modify: `main.py` (tk: `expand_fb_to_ppm` call site ~line 671 and `fb_fit` use ~line 675; new pure helper `aspect_row_map`)
- Test: append to `tests/test_mac_ui.py`-style pure tests in `tests/test_pixel_aspect.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pixel_aspect.py` (and the `__main__` block):

```python
def test_letterbox_stretched_height_fills_4_3():
    # 320x200 at CRT aspect displays as 320x240 art: in an 800x600 window the
    # letterbox must fill it edge-to-edge (4:3 in 4:3)
    import mac_ui
    disp_h = round(200 / CRT)                  # 240
    ox, oy, ow, oh = mac_ui.letterbox_rect(320, disp_h, 800, 600)
    assert (ox, oy, ow, oh) == (0, 0, 800, 600)


def test_aspect_row_map():
    import main as tkmain
    m = tkmain.aspect_row_map(200, CRT)
    assert len(m) == 240                       # 200 rows shown as 240
    assert m[0] == 0 and m[-1] == 199
    assert all(m[i] <= m[i+1] for i in range(len(m) - 1))   # monotonic
    assert all(0 <= r < 200 for r in m)
    counts = [m.count(r) for r in range(200)]
    assert set(counts) <= {1, 2}               # each row once or twice
    assert tkmain.aspect_row_map(200, 1.0) is None          # square: no-op
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py`
Expected: `AttributeError: module 'main' has no attribute 'aspect_row_map'`
(the letterbox test may already pass — `letterbox_rect` is generic; that
test pins the convention the frontends rely on).

- [ ] **Step 3: Implement**

(a) `main.py` — pure helper next to `expand_fb_to_ppm` (~line 86):

```python
def aspect_row_map(h, pixel_aspect):
    """Source-row indices that stretch h framebuffer rows to h/pixel_aspect
    display rows by duplication (nearest row) -- the CRT vertical stretch for
    the integer-zoom-only Tk path. None when square (no work to do)."""
    if pixel_aspect >= 1.0:
        return None
    out_h = round(h / pixel_aspect)
    return [min(h - 1, int(y * pixel_aspect)) for y in range(out_h)]
```

In the tk draw path (~line 671), before `expand_fb_to_ppm`:

```python
        rmap = aspect_row_map(h, rf.pixel_aspect)
        if rmap:
            fb = b"".join(fb[r * w:(r + 1) * w] for r in rmap)
            h = len(rmap)
        ppm = expand_fb_to_ppm(fb, w, h, self._pal_r, self._pal_g, self._pal_b)
```

(everything downstream — `fb_fit`, zoom, centring — already works from `h`,
so the stretch rides through; adapt variable names to the real ones at the
call site).

(b) `mac_cocoa.py` (~line 233): stretch the destination rect —

```python
            disp_h = round(fh / rf.pixel_aspect) if rf.pixel_aspect < 1.0 else fh
            ox, oy, ow, oh = letterbox_rect(fw, disp_h, w, h)
```

CoreGraphics scales the CGImage into whatever rect it's drawn to, so no
other change. (`rf` here is whatever the draw path calls its stored
RenderFrame — adapt the name; if only the fb tuple is stored, store the
aspect alongside it where the frame is handed to the view.)

(c) `win_gdi.py`/`win_ui.py`: `GdiBlitter` letterboxes at `win_ui.py:332`
(`letterbox_rect(w, h, dst_w, dst_h)`) and `StretchDIBits` scales into the
rect. Thread the aspect to that call (e.g. a `pixel_aspect=1.0` parameter on
the blit method, passed by `win_gdi` from the RenderFrame) and use
`round(h / pixel_aspect)` as the src-height argument to `letterbox_rect`
only — `StretchDIBits` must still be told the *real* source height for the
DIB, just a taller destination rect. Keep the parameter defaulted so
existing callers/tests are untouched. This is a Windows path that can't run
on this Mac — keep the change minimal and mechanical, and rely on
`tests/test_win_ui.py`'s import-free pure-helper convention: if
`letterbox_rect` itself needs no change (it doesn't — it's generic), only
the call site moves, which `tests/smoke_win_gdi.py` covers on a real Windows
box later.

- [ ] **Step 4: Run tests**

Run: `PQ_AUDIO=0 python tests/test_pixel_aspect.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_mac_ui.py` → `OK`
Run: `PQ_AUDIO=0 python tests/test_fb_scale.py` → `OK`
Full suite: `export PQ_AUDIO=0; for t in tests/test_*.py; do python "$t" >/dev/null 2>&1 || echo "FAIL $t"; done` → no FAIL lines.

- [ ] **Step 5: Commit**

```bash
git add main.py mac_cocoa.py win_gdi.py win_ui.py tests/test_pixel_aspect.py
git commit -m "frontends: display the zbuf framebuffer stretched by pixel_aspect (CRT 4:3)"
```

---

### Task 4: Eyeball it

- [ ] **Step 1: Headless A/B captures**

Render the same scene at `pixel_aspect` 1.0 and 5/6 (with the display
stretch applied to the PNG rows for the CRT one, duplicating rows via
`aspect_row_map`) and compare side by side: CRT shows more world vertically,
the gun is more visible, the status bar is taller, nothing looks squashed.

- [ ] **Step 2: Run the game**

`python main.py e1m1`, open the console/menu: `pixel_aspect 0.8333` and the
menu Aspect item both switch live; Square restores the old look exactly.

- [ ] **Step 3: Fixups commit if needed**
