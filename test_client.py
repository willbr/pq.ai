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


if __name__ == "__main__":
    test_inputstate_defaults_are_neutral()
    test_renderframe_holds_mode_and_overlays()
    test_client_boots_e1m1_with_spawn_and_viewport()
    print("OK")
