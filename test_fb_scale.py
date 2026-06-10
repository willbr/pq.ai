"""Framebuffer-to-window scaling for the tkinter textured (z-buffer) blit.

The renderer returns a fixed-size 8-bit framebuffer (Auto = window//zbuf_scale,
or a fixed video-menu resolution like 320x240). Tk's PhotoImage can only scale by
integer factors (zoom up / subsample down), so main.fb_fit picks the largest
integer zoom that fits the window while preserving aspect, or subsamples when the
framebuffer is larger than the window. The frontend then centres the result
(letterbox). Pure logic test -- no Tk window, no pak.
"""

from main import fb_fit


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


if __name__ == "__main__":
    test_all()
    print("OK")
