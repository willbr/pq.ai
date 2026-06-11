"""Windows gdi32 front-end (outside the `quake` engine package): plays the REAL
game by driving the UI-agnostic `Client` core with its own classic Win32 game loop
and raw-input mouselook, drawing the textured view via win_ui.GdiBlitter.

Why this exists: tkinter owns the Win32 message pump and the ~13ms software render
blocks it, so raw WM_INPUT events queue faster than Tk drains them -- a hard mouse
swing builds seconds of backlog (see spike_gdi.py's diagnosis). This front-end
inverts ownership: we own the window and a classic loop --
    drain ALL pending messages -> step Client -> render -> repeat
Because the WndProc accumulates raw deltas and we read them ONCE per frame, a burst
that arrives during the render just sums into a single frame; backlog is therefore
structurally impossible, and keyboard is drained in the same pass so it can't be
starved. spike_gdi.py proved the loop; this wires it to the full Client.

Unlike the spike (which flew noclip through bare geometry), this is a real front-end:
it builds a `Client`, feeds it an `InputState` each frame, and blits the textured
RenderFrame it returns -- entities, HUD, weapon, the works. Mouselook is toggleable
(Tab, or a left-click while the cursor is free, like main.py's click-to-capture);
ungrabbed the cursor is visible and clicks work via legacy messages.

Run: python win_gdi.py [map]   (Windows). The frontend draws all three render
modes (textured/wireframe/flat) via GdiBlitter.

NOTE: the SpikeWindow-derived window class below is duplicated from spike_gdi.py on
purpose -- Stage 4 deletes the spike and this stays self-contained. Sharing the
class is deferred until then.
"""

import ctypes
import sys
import time
from ctypes import wintypes

from client import Client, InputState
from quake.console import TeeStdout
from quake.perf import PROFILER
import win_ui

# ---- Win32 window constants -------------------------------------------------
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
PM_REMOVE = 0x0001
WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_QUIT = 0x0012
CW_USEDEFAULT = -2147483648                # 0x80000000 as a signed int

# ---- virtual-key codes (winuser.h) ------------------------------------------
VK_TAB, VK_SHIFT, VK_CONTROL, VK_ESCAPE, VK_SPACE = 0x09, 0x10, 0x11, 0x1B, 0x20
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_W, VK_A, VK_S, VK_D = 0x57, 0x41, 0x53, 0x44
VK_C, VK_N, VK_F, VK_Z, VK_T = 0x43, 0x4E, 0x46, 0x5A, 0x54
VK_P = 0x50
VK_F1 = 0x70
VK_RETURN, VK_BACK, VK_DELETE = 0x0D, 0x08, 0x2E
VK_HOME, VK_END = 0x24, 0x23
VK_PRIOR, VK_NEXT = 0x21, 0x22        # PageUp / PageDown
WM_CHAR = 0x0102
# one-shot toggle keys -> the Client command they fire (edge-triggered)
COMMAND_KEYS = {VK_N: "noclip", VK_F: "flat", VK_Z: "zbuf", VK_T: "texture",
                VK_P: "prof"}

GetRawInputData = ctypes.WinDLL("user32").GetRawInputData
GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                            ctypes.POINTER(wintypes.UINT), wintypes.UINT]
GetRawInputData.restype = wintypes.UINT


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", win_ui.WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


class GameWindow:
    """Owns a gdi32 window, its (toggleable) raw-input grab, and the keyboard/mouse
    state the loop reads. The WndProc accumulates raw deltas; the loop drains and
    applies them once per frame. Adapted from spike_gdi.SpikeWindow: mouselook here
    is toggleable (start ungrabbed, cursor visible, legacy mouse on) rather than
    always-on, so menus/clicks work before the player engages the view."""

    def __init__(self, title, width, height):
        self.dx = 0
        self.dy = 0
        self.left_down = False          # raw left button (used while grabbed)
        self.lbutton = False            # legacy WM_LBUTTON state (used ungrabbed)
        self.keys = set()               # held virtual-key codes
        self._prev_keys = set()         # last frame's keys, for edge detection
        self.mouselook = False          # raw grab engaged?
        self.running = True
        self.raw_events = 0             # cumulative WM_INPUT count (diagnostics only)
        self.console = None             # wired to client.con in run(); None until then
        self.menu = None                # wired to client.menu in run(); None until then
        u = self.user32 = ctypes.WinDLL("user32")
        k = ctypes.WinDLL("kernel32")
        for name, restype, argtypes in (
            ("DefWindowProcW", win_ui.LRESULT, [wintypes.HWND, wintypes.UINT,
                                                wintypes.WPARAM, wintypes.LPARAM]),
            ("RegisterClassExW", wintypes.ATOM, [ctypes.POINTER(WNDCLASSEXW)]),
            ("CreateWindowExW", wintypes.HWND, [wintypes.DWORD, wintypes.LPCWSTR,
                wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                wintypes.HINSTANCE, ctypes.c_void_p]),
            ("ShowWindow", wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
            ("UpdateWindow", wintypes.BOOL, [wintypes.HWND]),
            ("PeekMessageW", wintypes.BOOL, [ctypes.POINTER(MSG), wintypes.HWND,
                wintypes.UINT, wintypes.UINT, wintypes.UINT]),
            ("TranslateMessage", wintypes.BOOL, [ctypes.POINTER(MSG)]),
            ("DispatchMessageW", win_ui.LRESULT, [ctypes.POINTER(MSG)]),
            ("PostQuitMessage", None, [ctypes.c_int]),
            ("DestroyWindow", wintypes.BOOL, [wintypes.HWND]),
            ("GetClientRect", wintypes.BOOL, [wintypes.HWND,
                                              ctypes.POINTER(wintypes.RECT)]),
            ("RegisterRawInputDevices", wintypes.BOOL,
                [ctypes.POINTER(win_ui.RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]),
            ("ClipCursor", wintypes.BOOL, [ctypes.POINTER(wintypes.RECT)]),
            ("GetWindowRect", wintypes.BOOL, [wintypes.HWND,
                                              ctypes.POINTER(wintypes.RECT)]),
            ("ShowCursor", ctypes.c_int, [wintypes.BOOL]),
        ):
            fn = getattr(u, name)
            fn.restype = restype
            fn.argtypes = argtypes
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        self._wndproc = win_ui.WNDPROC(self._proc)      # keep a ref (anti-GC)
        hinst = k.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.style = CS_HREDRAW | CS_VREDRAW
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "pqai_gdi"
        if not u.RegisterClassExW(ctypes.byref(cls)):
            raise OSError(f"RegisterClassExW failed ({ctypes.GetLastError()})")
        self._cls = cls                                  # keep alive
        self.hwnd = u.CreateWindowExW(0, "pqai_gdi", title, WS_OVERLAPPEDWINDOW,
                                      CW_USEDEFAULT, CW_USEDEFAULT, width, height,
                                      None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.GetLastError()})")
        u.ShowWindow(self.hwnd, SW_SHOW)
        u.UpdateWindow(self.hwnd)
        # start ungrabbed: cursor visible, legacy mouse on so the first click works.

    # -- raw grab / ungrab (toggleable, unlike the spike's always-on _grab) -----
    def grab(self):
        """Engage mouselook: register raw mouse NOLEGACY (only WM_INPUT), confine
        and hide the cursor."""
        if self.mouselook:
            return
        rid = win_ui.RAWINPUTDEVICE(win_ui.HID_USAGE_PAGE_GENERIC,
                                    win_ui.HID_USAGE_GENERIC_MOUSE,
                                    win_ui.RIDEV_NOLEGACY, self.hwnd)
        self.user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                            ctypes.sizeof(win_ui.RAWINPUTDEVICE))
        r = wintypes.RECT()
        if self.user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            self.user32.ClipCursor(ctypes.byref(r))
        self.user32.ShowCursor(False)
        self.dx = self.dy = 0
        self.left_down = False
        self.mouselook = True

    def ungrab(self):
        """Release mouselook: unregister raw mouse, unclip and show the cursor,
        restoring legacy mouse messages so clicks work again."""
        if not self.mouselook:
            return
        rm = win_ui.RAWINPUTDEVICE(win_ui.HID_USAGE_PAGE_GENERIC,
                                   win_ui.HID_USAGE_GENERIC_MOUSE,
                                   win_ui.RIDEV_REMOVE, None)
        self.user32.RegisterRawInputDevices(ctypes.byref(rm), 1,
                                            ctypes.sizeof(win_ui.RAWINPUTDEVICE))
        self.user32.ClipCursor(None)
        self.user32.ShowCursor(True)
        self.lbutton = False
        self.mouselook = False

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == win_ui.WM_INPUT:
            self._read_raw(lparam)
        elif msg == WM_CHAR:
            if self.console and self.console.active:
                ch = chr(wparam) if 0x20 <= wparam <= 0x7E else ""
                if ch:
                    self.console.key_char(ch)
                # swallow; control chars (CR/BS/TAB) are handled via WM_KEYDOWN
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
        elif msg in (WM_KEYUP, WM_SYSKEYUP):
            self.keys.discard(wparam)
        elif msg == WM_LBUTTONDOWN:
            self.lbutton = True
        elif msg == WM_LBUTTONUP:
            self.lbutton = False
        elif msg == WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            self.running = False
            self.user32.PostQuitMessage(0)
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

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

    def _console_key(self, vk):
        """Drive the console line editor from a virtual-key while it is open.
        Everything here is swallowed -- no game state is touched."""
        con = self.console
        if vk == VK_ESCAPE:
            con.active = False
        elif vk == VK_RETURN:
            con.key_enter()
        elif vk == VK_BACK:
            con.key_backspace()
        elif vk == VK_DELETE:
            con.key_delete()
        elif vk == VK_TAB:
            con.key_tab()
        elif vk == VK_LEFT:
            con.key_left()
        elif vk == VK_RIGHT:
            con.key_right()
        elif vk == VK_HOME:
            con.key_home()
        elif vk == VK_END:
            con.key_end()
        elif vk == VK_UP:
            con.key_up()
        elif vk == VK_DOWN:
            con.key_down()
        elif vk == VK_PRIOR:
            con.key_pageup()
        elif vk == VK_NEXT:
            con.key_pagedown()

    def _read_raw(self, lparam):
        ri = win_ui.RAWINPUT()
        size = wintypes.UINT(ctypes.sizeof(win_ui.RAWINPUT))
        got = GetRawInputData(lparam, win_ui.RID_INPUT, ctypes.byref(ri),
                              ctypes.byref(size), ctypes.sizeof(win_ui.RAWINPUTHEADER))
        if got == 0xFFFFFFFF or ri.header.dwType != win_ui.RIM_TYPEMOUSE:
            return
        self.raw_events += 1
        ddx, ddy = win_ui.raw_mouse_delta(ri.mouse.usFlags,
                                          ri.mouse.lLastX, ri.mouse.lLastY)
        self.dx += ddx
        self.dy += ddy
        self.left_down = win_ui.apply_left_button(self.left_down,
                                                  ri.mouse.ulButtons & 0xFFFF)

    def pump(self):
        """Drain EVERY pending message this frame (the crux of the fix)."""
        msg = MSG()
        while self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if msg.message == WM_QUIT:
                self.running = False
                return
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageW(ctypes.byref(msg))

    def read_mouse(self):
        dx, dy = self.dx, self.dy
        self.dx = self.dy = 0
        return dx, dy

    def client_size(self):
        r = wintypes.RECT()
        self.user32.GetClientRect(self.hwnd, ctypes.byref(r))
        return max(1, r.right - r.left), max(1, r.bottom - r.top)

    # -- per-frame input -> a Client InputState ---------------------------------
    def build_input(self, dt):
        """Translate this frame's keyboard + mouse state into an InputState the
        Client consumes. Edge detection (newly-pressed since last frame) drives the
        one-shot intents: impulse (weapon select 1-8), commands (N/F/Z/T toggles),
        Tab (mouselook toggle) and click-to-capture. Held keys drive movement."""
        if self.console and self.console.active:
            # console owns the keyboard; feed the Client a do-nothing frame
            # (keep mouselook flag only so the HUD prompt is right).
            self._prev_keys = set()
            return InputState(mouselook=self.mouselook)
        if self.menu and self.menu.active:
            # menu owns the keyboard; feed the Client a do-nothing frame
            self._prev_keys = set()
            return InputState(mouselook=self.mouselook)
        keys = self.keys
        newly = keys - self._prev_keys     # keys pressed since last frame

        # Tab toggles mouselook; a left click while ungrabbed engages it (like
        # main.py's click-to-capture). Both are edge-triggered.
        if VK_TAB in newly:
            self.ungrab() if self.mouselook else self.grab()
        if not self.mouselook and self.lbutton:
            self.grab()

        def held(vk):
            return 1.0 if vk in keys else 0.0

        move_forward = (1.0 if (VK_W in keys or VK_UP in keys) else 0.0) - \
                       (1.0 if (VK_S in keys or VK_DOWN in keys) else 0.0)
        move_strafe = held(VK_D) - held(VK_A)
        move_up = held(VK_SPACE) - held(VK_C)
        turn = held(VK_RIGHT) - held(VK_LEFT)
        run = VK_SHIFT in keys

        look_dx, look_dy = self.read_mouse() if self.mouselook else (0, 0)

        # fire: raw left button while grabbed, legacy left button while not, OR Ctrl.
        fire = (self.left_down if self.mouselook else self.lbutton) or \
               (VK_CONTROL in keys)

        # impulse: a weapon-select digit (1..8) newly pressed this frame.
        impulse = 0
        for i in range(8):
            if (0x31 + i) in newly:
                impulse = i + 1
                break

        commands = frozenset(cmd for vk, cmd in COMMAND_KEYS.items() if vk in newly)

        self._prev_keys = set(keys)
        return InputState(move_forward=move_forward, move_strafe=move_strafe,
                          move_up=move_up, turn=turn, look_dx=look_dx,
                          look_dy=look_dy, run=run, fire=fire, impulse=impulse,
                          commands=commands, mouselook=self.mouselook)

    def shutdown(self):
        self.ungrab()
        # ungrab() restores the cursor if it was hidden; ClipCursor(None) is a harmless re-unclip
        self.user32.ClipCursor(None)


def run(mapname):
    win = GameWindow(f"pq.ai gdi — {mapname}", 800, 600)
    real_stdout = sys.stdout            # restored in finally; safe even if Client() throws
    client = Client(mapname)
    win.console = client.con
    win.menu = client.menu
    sys.stdout = TeeStdout(real_stdout, client.con.print)
    blitter = None

    last = time.perf_counter()
    try:
        blitter = win_ui.GdiBlitter(win.hwnd)
        blitter.set_palette(client.palette)   # 8bpp palettised framebuffer blits
        pal_version = 0                       # view-palette (tint) the DIB matches
        cw, ch = win.client_size()
        client.resize(cw, ch)
        last_wh = (cw, ch)
        while win.running:
            win.pump()                       # drain ALL input first
            if not win.running:
                break
            now = time.perf_counter()
            dt = now - last
            last = now

            inp = win.build_input(dt)
            rf = client.frame(dt, inp)
            if client.quit_requested:
                win.running = False

            cw, ch = win.client_size()
            if (cw, ch) != last_wh:
                client.resize(cw, ch)
                last_wh = (cw, ch)
            texts = list(rf.overlays) + [
                (rf.crosshair[0], rf.crosshair[1], "+", (0, 255, 102), "center")]
            # no sleep: present/present_vector provide implicit back-pressure
            PROFILER.begin("present")
            if rf.mode == "zbuf":
                if rf.palette_version != pal_version:   # tint shift changed
                    blitter.set_palette(rf.palette or client.palette)
                    pal_version = rf.palette_version
                fb, fw, fh = rf.framebuffer
                blitter.present(fb, fw, fh, cw, ch, texts=texts,
                                particles=rf.particles)
            elif rf.mode == "wire":
                blitter.present_vector(rf.segs, None, rf.particles, cw, ch,
                                       texts=texts)
            elif rf.mode == "wire_hidden":
                blitter.present_vector(None, rf.polys, rf.particles, cw, ch,
                                       texts=texts, hidden=True)
            else:   # "flat"
                blitter.present_vector(None, rf.polys, rf.particles, cw, ch,
                                       texts=texts)
            PROFILER.end("present")
            if rf.console is not None:
                lines, input_line, cursor_col = rf.console
                blitter.draw_console(lines, input_line, cursor_col, cw, ch)
            if rf.menu is not None:
                blitter.draw_menu(rf.menu, cw, ch)
            PROFILER.frame_end()     # roll this frame's section times into the HUD readout
    finally:
        sys.stdout = real_stdout
        if blitter is not None:
            blitter.close()
        win.shutdown()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("win_gdi is Windows-only")
    run(sys.argv[1] if len(sys.argv) > 1 else "e1m1")
