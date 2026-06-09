"""Unit tests for the pure, OS-independent core of win_ui.py (the Windows GDI
blit + raw-mouse front-end). The window/blit/grab ctypes glue needs a live
window and a human at the mouse, so it is verified by running the game; what is
unit-testable here is the colour-channel swap, the raw-mouse delta extraction,
and -- the classic place ctypes breaks -- the RAWINPUT struct layout matching
the Win32 ABI.

Windows-only (win_ui imports ctypes.wintypes); skips elsewhere."""

import sys
import ctypes


def test_bgr_swap_single_pixel():
    """A packed 24bpp pixel R,G,B becomes B,G,R for GDI's BI_RGB DIB order."""
    import win_ui
    assert bytes(win_ui.bgr_swap(b"\x01\x02\x03")) == b"\x03\x02\x01"


def test_bgr_swap_multi_pixel_keeps_green_and_length():
    """Every pixel's R/B swap independently; green stays put, length preserved."""
    import win_ui
    out = bytes(win_ui.bgr_swap(b"\x01\x02\x03\x04\x05\x06"))
    assert out == b"\x03\x02\x01\x06\x05\x04"


def test_to_dib_bgr_no_padding_when_row_already_aligned():
    """A 4px row is 12 bytes, a multiple of 4 (24bpp rows align only at w%4==0)
    -- no padding, just the channel swap, so this matches bgr_swap exactly."""
    import win_ui
    rgb = bytes(range(1, 13))                  # 4 pixels: (1,2,3)(4,5,6)(7,8,9)(10,11,12)
    out = bytes(win_ui.to_dib_bgr(rgb, 4, 1))
    assert out == bytes(win_ui.bgr_swap(rgb))
    assert out == b"\x03\x02\x01\x06\x05\x04\x09\x08\x07\x0c\x0b\x0a"


def test_to_dib_bgr_pads_each_row_to_dword_boundary():
    """A 1px row is 3 bytes; GDI reads DWORD-aligned rows, so each row is padded
    to 4 bytes. Two stacked 1px rows therefore yield 8 bytes, swapped + padded."""
    import win_ui
    out = bytes(win_ui.to_dib_bgr(b"\x01\x02\x03\x0a\x0b\x0c", 1, 2))
    assert out == b"\x03\x02\x01\x00\x0c\x0b\x0a\x00"


def test_raw_mouse_delta_relative_is_passed_through():
    """A MOUSE_MOVE_RELATIVE event (usFlags bit 0 clear) carries deltas directly."""
    import win_ui
    assert win_ui.raw_mouse_delta(win_ui.MOUSE_MOVE_RELATIVE, 5, -3) == (5, -3)


def test_raw_mouse_delta_absolute_yields_no_motion():
    """A MOUSE_MOVE_ABSOLUTE event (touchpad / RDP) carries screen coords, not
    deltas -- applying them would snap the view, so it must yield (0, 0).
    This is the raw-input analogue of look_delta's warp-straddle guard."""
    import win_ui
    assert win_ui.raw_mouse_delta(win_ui.MOUSE_MOVE_ABSOLUTE, 1234, 5678) == (0, 0)


def test_left_button_press_and_release_track_held_state():
    """With RIDEV_NOLEGACY the mouse stops emitting WM_LBUTTONDOWN/UP, so fire is
    read from RAWMOUSE button flags: a DOWN flag holds the button, an UP flag
    releases it."""
    import win_ui
    assert win_ui.apply_left_button(False, win_ui.RI_MOUSE_LEFT_BUTTON_DOWN) is True
    assert win_ui.apply_left_button(True, win_ui.RI_MOUSE_LEFT_BUTTON_UP) is False


def test_left_button_unchanged_when_no_transition():
    """Button flags only report transitions; a packet with neither flag (pure
    motion, or a held button) leaves the held state as it was."""
    import win_ui
    assert win_ui.apply_left_button(True, 0) is True       # held across motion
    assert win_ui.apply_left_button(False, 0) is False


def test_left_button_down_and_up_in_one_packet_ends_released():
    """A down+up coalesced into one packet ends released (no stuck fire)."""
    import win_ui
    flags = win_ui.RI_MOUSE_LEFT_BUTTON_DOWN | win_ui.RI_MOUSE_LEFT_BUTTON_UP
    assert win_ui.apply_left_button(False, flags) is False


def test_rawmouse_layout_matches_win32_abi():
    """RAWMOUSE is 24 bytes with lLastX at offset 12 -- a wrong layout silently
    reads garbage deltas, so pin it down."""
    import win_ui
    assert ctypes.sizeof(win_ui.RAWMOUSE) == 24
    assert win_ui.RAWMOUSE.lLastX.offset == 12


def test_rawinput_layout_matches_win32_abi():
    """RAWINPUTHEADER is two DWORDs plus two pointer-sized members; RAWINPUT is
    that header followed by the RAWMOUSE union. Compute the pointer-size-aware
    expected sizes so this holds on 32- and 64-bit."""
    import win_ui
    ptr = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(win_ui.RAWINPUTHEADER) == 8 + 2 * ptr
    assert ctypes.sizeof(win_ui.RAWINPUT) == 8 + 2 * ptr + 24


if __name__ == "__main__":
    if sys.platform != "win32":
        print("SKIP (win_ui is Windows-only)")
        sys.exit(0)
    test_bgr_swap_single_pixel()
    test_bgr_swap_multi_pixel_keeps_green_and_length()
    test_to_dib_bgr_no_padding_when_row_already_aligned()
    test_to_dib_bgr_pads_each_row_to_dword_boundary()
    test_raw_mouse_delta_relative_is_passed_through()
    test_raw_mouse_delta_absolute_yields_no_motion()
    test_left_button_press_and_release_track_held_state()
    test_left_button_unchanged_when_no_transition()
    test_left_button_down_and_up_in_one_packet_ends_released()
    test_rawmouse_layout_matches_win32_abi()
    test_rawinput_layout_matches_win32_abi()
    print("OK")
