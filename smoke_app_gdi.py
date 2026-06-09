"""Live integration smoke: boot the real App (real shareware data, real renderer)
and drive a handful of frames through the Windows GDI textured path with mouselook
engaged, then tear down. Catches wiring faults (present args, overlay toggle, raw
grab) against the actual render_zbuffer output -- not visual correctness, which
needs eyes on the window. Prints OK on success.

Run: python smoke_app_gdi.py   (Windows, desktop session, shareware pak present)"""

import sys

if sys.platform != "win32":
    print("SKIP (Windows-only)")
    sys.exit(0)

import main

app = main.App("e1m1")
assert app.gdi is not None, "GdiBlitter not created on win32"
assert app.rawmouse is not None, "RawMouse not created on win32"

# tick() reschedules itself via root.after; with no mainloop we drive it by hand,
# so neutralise the reschedule (otherwise root.update() would pump it forever, as
# a textured frame far exceeds the 16ms timer and is always overdue). The raw
# WM_INPUT path is covered by smoke_win_ui; here we exercise the GDI render path.
app.root.after = lambda *a, **k: None

# force the GDI textured path and engage mouselook (real cursor grab + raw device)
app.zbuf = True
app.gdi_present = True
app._set_mouselook(True)

for i in range(5):
    app.tick()                       # runs server + render_zbuffer + gdi.present
assert app._overlays_visible is False, "Tk overlays should be hidden on GDI path"

# flip to the PPM path mid-run: overlays must come back
app.gdi_present = False
app.tick()
assert app._overlays_visible is True, "Tk overlays should return on PPM path"

app._set_mouselook(False)            # release grab (ClipCursor/ShowCursor undone)
app.rawmouse.shutdown()              # restore the original WndProc
app.root.destroy()
print("OK")
