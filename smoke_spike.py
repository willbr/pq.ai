"""Non-interactive smoke for spike_gdi: construct the gdi32 window + raw grab,
render one real frame and present it, pump a few messages, then shut down (always
releasing the cursor clip). Catches ctypes signature / window-creation faults
without needing a human; it does NOT verify it looks right. Prints OK on success."""

import sys

if sys.platform != "win32":
    print("SKIP (Windows-only)")
    sys.exit(0)

import time
import spike_gdi
import win_ui
from quake.pak import Pak
from quake.bsp import Bsp
from quake.render import Renderer

pak = Pak(spike_gdi.PAK_PATH)
pal = pak.read("gfx/palette.lmp")
palette = [(pal[i * 3], pal[i * 3 + 1], pal[i * 3 + 2]) for i in range(256)]
bsp = Bsp(pak.read("maps/e1m1.bsp"))
rend = Renderer(bsp, palette)

win = spike_gdi.SpikeWindow("spike smoke", 320, 240)
try:
    cw, ch = win.client_size()
    rend.resize(cw, ch)
    blitter = win_ui.GdiBlitter(win.hwnd)
    (sx, sy, sz), yaw = bsp.find_spawn()
    for _ in range(3):
        win.pump()
        (fb, w, h), _ = rend.render_zbuffer((sx, sy, sz + 22.0), yaw, 0.0,
                                            textured=True, time=0.0)
        blitter.present(fb, w, h, cw, ch,
                        texts=[(8, 8, "smoke", (0, 255, 102), "nw")])
    print(f"raw_events={win.raw_events} running={win.running}")
finally:
    win.shutdown()

print("OK")
