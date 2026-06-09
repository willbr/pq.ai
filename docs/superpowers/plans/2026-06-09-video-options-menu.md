# Video Options Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Escape-opened, keyboard-driven overlay menu whose Video Options section sets the textured rasteriser's internal framebuffer to a fixed resolution (240x160 / 320x240 default / 640x480), stretched to fill the window.

**Architecture:** Mirror the existing console split — a pure, UI-agnostic state machine (`quake/menu.py`) owned by the UI-agnostic `Client`, exposed on `RenderFrame.menu`; the gdi32 frontend (`win_gdi.py`) routes native keys into it and draws it via a new `GdiBlitter.draw_menu`. The renderer gains a `video_res` override that fixes the z-buffer framebuffer size; `present()` already `StretchDIBits` it to the window.

**Tech Stack:** Python 3.13 stdlib only; ctypes/gdi32 for the Windows frontend. Tests are standalone `test_*.py` scripts that print `OK` (no pytest).

**Reference spec:** `docs/superpowers/specs/2026-06-09-video-options-menu-design.md`

**Conventions to follow:**
- Inside `quake/` use **relative** imports; root files (`client.py`, `win_gdi.py`) use **absolute** (`from quake.menu import ...`).
- Each `test_*.py` ends with an `if __name__ == "__main__":` block that calls every `test_*` function and prints `OK`.
- Run a single test with `python test_foo.py`; tests boot the real shareware stack, so `quake-shareware/id1/pak0.pak` must be present.
- The gdi32 frontend and its GDI drawing are Windows-only and not unit-tested (same as the existing console panel) — they are verified manually.

---

## Task 1: Renderer fixed-resolution override (`video_res`)

**Files:**
- Modify: `quake/render.py` (`Renderer.__init__` ~line 204, `Renderer._setup_zbuf` ~line 584)
- Test: `test_video_menu.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_video_menu.py`:

```python
"""Tests for the video-options resolution path: the Renderer's video_res
override (fixed z-buffer framebuffer size) and the Client's video state +
menu wiring. Boots the real shareware stack, like the other client tests."""

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, ZBUF_SCALE


def _palette(pak):
    pal = pak.read("gfx/palette.lmp")
    return [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]


def test_renderer_video_res_fixes_framebuffer():
    pak = Pak("quake-shareware/id1/pak0.pak")
    bsp = Bsp(pak.read("maps/e1m1.bsp"))
    rend = Renderer(bsp, _palette(pak))
    # default: video_res None -> behaves as before (window // zbuf_scale)
    assert rend.video_res is None
    rend.resize(800, 600)
    assert rend.zw == 800 // ZBUF_SCALE and rend.zh == 600 // ZBUF_SCALE
    # set a fixed resolution -> framebuffer is exactly that, ignoring zbuf_scale
    rend.video_res = (320, 240)
    rend.resize(800, 600)
    assert rend.zw == 320 and rend.zh == 240
    assert len(rend._zb_zero) == 320 * 240 * 4
    # a different window size keeps the fixed buffer
    rend.resize(1024, 768)
    assert rend.zw == 320 and rend.zh == 240


if __name__ == "__main__":
    test_renderer_video_res_fixes_framebuffer()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_video_menu.py`
Expected: FAIL — `AttributeError: 'Renderer' object has no attribute 'video_res'`.

- [ ] **Step 3: Add the `video_res` attribute**

In `quake/render.py` `__init__`, immediately after the `self.zbuf_scale = ZBUF_SCALE` line (~line 204), add:

```python
        # Fixed textured render resolution: when set to (w, h) the z-buffer
        # framebuffer is exactly that size (stretched to the window on present),
        # overriding the zbuf_scale-derived size. None = derive from the window
        # (today's behaviour); the video-options menu sets a fixed mode.
        self.video_res = None
```

- [ ] **Step 4: Honour `video_res` in `_setup_zbuf`**

In `quake/render.py` `_setup_zbuf` (~line 589), replace these two lines:

```python
        self.zw = max(1, self.width // self.zbuf_scale)
        self.zh = max(1, self.height // self.zbuf_scale)
```

with:

```python
        if self.video_res is not None:
            self.zw, self.zh = self.video_res        # fixed mode (video menu)
        else:
            self.zw = max(1, self.width // self.zbuf_scale)
            self.zh = max(1, self.height // self.zbuf_scale)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python test_video_menu.py`
Expected: `OK`

- [ ] **Step 6: Confirm the existing renderer test still passes**

Run: `python test_console_client.py`
Expected: `OK` (the `video_res is None` default keeps `test_renderer_zbuf_scale_is_live` green).

- [ ] **Step 7: Commit**

```bash
git add quake/render.py test_video_menu.py
git commit -m "render: add video_res override for a fixed z-buffer framebuffer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Pure menu state machine (`quake/menu.py`)

**Files:**
- Create: `quake/menu.py`
- Test: `test_menu.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_menu.py`:

```python
"""Tests for the pure overlay-menu state machine (quake/menu.py): navigation,
choice cycling firing its callback, action items firing on Enter, and the
view() the frontend draws. No boot -- the menu is pure stdlib."""

from quake.menu import Menu, ChoiceItem, ActionItem


def _menu():
    picked = []
    quit_flag = []
    res = ChoiceItem("Resolution",
                     [("240x160", (240, 160)), ("320x240", (320, 240)),
                      ("640x480", (640, 480))],
                     index=1, on_select=picked.append)
    back = ActionItem("Back", lambda: picked.append("back"))
    quit_item = ActionItem("Quit", lambda: quit_flag.append(True))
    return Menu("VIDEO OPTIONS", [res, back, quit_item]), picked, quit_flag


def test_navigation_wraps():
    m, _, _ = _menu()
    assert m.selected == 0
    m.key_up()                       # wraps to last
    assert m.selected == 2
    m.key_down()                     # wraps back to first
    assert m.selected == 0
    m.key_down()
    assert m.selected == 1


def test_choice_cycles_and_fires_on_select():
    m, picked, _ = _menu()
    # selected is the Resolution item (index 0), starting on 320x240 (option 1)
    m.key_right()
    assert m.items[0].index == 2 and picked[-1] == (640, 480)
    m.key_right()                    # wraps to first option
    assert m.items[0].index == 0 and picked[-1] == (240, 160)
    m.key_left()                     # wraps to last option
    assert m.items[0].index == 2 and picked[-1] == (640, 480)


def test_action_item_fires_on_enter():
    m, _, quit_flag = _menu()
    m.selected = 2                   # Quit
    m.key_enter()
    assert quit_flag == [True]


def test_escape_closes():
    m, _, _ = _menu()
    m.active = True
    m.key_escape()
    assert m.active is False


def test_view_reports_rows_and_selection():
    m, _, _ = _menu()
    m.selected = 1
    title, rows = m.view()
    assert title == "VIDEO OPTIONS"
    assert rows[0] == ("Resolution", "320x240", False)
    assert rows[1] == ("Back", "", True)
    assert rows[2] == ("Quit", "", False)


if __name__ == "__main__":
    test_navigation_wraps()
    test_choice_cycles_and_fires_on_select()
    test_action_item_fires_on_enter()
    test_escape_closes()
    test_view_reports_rows_and_selection()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_menu.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'quake.menu'`.

- [ ] **Step 3: Implement `quake/menu.py`**

Create `quake/menu.py`:

```python
"""Quake-style overlay menu: a pure, UI-agnostic state machine behind the
Escape menu. A `Menu` owns an ordered list of items and a selection cursor;
items are either a `ChoiceItem` (cycles a fixed option list, firing a callback)
or an `ActionItem` (fires a callback on Enter). It knows nothing about keycodes,
ctypes or GDI -- the frontend maps native keys onto the key_* methods and draws
what view() reports.

Pure stdlib, no OS/UI imports (same discipline as quake/console.py and
quake/perf.py). Single-thread by design: only the frame/input thread touches it."""


class ChoiceItem:
    """A menu row that cycles a fixed list of (label, value) options. Cycling
    (left/right, or Enter) advances the selection with wraparound and fires
    on_select(value) with the newly selected value."""

    def __init__(self, title, options, index, on_select):
        self.title = title
        self.options = options          # list of (label, value)
        self.index = index
        self.on_select = on_select

    @property
    def value_label(self):
        return self.options[self.index][0]

    def cycle(self, step):
        self.index = (self.index + step) % len(self.options)
        self.on_select(self.options[self.index][1])

    def activate(self):
        self.cycle(1)


class ActionItem:
    """A menu row that fires on_activate() when chosen with Enter. Left/right do
    nothing (no value to cycle)."""

    def __init__(self, title, on_activate):
        self.title = title
        self.on_activate = on_activate

    @property
    def value_label(self):
        return ""

    def cycle(self, step):
        pass

    def activate(self):
        self.on_activate()


class Menu:
    """An overlay menu: a title, an ordered item list, a selection cursor and an
    `active` flag the frontend toggles. key_* methods drive it; view() returns a
    draw-ready snapshot."""

    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected = 0
        self.active = False

    def key_up(self):
        self.selected = (self.selected - 1) % len(self.items)

    def key_down(self):
        self.selected = (self.selected + 1) % len(self.items)

    def key_left(self):
        self.items[self.selected].cycle(-1)

    def key_right(self):
        self.items[self.selected].cycle(1)

    def key_enter(self):
        self.items[self.selected].activate()

    def key_escape(self):
        self.active = False

    def view(self):
        """Draw-ready snapshot: (title, [(label, value_label, is_selected), ...])."""
        rows = [(it.title, it.value_label, i == self.selected)
                for i, it in enumerate(self.items)]
        return (self.title, rows)


if __name__ == "__main__":
    # smoke test: build a tiny menu and exercise it
    log = []
    m = Menu("T", [ChoiceItem("R", [("a", 1), ("b", 2)], 0, log.append),
                   ActionItem("Q", lambda: log.append("q"))])
    m.key_right(); m.key_down(); m.key_enter()
    assert log == [2, "q"], log
    print("OK")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_menu.py`
Expected: `OK`

- [ ] **Step 5: Confirm the module self-test runs**

Run: `python -m quake.menu`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add quake/menu.py test_menu.py
git commit -m "menu: pure UI-agnostic overlay-menu state machine

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Client video state, menu wiring, and RenderFrame field

**Files:**
- Modify: `client.py` (imports; `RenderFrame`; `Client.__init__`; `Client._load_map`; new `set_video_res`, `_build_menu`, `_menu_back`; `Client.frame`)
- Test: `test_video_menu.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `test_video_menu.py` (add `from client import Client` to the imports at the top of the file):

```python
def test_client_default_video_res_is_320x240():
    c = Client("e1m1")
    assert c.video_res == (320, 240)
    assert c.rend.video_res == (320, 240)
    assert c.rend.zw == 320 and c.rend.zh == 240


def test_set_video_res_rebuilds_buffer_immediately():
    c = Client("e1m1")
    c.resize(800, 600)
    c.set_video_res((640, 480))
    assert c.video_res == (640, 480)
    assert c.rend.zw == 640 and c.rend.zh == 480


def test_video_res_persists_across_map_change():
    c = Client("e1m1")
    c.set_video_res((240, 160))
    c._cmd_map(["e1m1"])             # rebuilds rend
    c.resize(800, 600)               # frontend resizes after a map load
    assert c.rend.video_res == (240, 160)
    assert c.rend.zw == 240 and c.rend.zh == 160


def test_menu_resolution_item_drives_client():
    c = Client("e1m1")
    c.resize(800, 600)
    # the first menu item is the Resolution choice, wired to set_video_res
    c.menu.selected = 0
    c.menu.key_right()               # cycle off the default (320x240) to 640x480
    assert c.video_res == (640, 480)
    assert c.rend.zw == 640 and c.rend.zh == 480
```

And register them in the `__main__` block of `test_video_menu.py`:

```python
if __name__ == "__main__":
    test_renderer_video_res_fixes_framebuffer()
    test_client_default_video_res_is_320x240()
    test_set_video_res_rebuilds_buffer_immediately()
    test_video_res_persists_across_map_change()
    test_menu_resolution_item_drives_client()
    print("OK")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_video_menu.py`
Expected: FAIL — `AttributeError: 'Client' object has no attribute 'video_res'`.

- [ ] **Step 3: Add the `VIDEO_MODES` constant and import**

In `client.py`, add to the `from quake.*` imports near the top (after the `from quake.console import Console` line):

```python
from quake.menu import Menu, ChoiceItem, ActionItem
```

Then add a module-level constant after the other module constants (e.g. after `CL_BOBUP = 0.5`):

```python
# Selectable textured-mode render resolutions for the video-options menu.
# "Auto" = derive from the window via zbuf_scale (today's behaviour, keeps the
# zbuf_scale cvar meaningful); the fixed modes set the framebuffer exactly.
VIDEO_MODES = [("Auto", None), ("240x160", (240, 160)),
               ("320x240", (320, 240)), ("640x480", (640, 480))]
DEFAULT_VIDEO_RES = (320, 240)
```

- [ ] **Step 4: Add the `menu` field to `RenderFrame`**

In `client.py` `RenderFrame`, add a field after the `console` field:

```python
    menu: tuple = None       # (title, [(label, value, selected), ...]) when open, else None
```

And extend the `RenderFrame` docstring's final sentence to mention it:

```python
    (lines, input_line, cursor_col) when open, else None. menu is the overlay
    menu's view (title, rows) when open, else None.
```

- [ ] **Step 5: Initialise video state in `Client.__init__`**

In `client.py` `Client.__init__`, immediately after the existing line `self._zbuf_scale = ZBUF_SCALE     # desired textured divisor, persists across maps`, add:

```python
        # fixed textured render resolution (video-options menu), persists across
        # maps like _zbuf_scale; applied to each freshly built Renderer.
        self.video_res = DEFAULT_VIDEO_RES
```

Then, after the existing `self._register_console()` call at the end of `__init__`, add:

```python
        self.menu = self._build_menu()
```

- [ ] **Step 6: Apply `video_res` to the renderer in `_load_map`**

In `client.py` `_load_map`, immediately after the existing line `self.rend.zbuf_scale = self._zbuf_scale   # keep the console's chosen scale`, add:

```python
        self.rend.video_res = self.video_res      # keep the menu's chosen resolution
```

- [ ] **Step 7: Add `set_video_res`, `_build_menu`, and `_menu_back`**

In `client.py`, add these methods to `Client` (place them just after `_apply_mode`, near the other render-state helpers):

```python
    def set_video_res(self, wh):
        """Set the textured render resolution (None = Auto/window-derived) and
        rebuild the framebuffer now, so a menu change takes effect immediately
        even when the window size hasn't changed."""
        self.video_res = wh
        self.rend.video_res = wh
        self.rend.resize(self.rend.width, self.rend.height)

    def _menu_back(self):
        self.menu.active = False

    def _build_menu(self):
        """Build the Escape overlay menu: Resolution (cycles VIDEO_MODES), Back,
        Quit. Closures bind to this Client's methods, like console commands."""
        idx = next((i for i, (_, v) in enumerate(VIDEO_MODES)
                    if v == self.video_res), 0)
        res = ChoiceItem("Resolution", VIDEO_MODES, idx, self.set_video_res)
        back = ActionItem("Back", self._menu_back)
        quit_item = ActionItem("Quit", self._cmd_quit_menu)
        return Menu("VIDEO OPTIONS", [res, back, quit_item])

    def _cmd_quit_menu(self):
        self.quit_requested = True
```

- [ ] **Step 8: Expose the menu view on the returned `RenderFrame`**

In `client.py` `Client.frame`, find the console block near the end:

```python
        con = self.con
        console = None
        if con.active:
            con.width = max(20, w // 9)           # ~9px per monospace cell at the HUD size
            rows = max(1, (h * 2 // 5) // 16 - 1)  # panel is ~40% tall, ~16px lines
            console = (con.view_lines(rows), "]" + con.input, con.cursor + 1)
```

Immediately after it, add:

```python
        menu = self.menu.view() if self.menu.active else None
```

Then change the final `return RenderFrame(...)` to pass `menu=menu`:

```python
        return RenderFrame(mode=self.mode, segs=segs, polys=polys,
                           framebuffer=framebuffer, particles=particles,
                           overlays=overlays, crosshair=(w // 2, h // 2),
                           console=console, menu=menu)
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python test_video_menu.py`
Expected: `OK`

- [ ] **Step 10: Confirm no regressions in the client/console tests**

Run: `python test_client.py` then `python test_console_client.py`
Expected: `OK` from each.

- [ ] **Step 11: Commit**

```bash
git add client.py test_video_menu.py
git commit -m "client: video_res state + Escape menu model + RenderFrame.menu

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Draw the menu panel (`GdiBlitter.draw_menu`)

**Files:**
- Modify: `win_ui.py` (add `GdiBlitter.draw_menu`, after `draw_console` ~line 440)

This is GDI drawing (Windows-only, ctypes); like `draw_console` it is verified manually, not unit-tested. There is no test step.

- [ ] **Step 1: Add `draw_menu`**

In `win_ui.py`, add this method to `GdiBlitter` immediately after `draw_console`:

```python
    def draw_menu(self, view, dst_w, dst_h):
        """Draw the Escape overlay menu: a centered dark panel with the title and
        rows, the selected row prefixed '> ' and brightened. view is
        (title, [(label, value, selected), ...]). Drawn after the world present,
        straight onto the window DC. Mirrors draw_console's font handling."""
        title, rows = view
        g, u = self.gdi32, self.user32
        hdc = u.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            g.SetBkMode(hdc, TRANSPARENT)
            g.SelectObject(hdc, self._font)
            sz = SIZE()
            g.GetTextExtentPoint32W(hdc, "X", 1, ctypes.byref(sz))
            lh = sz.cy or 16
            # panel sized to the content: title + blank + one row per item
            nlines = len(rows) + 2
            panel_w = 360
            panel_h = nlines * lh + 24
            x0 = (dst_w - panel_w) // 2
            y0 = (dst_h - panel_h) // 2
            rect = wintypes.RECT(x0, y0, x0 + panel_w, y0 + panel_h)
            brush = g.CreateSolidBrush(colorref((16, 16, 24)))
            u.FillRect(hdc, ctypes.byref(rect), brush)
            g.DeleteObject(brush)
            # 1px green bottom edge, like the console panel
            edge = wintypes.RECT(x0, y0 + panel_h - 1, x0 + panel_w, y0 + panel_h)
            ebrush = g.CreateSolidBrush(colorref((0, 160, 70)))
            u.FillRect(hdc, ctypes.byref(edge), ebrush)
            g.DeleteObject(ebrush)
            # title in yellow
            g.SetTextColor(hdc, colorref((255, 255, 0)))
            g.TextOutW(hdc, x0 + 16, y0 + 12, title, len(title))
            # rows, starting one blank line below the title
            y = y0 + 12 + 2 * lh
            for label, value, selected in rows:
                text = label if not value else f"{label}: {value}"
                if selected:
                    text = "> " + text
                    g.SetTextColor(hdc, colorref((255, 255, 255)))
                else:
                    text = "  " + text
                    g.SetTextColor(hdc, colorref((160, 200, 160)))
                g.TextOutW(hdc, x0 + 16, y, text, len(text))
                y += lh
        finally:
            u.ReleaseDC(self.hwnd, hdc)
```

- [ ] **Step 2: Sanity-check imports/symbols**

Confirm `draw_menu` uses only names already imported/defined in `win_ui.py`: `TRANSPARENT`, `SIZE`, `wintypes`, `ctypes`, `colorref`, `self._font` — all are used by `draw_console` already, so no new imports are needed.

- [ ] **Step 3: Commit**

```bash
git add win_ui.py
git commit -m "win_ui: GdiBlitter.draw_menu centered overlay panel

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire the menu into the gdi32 frontend (`win_gdi.py`)

**Files:**
- Modify: `win_gdi.py` (`GameWindow.__init__`; `_proc`; `_toggle_console`; new `_open_menu`, `_menu_key`; `build_input`; `run`)

Windows-only frontend wiring; verified manually (Task 6). No unit test.

- [ ] **Step 1: Add a `menu` reference in `GameWindow.__init__`**

In `win_gdi.py` `GameWindow.__init__`, immediately after the existing line `self.console = None             # wired to client.con in run(); None until then`, add:

```python
        self.menu = None                # wired to client.menu in run(); None until then
```

- [ ] **Step 2: Route Escape to open the menu, and arrows/Enter into it when open**

In `win_gdi.py` `_proc`, replace the `WM_KEYDOWN`/`WM_SYSKEYDOWN` branch:

```python
        elif msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            # F1 checked first so it toggles the console open AND closed
            if wparam == VK_F1:
                self._toggle_console()
            elif self.console and self.console.active:
                self._console_key(wparam)
            elif wparam == VK_ESCAPE:
                self.running = False
                self.user32.PostQuitMessage(0)
            else:
                self.keys.add(wparam)
```

with:

```python
        elif msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            # F1 checked first so it toggles the console open AND closed
            if wparam == VK_F1:
                self._toggle_console()
            elif self.console and self.console.active:
                self._console_key(wparam)
            elif self.menu and self.menu.active:
                self._menu_key(wparam)
            elif wparam == VK_ESCAPE:
                self._open_menu()
            else:
                self.keys.add(wparam)
```

- [ ] **Step 3: Add `_open_menu` and `_menu_key`; close the menu when the console opens**

In `win_gdi.py`, add these two methods next to `_console_key` (e.g. right after `_toggle_console`):

```python
    def _open_menu(self):
        """Esc (with the console closed): open the overlay menu. Clears held keys
        and ungrabs the mouse so the cursor is visible, like opening the console.
        Falls back to quitting if no menu is wired."""
        if self.menu is None:
            self.running = False
            self.user32.PostQuitMessage(0)
            return
        self.menu.active = True
        self.keys.clear()
        self.ungrab()

    def _menu_key(self, vk):
        """Drive the overlay menu from a virtual-key while it is open. Everything
        here is swallowed -- no game state is touched."""
        m = self.menu
        if vk == VK_ESCAPE:
            m.key_escape()
        elif vk == VK_UP:
            m.key_up()
        elif vk == VK_DOWN:
            m.key_down()
        elif vk == VK_LEFT:
            m.key_left()
        elif vk == VK_RIGHT:
            m.key_right()
        elif vk == VK_RETURN:
            m.key_enter()
```

Then, in `_toggle_console`, close the menu when the console is opened so the two panels are never active at once. Replace:

```python
    def _toggle_console(self):
        """F1: open/close the console. Opening clears held movement keys and
        ungrabs the mouse so the cursor is visible while typing."""
        con = self.console
        if con is None:
            return
        con.active = not con.active
        if con.active:
            self.keys.clear()
            self.ungrab()
```

with:

```python
    def _toggle_console(self):
        """F1: open/close the console. Opening clears held movement keys and
        ungrabs the mouse so the cursor is visible while typing, and closes the
        overlay menu so the two panels are never active at once."""
        con = self.console
        if con is None:
            return
        con.active = not con.active
        if con.active:
            if self.menu:
                self.menu.active = False
            self.keys.clear()
            self.ungrab()
```

- [ ] **Step 4: Suppress game input while the menu is open**

In `win_gdi.py` `build_input`, immediately after the existing console-guard block:

```python
        if self.console and self.console.active:
            # console owns the keyboard; feed the Client a do-nothing frame
            # (keep mouselook flag only so the HUD prompt is right).
            self._prev_keys = set()
            return InputState(mouselook=self.mouselook)
```

add:

```python
        if self.menu and self.menu.active:
            # menu owns the keyboard; feed the Client a do-nothing frame
            self._prev_keys = set()
            return InputState(mouselook=self.mouselook)
```

- [ ] **Step 5: Wire the menu reference and draw it in `run`**

In `win_gdi.py` `run`, immediately after the existing line `win.console = client.con`, add:

```python
    win.menu = client.menu
```

Then, in the same `run` loop, immediately after the console-drawing block:

```python
            if rf.console is not None:
                lines, input_line, cursor_col = rf.console
                blitter.draw_console(lines, input_line, cursor_col, cw, ch)
```

add:

```python
            if rf.menu is not None:
                blitter.draw_menu(rf.menu, cw, ch)
```

- [ ] **Step 6: Commit**

```bash
git add win_gdi.py
git commit -m "win_gdi: Escape opens the video menu; route keys + draw it

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Manual verification on Windows

**Files:** none (manual run).

- [ ] **Step 1: Run the game**

Run: `python main.py e1m1`  (gdi32 is the Windows default)

- [ ] **Step 2: Verify menu open/close**

- Press **Escape** → the centered "VIDEO OPTIONS" panel appears; the game no longer quits.
- Mouse cursor is visible; movement keys do nothing while the menu is open.
- Press **Escape** again, or select **Back** with Enter → the menu closes and play resumes.

- [ ] **Step 3: Verify resolution switching (textured mode)**

- Ensure textured mode is on (default; toggle with **Z**/**T** if needed).
- Open the menu, select **Resolution**, press **Left/Right** to cycle 240x160 ↔ 320x240 ↔ 640x480 ↔ Auto.
- Confirm the world visibly changes chunkiness: **240x160** is coarsest, **640x480** is finest; the image always fills the window (stretched). 240x160 looks slightly vertically stretched (3:2 into 4:3) — expected.
- Confirm wireframe (**N** off textured, or via console) and flat modes are unaffected by the resolution setting.

- [ ] **Step 4: Verify Quit + console coexistence**

- Open the menu, select **Quit**, press Enter → the game exits cleanly.
- Re-run; press **F1** (console) while the menu is open → the menu closes and the console opens (never both).
- The `zbuf_scale` console command still works when Resolution is set to **Auto**.

- [ ] **Step 5: Run the whole test suite once**

Run (PowerShell): `Get-ChildItem test_*.py | ForEach-Object { python $_.Name }`
Expected: each prints `OK`.

- [ ] **Step 6: Final push**

```bash
git push
```

---

## Self-review notes

- **Spec coverage:** fixed render buffer (Task 1), pure menu model (Task 2), Client video state + default 320x240 + persistence + RenderFrame field (Task 3), panel drawing (Task 4), Escape routing / input suppression / Quit-as-item / draw (Task 5), aspect-ratio + zbuf_scale-Auto behaviour verified (Task 6). Auto retained per the spec's flagged decision.
- **Out of scope (unchanged):** tkinter frontend, mouse navigation, window resizing, letterboxing.
- **Type consistency:** `video_res` is `(w, h) | None` everywhere; `ChoiceItem(title, options, index, on_select)`, `ActionItem(title, on_activate)`, `Menu.view()` → `(title, [(label, value_label, is_selected)])` are used identically across `quake/menu.py`, `client.py`, and `win_ui.draw_menu`.
