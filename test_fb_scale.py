"""Framebuffer-to-window scaling for the tkinter textured (z-buffer) blit.

The renderer returns a fixed-size 8-bit framebuffer (Auto = window//zbuf_scale,
or a fixed video-menu resolution like 320x240). Tk's PhotoImage can only scale by
integer factors (zoom up / subsample down), so main.fb_fit picks the largest
integer zoom that fits the window while preserving aspect, or subsamples when the
framebuffer is larger than the window. The frontend then centres the result
(letterbox). Pure logic test -- no Tk window, no pak.
"""

from main import fb_fit, pal_channel_tables, expand_fb_to_ppm


def _naive_ppm(fb, w, h, pal):
    """The old per-pixel expansion: one __getitem__ per byte, joined. The
    translate-based expand_fb_to_ppm must reproduce this exactly."""
    lut = [bytes(c) for c in pal]
    return b"P6 %d %d 255 " % (w, h) + b"".join(map(lut.__getitem__, fb))


def test_present_expansion_matches_naive():
    # full 256 palette, every index exercised; translate path == naive path
    pal = [((i * 7) & 255, (i * 13) & 255, (i * 29) & 255) for i in range(256)]
    fb = bytes(range(256)) * 8         # 2048 px, w*h must match
    w, h = 64, 32
    r, g, b = pal_channel_tables(pal)
    assert expand_fb_to_ppm(fb, w, h, r, g, b) == _naive_ppm(fb, w, h, pal)


def test_present_expansion_bytearray_fb():
    # the renderer hands us a bytearray framebuffer, not bytes
    pal = [(i, 255 - i, (i * 3) & 255) for i in range(256)]
    fb = bytearray(b"\x00\x7f\xff\x01" * 16)   # 64 px
    w, h = 8, 8
    r, g, b = pal_channel_tables(pal)
    out = expand_fb_to_ppm(fb, w, h, r, g, b)
    assert out == _naive_ppm(fb, w, h, pal)
    assert len(out) == len(b"P6 8 8 255 ") + 3 * 64


def test_short_palette_padded():
    # a palette shorter than 256 still yields full 256-byte tables (no IndexError
    # if the fb references a high index -- it just maps to zero)
    pal = [(10, 20, 30), (40, 50, 60)]
    r, g, b = pal_channel_tables(pal)
    assert len(r) == len(g) == len(b) == 256
    assert (r[0], g[0], b[0]) == (10, 20, 30)
    assert (r[255], g[255], b[255]) == (0, 0, 0)


def test_auto_fills_window_exactly():
    # Auto: framebuffer = window // zbuf_scale(4) = 200x150 -> zoom 4 -> 800x600
    assert fb_fit(800, 600, 200, 150) == (4, 1)


def test_fixed_320x240_best_fits_and_letterboxes():
    # 320x240 in 800x600: zoom 2 -> 640x480 (centred with a border), not zoom 4
    assert fb_fit(800, 600, 320, 240) == (2, 1)


def test_exact_fit_no_scaling():
    assert fb_fit(800, 600, 800, 600) == (1, 1)


def test_aspect_preserved_picks_min_factor():
    # 80x40 (2:1) in 800x600: min(800//80=10, 600//40=15) = 10 -> 800x400
    assert fb_fit(800, 600, 80, 40) == (10, 1)


def test_framebuffer_larger_than_window_subsamples():
    # 640x480 fb in a 400x300 window: subsample 2 -> 320x240 fits
    assert fb_fit(400, 300, 640, 480) == (1, 2)


def test_subsample_factor_covers_both_dims():
    # 1000x300 fb in 300x300 window: need ceil(1000/300)=4 to fit width
    assert fb_fit(300, 300, 1000, 300) == (1, 4)


def test_degenerate_sizes_are_safe():
    assert fb_fit(800, 600, 0, 0) == (1, 1)
    assert fb_fit(0, 0, 320, 240) == (1, 1)


def test_all():
    test_auto_fills_window_exactly()
    test_fixed_320x240_best_fits_and_letterboxes()
    test_exact_fit_no_scaling()
    test_aspect_preserved_picks_min_factor()
    test_framebuffer_larger_than_window_subsamples()
    test_subsample_factor_covers_both_dims()
    test_degenerate_sizes_are_safe()
    test_present_expansion_matches_naive()
    test_present_expansion_bytearray_fb()
    test_short_palette_padded()


if __name__ == "__main__":
    test_all()
    print("OK")
