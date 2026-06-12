"""Mouselook delta guard.

Mouselook recenters the cursor (a "warp") when it nears a window edge. On Windows
the warp's <Motion> arrives asynchronously and unsuppressed, so an event that
straddles the teleport reports a window-sized delta; applied to yaw/pitch that
snapped the view to a random angle. look_delta() must drop those window-scale
deltas while passing genuine (small) moves through unchanged.

Pure logic test -- no Tk window, no pak, no shareware data needed.
"""

import _bootstrap  # noqa: F401  (repo-root sys.path + cwd)

from main import look_delta, MOUSE_MARGIN

W, H = 800, 600
JUMP_X = W // 2 - MOUSE_MARGIN          # smallest delta a recenter warp can inject
JUMP_Y = H // 2 - MOUSE_MARGIN


def test_seed_on_none():
    # first event after capture just seeds the reference, moves nothing
    last, dx, dy = look_delta(None, 400, 300, W, H, MOUSE_MARGIN)
    assert last == (400, 300) and dx == 0 and dy == 0


def test_real_move_applies():
    last, dx, dy = look_delta((400, 300), 415, 308, W, H, MOUSE_MARGIN)
    assert last == (415, 308) and dx == 15 and dy == 8


def test_fast_but_real_flick_not_clipped():
    # a 250px flick is large but below the warp threshold (300) -> still real input
    _last, dx, _dy = look_delta((400, 300), 650, 300, W, H, MOUSE_MARGIN)
    assert dx == 250


def test_horizontal_warp_straddle_rejected():
    # last==centre (just recentred); a stale edge event reports a ~half-window jump
    last, dx, dy = look_delta((400, 300), 795, 300, W, H, MOUSE_MARGIN)
    assert dx == 0 and dy == 0 and last == (795, 300)   # dropped, but re-seeds


def test_vertical_warp_straddle_rejected():
    _last, dx, dy = look_delta((400, 300), 400, 520, W, H, MOUSE_MARGIN)
    assert dy == 0 and dx == 0


def test_min_straddle_at_threshold_is_rejected():
    # the smallest delta a recenter can produce is exactly (half - margin); the
    # guard must catch it (>=), or a just-triggered warp would still leak a jump
    _last, dx, _dy = look_delta((400, 300), 400 + JUMP_X, 300, W, H, MOUSE_MARGIN)
    assert dx == 0


def test_async_warp_stream_has_no_per_event_snap():
    """Replay the Windows async-warp event stream: turn right to the edge (real
    moves), the app recenters (last reset to centre), then the stale edge event and
    the warp's own move event both straddle the teleport. No single event may inject
    a window-scale step -- that snap is the bug."""
    last = (400, 300)
    max_step = 0
    for x in (430, 470, 520, 580, 650, 720, 790):     # real moves toward the edge
        last, dx, _dy = look_delta(last, x, 300, W, H, MOUSE_MARGIN)
        max_step = max(max_step, abs(dx))
    last = (400, 300)                                   # _warp_center reset
    for x in (798, 400, 405, 412):    # stale-edge, warp-move, then real moves
        last, dx, _dy = look_delta(last, x, 300, W, H, MOUSE_MARGIN)
        max_step = max(max_step, abs(dx))
    # the straddles (≈398, ≈-398) were dropped; only genuine small moves remain
    assert max_step < JUMP_X


def test_all():
    test_seed_on_none()
    test_real_move_applies()
    test_fast_but_real_flick_not_clipped()
    test_horizontal_warp_straddle_rejected()
    test_vertical_warp_straddle_rejected()
    test_min_straddle_at_threshold_is_rejected()
    test_async_warp_stream_has_no_per_event_snap()


if __name__ == "__main__":
    test_all()
    print("OK")
