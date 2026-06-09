"""Integration smoke check for win_ui's live ctypes glue: opens a real Tk window,
grabs its HWND, and drives GdiBlitter.present + RawMouse grab/read/ungrab/shutdown
for a few frames. Verifies the Win32 signatures don't fault; it does NOT verify it
looks right (that needs eyes on the window). Prints OK on success.

Run: python smoke_win_ui.py   (Windows, with a desktop session)"""

import sys
import tkinter as tk

if sys.platform != "win32":
    print("SKIP (Windows-only)")
    sys.exit(0)

import win_ui

root = tk.Tk()
root.title("win_ui smoke")
root.geometry("320x240")
root.update()                                # realise the window so the HWND exists
hwnd = root.winfo_id()

# a tiny RGB gradient framebuffer (80x60, packed 24bpp)
W, H = 80, 60
fb = bytearray(W * H * 3)
for y in range(H):
    for x in range(W):
        i = (y * W + x) * 3
        fb[i] = (x * 255) // W          # R ramps across
        fb[i + 1] = (y * 255) // H      # G ramps down
        fb[i + 2] = 128                 # B constant -> proves R/B not swapped wrong

blitter = win_ui.GdiBlitter(hwnd)
mouse = win_ui.RawMouse(hwnd)
mouse.grab()
try:
    for _ in range(8):
        blitter.present(fb, W, H, 320, 240,
                        texts=[(8, 8, "smoke\nframe", (0, 255, 102), "nw"),
                               (160, 120, "+", (0, 255, 102), "center")])
        root.update()                    # pump messages -> WM_INPUT -> _proc
    dx, dy = mouse.read()
    print(f"raw delta read back: ({dx}, {dy})")
finally:
    mouse.shutdown()
    root.destroy()

print("OK")
