"""Headless tests for the UI-agnostic Client core and its two data contracts.
Boots the full stack against the shareware pak (like the other test_*.py), so it
fails without quake-shareware/id1/pak0.pak."""

import client


def test_inputstate_defaults_are_neutral():
    inp = client.InputState()
    assert inp.move_forward == 0.0 and inp.move_strafe == 0.0 and inp.move_up == 0.0
    assert inp.turn == 0.0 and inp.look_dx == 0.0 and inp.look_dy == 0.0
    assert inp.run is False and inp.fire is False and inp.impulse == 0
    assert inp.commands == frozenset()


def test_renderframe_holds_mode_and_overlays():
    rf = client.RenderFrame(mode="wire", segs=[(0, 0, 1, 1)],
                            overlays=[(8, 8, "hi", (0, 255, 0), "nw")],
                            crosshair=(50, 50))
    assert rf.mode == "wire"
    assert rf.segs == [(0, 0, 1, 1)]
    assert rf.overlays[0][2] == "hi"


def test_client_boots_e1m1_with_spawn_and_viewport():
    c = client.Client("e1m1")
    c.resize(800, 600)
    assert len(c.pos) == 3                 # player origin from the spawn point
    assert isinstance(c.yaw, float)
    assert c.mode in ("wire", "flat", "zbuf")


def test_frame_returns_zbuf_renderframe_sized_to_viewport():
    c = client.Client("e1m1")
    c.resize(800, 600)
    c.mode = "zbuf"
    rf = c.frame(0.016, client.InputState())
    assert rf.mode == "zbuf"
    fb, w, h = rf.framebuffer
    assert w == 800 // 4 and h == 600 // 4        # ZBUF_SCALE == 4
    assert len(fb) == w * h * 3                    # packed RGB
    assert any("fps" in o[2] for o in rf.overlays) # HUD line present


def test_frame_forward_input_moves_player():
    c = client.Client("e1m1")
    c.resize(320, 240)
    c.noclip = True                                # fly so movement is unconstrained
    start = list(c.pos)
    for _ in range(5):
        c.frame(0.05, client.InputState(move_forward=1.0))
    assert c.pos != start


if __name__ == "__main__":
    test_inputstate_defaults_are_neutral()
    test_renderframe_holds_mode_and_overlays()
    test_client_boots_e1m1_with_spawn_and_viewport()
    test_frame_returns_zbuf_renderframe_sized_to_viewport()
    test_frame_forward_input_moves_player()
    print("OK")
