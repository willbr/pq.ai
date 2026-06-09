"""Spike: a pure-gdi32 window (NO tkinter) to prove the tight game-loop fixes the
raw-input backlog that the tkinter front-end can't.

Diagnosis (see the diag run): tkinter owns the Win32 message pump, and the ~13ms
software render blocks it ~65% of every frame, so raw WM_INPUT events queue faster
than Tk drains them -- a hard mouse swing builds *seconds* of backlog that spins
the view for 10+s and starves the keyboard while it drains.

This spike inverts ownership: we own the window and a classic loop --
    drain ALL pending messages -> step -> render -> repeat
Because the WndProc accumulates raw deltas and we read them ONCE per frame, a burst
that arrives during the render just sums into a single frame. Backlog is therefore
structurally impossible, and keyboard is drained in the same pass so it can't be
starved. It reuses the engine (quake pkg) and win_ui's GdiBlitter / raw structs; it
renders only world geometry with noclip flight -- enough to feel WASD + mouselook
together. No entities/HUD/weapon; wireframe/flat stay on the tkinter path.

Run: python spike_gdi.py [map]   (Windows). Prints the same per-second diag as
main.py's PQ_DIAG so the two are directly comparable -- watch that raw/s drains to
0 the instant you stop, with no spin.
"""

import ctypes
import sys
import time
from ctypes import wintypes

from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer, angle_vectors

import win_ui

PAK_PATH = "quake-shareware/id1/pak0.pak"
LOOK_SENS = 0.15          # degrees / mouse count (matches main.py)
FLY_SPEED = 400.0         # units / second (noclip flight)

# ---- Win32 window constants -------------------------------------------------
CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
PM_REMOVE = 0x0001
WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
CW_USEDEFAULT = -2147483648                # 0x80000000 as a signed int
VK_ESCAPE, VK_SPACE, VK_CONTROL = 0x1B, 0x20, 0x11

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


class SpikeWindow:
    """Owns a gdi32 window, its raw-input grab, and the keyboard/mouse state the
    loop reads. The WndProc accumulates raw deltas; the loop drains and applies
    them once per frame."""

    def __init__(self, title, width, height):
        self.dx = 0
        self.dy = 0
        self.left_down = False
        self.keys = set()           # held virtual-key codes
        self.running = True
        self.raw_events = 0
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
        cls.lpszClassName = "pqai_spike"
        if not u.RegisterClassExW(ctypes.byref(cls)):
            raise OSError(f"RegisterClassExW failed ({ctypes.GetLastError()})")
        self._cls = cls                                  # keep alive
        self.hwnd = u.CreateWindowExW(0, "pqai_spike", title, WS_OVERLAPPEDWINDOW,
                                      CW_USEDEFAULT, CW_USEDEFAULT, width, height,
                                      None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.GetLastError()})")
        u.ShowWindow(self.hwnd, SW_SHOW)
        u.UpdateWindow(self.hwnd)
        self._grab()

    def _grab(self):
        # raw mouse, NOLEGACY (only WM_INPUT), confined + hidden cursor
        rid = win_ui.RAWINPUTDEVICE(win_ui.HID_USAGE_PAGE_GENERIC,
                                    win_ui.HID_USAGE_GENERIC_MOUSE,
                                    win_ui.RIDEV_NOLEGACY, self.hwnd)
        self.user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                            ctypes.sizeof(win_ui.RAWINPUTDEVICE))
        r = wintypes.RECT()
        if self.user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            self.user32.ClipCursor(ctypes.byref(r))
        self.user32.ShowCursor(False)

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == win_ui.WM_INPUT:
            self._read_raw(lparam)
        elif msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            if wparam == VK_ESCAPE:
                self.running = False
                self.user32.PostQuitMessage(0)
            self.keys.add(wparam)
        elif msg in (WM_KEYUP, WM_SYSKEYUP):
            self.keys.discard(wparam)
        elif msg == WM_CLOSE:
            self.user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            self.running = False
            self.user32.PostQuitMessage(0)
            return 0
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

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

    def shutdown(self):
        self.user32.ClipCursor(None)
        self.user32.ShowCursor(True)


def main():
    mapname = sys.argv[1] if len(sys.argv) > 1 else "e1m1"
    pak = Pak(PAK_PATH)
    pal = pak.read("gfx/palette.lmp")
    palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]
    bsp = Bsp(pak.read(f"maps/{mapname}.bsp"))
    rend = Renderer(bsp, palette)

    win = SpikeWindow(f"pq.ai gdi spike — {mapname}", 800, 600)
    cw, ch = win.client_size()
    rend.resize(cw, ch)
    blitter = win_ui.GdiBlitter(win.hwnd)

    (sx, sy, sz), yaw = bsp.find_spawn()
    pos = [sx, sy, sz + 22.0]            # eye a bit above the floor
    pitch = 0.0

    t0 = last = time.perf_counter()
    diag_t, diag_frames, diag_look, diag_raw0 = last, 0, 0.0, 0

    while win.running:
        win.pump()                       # drain ALL input first
        if not win.running:
            break
        now = time.perf_counter()
        dt = now - last
        last = now

        dx, dy = win.read_mouse()
        yaw -= dx * LOOK_SENS
        pitch = max(-89.0, min(89.0, pitch + dy * LOOK_SENS))
        diag_look += (abs(dx) + abs(dy)) * LOOK_SENS

        forward, right, up = angle_vectors(yaw, pitch)
        step = FLY_SPEED * dt
        k = win.keys
        move = [0.0, 0.0, 0.0]
        if 0x57 in k:                                    # W
            for i in range(3): move[i] += forward[i]
        if 0x53 in k:                                    # S
            for i in range(3): move[i] -= forward[i]
        if 0x44 in k:                                    # D
            for i in range(3): move[i] += right[i]
        if 0x41 in k:                                    # A
            for i in range(3): move[i] -= right[i]
        if VK_SPACE in k:
            move[2] += 1.0
        if VK_CONTROL in k:
            move[2] -= 1.0
        for i in range(3):
            pos[i] += move[i] * step

        (fb, w, h), _leaf = rend.render_zbuffer(tuple(pos), yaw, pitch,
                                                textured=True, time=now - t0)
        cw, ch = win.client_size()
        if (cw, ch) != (rend.width, rend.height):
            rend.resize(cw, ch)
        hud = (f"gdi spike   yaw {yaw:.0f} pitch {pitch:.0f}   "
               f"WASD/space/ctrl fly   Esc quit")
        blitter.present(fb, w, h, cw, ch,
                        texts=[(8, 8, hud, (0, 255, 102), "nw"),
                               (cw // 2, ch // 2, "+", (0, 255, 102), "center")])

        diag_frames += 1
        if now - diag_t >= 1.0:
            el = now - diag_t
            print(f"[spike] loop/s={diag_frames / el:4.0f}  "
                  f"raw/s={(win.raw_events - diag_raw0) / el:5.0f}  "
                  f"look/s={diag_look / el:6.1f}deg  "
                  f"w={0x57 in win.keys}  fire={win.left_down}",
                  file=sys.stderr)
            diag_t, diag_frames, diag_look = now, 0, 0.0
            diag_raw0 = win.raw_events

    win.shutdown()
    print("spike done")


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("spike_gdi is Windows-only")
    main()
