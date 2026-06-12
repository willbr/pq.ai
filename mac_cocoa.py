"""macOS Cocoa front-end (outside the `quake` engine package): plays the REAL
game by driving the UI-agnostic `Client` core with its own drain-then-step
frame loop, drawing via CoreGraphics in an NSView's drawRect:, with true
relative-delta mouselook (no warp hack). The PyObjC twin of win_gdi.py.

Why this exists: tkinter's after() loop owns the event pump and the ~13ms
software render blocks it (win_gdi.py's diagnosis applies on every platform).
This front-end inverts ownership the same way:
    drain ALL pending NSEvents -> step Client -> view.display() -> repeat
Mouse deltas accumulate in the view between frames and are read ONCE per frame,
so input bursts coalesce structurally. Mouselook uses
CGAssociateMouseAndMouseCursorPosition(False) + NSEvent deltaX/deltaY -- real
relative input, so none of main.py's warp/recenter machinery exists here.

Run: python main.py [map] on macOS (this is the darwin default; --tk forces
tkinter). Requires PyObjC: pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz
"""

import signal
import sys
import time

import AppKit
import Quartz
import objc

from client import Client, InputState
from quake.console import TeeStdout
from quake.perf import PROFILER
import mac_ui
from mac_ui import letterbox_rect

# one-shot toggle keys -> the Client command they fire (edge-triggered)
COMMAND_KEYS = {"n": "noclip", "f": "flat", "z": "zbuf", "t": "texture",
                "p": "prof"}
FRAME_S = 1 / 60                       # target frame cadence (sleep floor)


class GameView(AppKit.NSView):
    """The game's content view: accumulates input state (held keys, mouse
    deltas, buttons) from the responder methods and draws the current
    RenderFrame in drawRect:. The frame loop owns stepping and presentation;
    this class is deliberately dumb storage + drawing."""

    def initWithFrame_(self, frame):
        self = objc.super(GameView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.keys = set()              # held key names (mac_ui.KEYCODE_NAMES)
        self.dx = 0.0                  # mouse deltas accumulated since last read
        self.dy = 0.0
        self.lbutton = False           # left button held
        self.mouselook = False         # cursor grabbed?
        self.console = None            # wired to client.con in run()
        self.menu = None               # wired to client.menu in run()
        self.rf = None                 # RenderFrame to draw
        self.client = None             # wired in run() (palette for the LUT)
        self._pal_luts = None          # (lr, lg, lb) translate tables
        self._pal_version = -1
        return self

    # ---- view behaviour ----
    def isFlipped(self):
        return True                    # y-down, matching RenderFrame coords

    def acceptsFirstResponder(self):
        return True

    # ---- mouse grab / ungrab ----
    def grab(self):
        if self.mouselook:
            return
        AppKit.NSCursor.hide()
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        self.dx = self.dy = 0.0
        self.mouselook = True

    def ungrab(self):
        if not self.mouselook:
            return
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        AppKit.NSCursor.unhide()
        self.lbutton = False
        self.mouselook = False

    # ---- keyboard ----
    def keyDown_(self, event):
        name = mac_ui.KEYCODE_NAMES.get(event.keyCode())
        # F1 (or backtick) toggles the console open AND closed -- checked first
        if name in ("f1", "grave"):
            self._toggle_console()
            return
        if self.console is not None and self.console.active:
            self._console_key(name, event)
            return
        if self.menu is not None and self.menu.active:
            self._menu_key(name)
            return
        if name == "escape":
            self._open_menu()
            return
        if name is not None and not event.isARepeat():
            self.keys.add(name)

    def keyUp_(self, event):
        name = mac_ui.KEYCODE_NAMES.get(event.keyCode())
        self.keys.discard(name)

    def flagsChanged_(self, event):
        """Shift (run) and Ctrl (fire) arrive as modifier-flag changes, not
        keyDown/keyUp; mirror them into the held-keys set."""
        flags = event.modifierFlags()
        for flag, name in ((AppKit.NSEventModifierFlagShift, "shift"),
                           (AppKit.NSEventModifierFlagControl, "control")):
            if flags & flag:
                self.keys.add(name)
            else:
                self.keys.discard(name)

    def _toggle_console(self):
        """F1/backtick: open or close the console. Opening clears held keys,
        ungrabs the mouse, and closes the menu (panels never stack). Mirrors
        win_gdi._toggle_console."""
        con = self.console
        if con is None:
            return
        con.active = not con.active
        if con.active:
            if self.menu is not None:
                self.menu.active = False
            self.keys.clear()
            self.ungrab()

    def _open_menu(self):
        """Esc with the console closed: open the overlay menu, clear held keys,
        ungrab. Mirrors win_gdi._open_menu."""
        if self.menu is None:
            return
        self.menu.active = True
        self.keys.clear()
        self.ungrab()

    def _menu_key(self, name):
        m = self.menu
        if name == "escape":
            m.key_escape()
        elif name == "up":
            m.key_up()
        elif name == "down":
            m.key_down()
        elif name == "left":
            m.key_left()
        elif name == "right":
            m.key_right()
        elif name in ("return", "kp_enter"):
            m.key_enter()

    def _console_key(self, name, event):
        con = self.console
        if name == "escape":
            con.active = False
        elif name in ("return", "kp_enter"):
            con.key_enter()
        elif name == "backspace":
            con.key_backspace()
        elif name == "delete":
            con.key_delete()
        elif name == "tab":
            con.key_tab()
        elif name == "left":
            con.key_left()
        elif name == "right":
            con.key_right()
        elif name == "home":
            con.key_home()
        elif name == "end":
            con.key_end()
        elif name == "up":
            con.key_up()
        elif name == "down":
            con.key_down()
        elif name == "pageup":
            con.key_pageup()
        elif name == "pagedown":
            con.key_pagedown()
        else:
            chars = event.charactersIgnoringModifiers()
            if chars:
                ch = chars[0]
                if ch >= " " and ch != "\x7f":
                    con.key_char(ch)

    # ---- mouse ----
    def mouseDown_(self, event):
        self.lbutton = True

    def mouseUp_(self, event):
        self.lbutton = False

    def mouseMoved_(self, event):
        if self.mouselook:
            self.dx += event.deltaX()
            self.dy += event.deltaY()

    def mouseDragged_(self, event):
        self.mouseMoved_(event)

    def read_mouse(self):
        dx, dy = self.dx, self.dy
        self.dx = self.dy = 0.0
        return dx, dy

    # ---- drawing ----
    def drawRect_(self, rect):
        rf = self.rf
        b = self.bounds()
        w, h = int(b.size.width), int(b.size.height)
        ctx = AppKit.NSGraphicsContext.currentContext().CGContext()
        mac_ui.fill_rect(ctx, 0, 0, w, h, (0, 0, 0))      # clear to black
        if rf is None:
            return
        texts = list(rf.overlays) + [
            (rf.crosshair[0], rf.crosshair[1], "+", (0, 255, 102), "center")]
        particles = rf.particles
        if rf.mode == "zbuf":
            fb, fw, fh = rf.framebuffer
            if self._pal_luts is None or rf.palette_version != self._pal_version:
                pal = rf.palette or self.client.palette
                self._pal_luts = mac_ui.pal_channel_tables(pal)
                self._pal_version = rf.palette_version
            rgba = mac_ui.expand_fb_rgba(fb, fw, fh, *self._pal_luts)
            img = mac_ui.fb_cgimage(rgba, fw, fh)
            ox, oy, ow, oh = letterbox_rect(fw, fh, w, h)
            mac_ui.draw_fb(ctx, img, ox, oy, ow, oh, h)
            if ox or oy:
                particles = mac_ui.fit_particles(particles, ox, oy, ow, oh, w, h)
        elif rf.mode == "wire":
            mac_ui.draw_segs(ctx, rf.segs)
        elif rf.mode == "wire_hidden":
            mac_ui.draw_wire_hidden(ctx, rf.polys)
        else:                                            # "flat"
            mac_ui.draw_polys(ctx, rf.polys)
        mac_ui.draw_particles(ctx, particles)
        mac_ui.draw_texts(ctx, texts)
        if rf.console is not None:
            lines, input_line, cursor_col = rf.console
            mac_ui.draw_console(ctx, lines, input_line, cursor_col, w, h)
        if rf.menu is not None:
            mac_ui.draw_menu(ctx, rf.menu, w, h)


class _Delegate(AppKit.NSObject):
    """Window + application delegate: turns 'the user closed the window' or
    Cmd-Q into a clean loop exit (running=False) instead of process death, so
    run()'s finally block can shut the Client down."""

    def initWithState_(self, state):
        self = objc.super(_Delegate, self).init()
        if self is None:
            return None
        self.state = state
        return self

    def windowWillClose_(self, note):
        self.state["running"] = False

    def applicationShouldTerminate_(self, app):
        self.state["running"] = False
        return AppKit.NSTerminateCancel


def _make_app():
    """NSApplication with the activation dance (without Regular policy +
    activate there is no key window) and a minimal menu bar (Quit, Cmd-Q)."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    menubar = AppKit.NSMenu.alloc().init()
    appitem = AppKit.NSMenuItem.alloc().init()
    menubar.addItem_(appitem)
    appmenu = AppKit.NSMenu.alloc().init()
    quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit pq.ai", "terminate:", "q")
    appmenu.addItem_(quit_item)
    appitem.setSubmenu_(appmenu)
    app.setMainMenu_(menubar)
    return app


def _make_window(title, width, height, state):
    style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskMiniaturizable |
             AppKit.NSWindowStyleMaskResizable)
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((0, 0), (width, height)), style, AppKit.NSBackingStoreBuffered, False)
    window.setTitle_(title)
    window.center()
    view = GameView.alloc().initWithFrame_(((0, 0), (width, height)))
    window.setContentView_(view)
    window.makeFirstResponder_(view)
    window.setAcceptsMouseMovedEvents_(True)
    delegate = _Delegate.alloc().initWithState_(state)
    window.setDelegate_(delegate)
    window.makeKeyAndOrderFront_(None)
    return window, view, delegate


def build_input(view, prev_keys):
    """Translate this frame's keyboard + mouse state into an InputState. Edge
    detection drives the one-shots (impulse, commands, Tab, click-to-grab);
    held keys drive movement. Mirrors win_gdi.GameWindow.build_input; returns
    (InputState, new_prev_keys)."""
    if (view.console is not None and view.console.active) or \
       (view.menu is not None and view.menu.active):
        return InputState(mouselook=view.mouselook), set()
    keys = view.keys
    newly = keys - prev_keys

    if "tab" in newly:
        view.ungrab() if view.mouselook else view.grab()
    if not view.mouselook and view.lbutton:
        view.grab()

    def held(name):
        return 1.0 if name in keys else 0.0

    move_forward = (1.0 if ("w" in keys or "up" in keys) else 0.0) - \
                   (1.0 if ("s" in keys or "down" in keys) else 0.0)
    move_strafe = held("d") - held("a")
    move_up = held("space") - held("c")
    turn = held("right") - held("left")
    run_held = "shift" in keys

    look_dx, look_dy = view.read_mouse() if view.mouselook else (0.0, 0.0)
    fire = view.lbutton or ("control" in keys)

    impulse = 0
    for i in range(8):
        if str(i + 1) in newly:
            impulse = i + 1
            break

    commands = frozenset(cmd for key, cmd in COMMAND_KEYS.items()
                         if key in newly)
    return InputState(move_forward=move_forward, move_strafe=move_strafe,
                      move_up=move_up, turn=turn, look_dx=look_dx,
                      look_dy=look_dy, run=run_held, fire=fire,
                      impulse=impulse, commands=commands,
                      mouselook=view.mouselook), set(keys)


def run(mapname):
    state = {"running": True}
    # Ctrl-C: a KeyboardInterrupt raised while control is inside the ObjC
    # bridge (sendEvent_/display) is logged and swallowed by PyObjC, so the
    # default handler can't stop the loop; flip the flag instead and exit
    # through the normal shutdown path.
    signal.signal(signal.SIGINT,
                  lambda *_: state.__setitem__("running", False))
    app = _make_app()
    window, view, delegate = _make_window(f"pq.ai cocoa — {mapname}",
                                          800, 600, state)
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.finishLaunching()

    real_stdout = sys.stdout
    client = Client(mapname)
    view.client = client
    view.console = client.con
    view.menu = client.menu
    sys.stdout = TeeStdout(real_stdout, client.con.print)

    distant_past = AppKit.NSDate.distantPast()
    prev_keys = set()
    last = time.perf_counter()
    last_wh = (0, 0)
    try:
        while state["running"]:
            # drain ALL pending events first (the crux, as in win_gdi.pump)
            while True:
                event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AppKit.NSEventMaskAny, distant_past,
                    AppKit.NSDefaultRunLoopMode, True)
                if event is None:
                    break
                app.sendEvent_(event)
            if not state["running"]:
                break

            now = time.perf_counter()
            dt = now - last
            last = now

            b = view.bounds()
            cw, ch = max(1, int(b.size.width)), max(1, int(b.size.height))
            if (cw, ch) != last_wh:
                client.resize(cw, ch)
                last_wh = (cw, ch)

            inp, prev_keys = build_input(view, prev_keys)
            rf = client.frame(dt, inp)
            if client.quit_requested:
                break
            if client.mapname != mapname:        # changelevel / `map`: retitle
                mapname = client.mapname
                window.setTitle_(f"pq.ai cocoa — {mapname}")

            view.rf = rf
            with PROFILER.section("present"):
                view.setNeedsDisplay_(True)
                view.displayIfNeeded()
            PROFILER.frame_end()

            work = time.perf_counter() - now
            if work < FRAME_S:
                time.sleep(FRAME_S - work)
    finally:
        sys.stdout = real_stdout
        client.shutdown()            # stop+dispose audio while healthy
        view.ungrab()                # restore cursor + re-associate the mouse
        window.close()


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("mac_cocoa is macOS-only")
    run(sys.argv[1] if len(sys.argv) > 1 else "e1m1")
