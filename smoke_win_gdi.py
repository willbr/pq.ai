"""Non-interactive smoke for win_gdi: construct the gdi32 window + the full Client +
a GdiBlitter, force the textured (zbuf) path, then step a few real frames (pump +
client.frame + present) and shut down (always releasing the cursor clip). Catches
ctypes signature / window-creation / Client wiring faults without needing a human;
it does NOT verify it looks right. Runs to completion (no infinite loop). Prints OK."""

import sys

if sys.platform != "win32":
    print("SKIP (Windows-only)")
    sys.exit(0)

import win_gdi
import win_ui
from client import Client, InputState

win = win_gdi.GameWindow("win_gdi smoke", 320, 240)
try:
    client = Client("e1m1")
    blitter = win_ui.GdiBlitter(win.hwnd)
    cw, ch = win.client_size()
    client.resize(cw, ch)
    win_gdi._force_zbuf(client)
    assert client.mode == "zbuf", f"expected zbuf, got {client.mode}"
    for _ in range(3):
        win.pump()
        rf = client.frame(0.016, InputState())
        fb, w, h = rf.framebuffer
        texts = list(rf.overlays) + [
            (rf.crosshair[0], rf.crosshair[1], "+", (0, 255, 102), "center")]
        blitter.present(fb, w, h, cw, ch, texts=texts)

    # Exercise the double-buffered vector path: wireframe then flat-shaded.
    # mode is only recomputed by client.frame when commands are present, so
    # setting it directly + framing with no commands keeps the chosen mode.
    client.mode = "wire"
    rf = client.frame(0.016, InputState())
    blitter.present_vector(rf.segs, None, rf.particles, cw, ch, rf.overlays)
    assert rf.segs is not None, "wire frame should produce segs"

    client.mode = "flat"
    rf = client.frame(0.016, InputState())
    blitter.present_vector(None, rf.polys, rf.particles, cw, ch, rf.overlays)
    assert rf.polys is not None, "flat frame should produce polys"

    print(f"mode={client.mode} raw_events={win.raw_events} running={win.running}")
finally:
    try:
        blitter.close()
    except NameError:
        pass
    win.shutdown()

print("OK")
